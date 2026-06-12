#!/bin/sh
# Gateway entrypoint: program the transparent-proxy firewall, start the broker store,
# start mitmproxy, and publish the public CA cert.
#
# UNVERIFIED scaffold — the iptables/transparent-proxy wiring in particular needs a
# real run to validate (see docs/SANDBOX-PLAN.md §3.1, §12). Runs as root (needs
# NET_ADMIN to set rules); mitmproxy/redis drop to the unprivileged `proxy` uid.
set -eu

PROXY_USER=proxy
PROXY_UID=$(id -u "$PROXY_USER")
MITM_PORT=8080
CONFDIR=/home/proxy/.mitmproxy

# ---------------------------------------------------------------------------
# sysctls (compose sets these too; harmless to reassert)
# ---------------------------------------------------------------------------
sysctl -w net.ipv4.ip_forward=1            >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.all.rp_filter=0    >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.all.send_redirects=0 >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# IPv6: no egress at all (prevents bypassing the IPv4 filter over v6)
# ---------------------------------------------------------------------------
ip6tables -F 2>/dev/null || true
ip6tables -P OUTPUT DROP 2>/dev/null || true
ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true

# ---------------------------------------------------------------------------
# IPv4 NAT: transparently redirect the workload's HTTP(S) to mitmproxy.
# The proxy's OWN traffic (uid=proxy) is exempted so it can reach upstreams.
# ---------------------------------------------------------------------------
iptables -t nat -F
iptables -t nat -A OUTPUT -m owner --uid-owner "$PROXY_UID" -j RETURN
iptables -t nat -A OUTPUT -o lo -j RETURN
iptables -t nat -A OUTPUT -p tcp --dport 80  -j REDIRECT --to-ports "$MITM_PORT"
iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT --to-ports "$MITM_PORT"

# ---------------------------------------------------------------------------
# IPv4 filter: workload may only reach loopback, DNS, and the (local) proxy.
# The proxy itself is unrestricted at L3 — it enforces the domain allowlist in
# broker_addon.py. Everything else from the workload is dropped.
# ---------------------------------------------------------------------------
iptables -F OUTPUT
iptables -P OUTPUT DROP
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "$PROXY_UID" -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
# (REDIRECT'd 80/443 packets now have a local destination -> matched by lo/ESTABLISHED.)
# TODO: optionally run dnsmasq here restricted to the allowlist for DNS-level defense in depth.

# ---------------------------------------------------------------------------
# Broker store: redis on a unix socket only (no TCP). Lives in the gateway's
# mount namespace, which the workload does NOT share — so it is unreachable from
# the workload by construction. proxy-only socket perms as belt-and-braces.
# ---------------------------------------------------------------------------
runuser -u "$PROXY_USER" -- redis-server \
  --unixsocket /run/broker/redis.sock --unixsocketperm 700 \
  --port 0 --dir /var/lib/broker --save "" --appendonly no \
  --daemonize no &

# ---------------------------------------------------------------------------
# mitmproxy (transparent). The addon does allowlist + credential injection.
# ---------------------------------------------------------------------------
runuser -u "$PROXY_USER" -- mitmdump \
  --mode transparent --showhost \
  --listen-port "$MITM_PORT" \
  --set confdir="$CONFDIR" \
  --set block_global=false \
  --set stream_large_bodies=10m \
  -s /opt/broker/broker_addon.py &
MITM_PID=$!

# ---------------------------------------------------------------------------
# Publish the PUBLIC CA cert (never the key) for the workload to trust.
# ---------------------------------------------------------------------------
echo "gateway: waiting for mitmproxy CA..."
i=0
while [ ! -f "$CONFDIR/mitmproxy-ca-cert.pem" ] && [ "$i" -lt 150 ]; do
  sleep 0.2; i=$((i + 1))
done
if [ -f "$CONFDIR/mitmproxy-ca-cert.pem" ]; then
  cp "$CONFDIR/mitmproxy-ca-cert.pem" /ca-pub/mitmproxy-ca-cert.pem
  chmod 644 /ca-pub/mitmproxy-ca-cert.pem
  echo "gateway: published public CA cert."
else
  echo "gateway: WARNING — CA cert never appeared." >&2
fi

wait "$MITM_PID"
