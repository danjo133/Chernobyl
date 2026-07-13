#!/bin/sh
# Gateway entrypoint: program the transparent-proxy firewall, start the broker store,
# start mitmproxy, and publish the public CA cert.
#
# UNVERIFIED scaffold — the iptables/transparent-proxy wiring in particular needs a
# real run to validate (see docs/SANDBOX-PLAN.md §3.1, §12). Runs as root (needs
# NET_ADMIN to set rules); mitmproxy/redis drop to the unprivileged `proxy` uid.
set -eu

PROXY_USER=mitm
PROXY_UID=$(id -u "$PROXY_USER")
MITM_PORT=8080
CONFDIR=/home/mitm/.mitmproxy

# Tripwire: the proxy uid must not collide with the workload's uid (default node=1000).
# A collision makes the workload match the proxy's iptables RETURN exemption and bypass
# the firewall (see Dockerfile). Refuse to start rather than run wide open.
if [ "$PROXY_UID" = "1000" ]; then
  echo "gateway: FATAL — proxy uid 1000 collides with the workload; egress filter would be bypassed." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# sysctls (compose sets these too; harmless to reassert)
# ---------------------------------------------------------------------------
sysctl -w net.ipv4.ip_forward=1            >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.all.rp_filter=0    >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.all.send_redirects=0 >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# The CA confdir is a named volume (gateway-ca) mounted root-owned over mitm's
# home; mitmproxy runs as `mitm` and must be able to create the CA there.
# ---------------------------------------------------------------------------
mkdir -p "$CONFDIR"
# Recursive: the volume persists across rebuilds, so pre-existing CA files may be
# owned by a previous proxy uid and must be reclaimed by the current one.
chown -R "$PROXY_USER:$PROXY_USER" "$CONFDIR"

# ---------------------------------------------------------------------------
# IPv6: no egress at all (prevents bypassing the IPv4 filter over v6)
# ---------------------------------------------------------------------------
ip6tables -F 2>/dev/null || true
ip6tables -P OUTPUT DROP 2>/dev/null || true
ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true

# Always restore Docker's embedded-DNS hook first (both modes need working DNS).
# Flush ONLY our own OUTPUT chain — never the whole nat table. `iptables -t nat -F`
# wipes Docker's embedded-DNS hook (the OUTPUT -> DOCKER_OUTPUT jump that DNATs the
# 127.0.0.11 resolver), which silently breaks name resolution for the workload.
iptables -t nat -F OUTPUT
if iptables -t nat -L DOCKER_OUTPUT -n >/dev/null 2>&1; then
  iptables -t nat -A OUTPUT -d 127.0.0.11/32 -j DOCKER_OUTPUT
fi

ALLOW_INTERNET="${ALLOW_INTERNET:-0}"
if [ "$ALLOW_INTERNET" = "1" ]; then
  # ---------------------------------------------------------------------------
  # UNRESTRICTED egress: no REDIRECT to mitmproxy, no allowlist, no MITM. The
  # workload talks straight to the internet over real TLS. Opt-in escape hatch
  # (sandbox --allow-internet) — drops the egress-containment guarantee.
  # ---------------------------------------------------------------------------
  echo "gateway: ALLOW_INTERNET=1 — UNRESTRICTED egress (no allowlist, no MITM)."
  iptables -F OUTPUT
  iptables -P OUTPUT ACCEPT
else
  # ---------------------------------------------------------------------------
  # IPv4 NAT: transparently redirect the workload's HTTP(S) to mitmproxy.
  # The proxy's OWN traffic (uid=proxy) is exempted so it can reach upstreams.
  # ---------------------------------------------------------------------------
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
  # REDIRECT'd packets (dst rewritten to 127.0.0.1:MITM) do NOT match `-o lo` here:
  # the reroute to lo happens only AFTER the whole LOCAL_OUT hook chain, so filter
  # OUTPUT still sees the original eth0 route. Match by rewritten destination instead.
  iptables -A OUTPUT -p tcp -d 127.0.0.1 --dport "$MITM_PORT" -j ACCEPT
  iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
  iptables -A OUTPUT -m owner --uid-owner "$PROXY_UID" -j ACCEPT
  iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
  iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
  # (REDIRECT'd 80/443 packets now have a local destination -> matched by lo/ESTABLISHED.)
  # TODO: optionally run dnsmasq here restricted to the allowlist for DNS-level defense in depth.
fi

# ---------------------------------------------------------------------------
# Broker store: redis on a unix socket only (no TCP). Lives in the gateway's
# mount namespace, which the workload does NOT share — so it is unreachable from
# the workload by construction. proxy-only socket perms as belt-and-braces.
#
# Credentials are INTENTIONALLY EPHEMERAL: `--save "" --appendonly no` keeps the
# store in memory only, so `sandbox down` wipes all secrets — nothing sensitive at
# rest. Change the egress allowlist with `sandbox allow` (hot-reloaded, no teardown)
# instead of down/up so creds aren't lost needlessly. To make creds survive down/up,
# enable AOF here pointing at the broker-data volume — but that writes tokens to disk
# in plaintext; weigh it against the at-rest exposure first (see SANDBOX-PLAN §8, §15).
# ---------------------------------------------------------------------------
runuser -u "$PROXY_USER" -- redis-server \
  --unixsocket /run/broker/redis.sock --unixsocketperm 700 \
  --port 0 --dir /var/lib/broker --save "" --appendonly no \
  --daemonize no &

# ---------------------------------------------------------------------------
# Control endpoint + request log live in the bind-mounted control dir so the
# HOST-side web UI can reach them. The workload does NOT share this mount
# namespace, so neither the socket nor the log is reachable from the workload.
# The proxy uid (unprivileged) must be able to create the socket + log file here;
# the dir is host-private (the CLI's .broker-control-<name>/), so loose perms only
# affect other LOCAL host users — single-user-host assumption (SANDBOX-PLAN §15).
# ---------------------------------------------------------------------------
mkdir -p /run/broker-control
chmod 0777 /run/broker-control
runuser -u "$PROXY_USER" -- python3 /opt/broker/control_server.py &

# ---------------------------------------------------------------------------
# Optional: chain inspected flows to a downstream Caido proxy (the operator's
# interactive workbench) when CAIDO_UPSTREAM is set — i.e. the tools overlay is
# active. mitmproxy becomes the TLS *client* to Caido, so it must trust Caido's
# CA; we fetch it from Caido's /ca.crt endpoint and append it to the default
# bundle. On success we drop a marker the broker addon reads to enable per-flow
# `via` routing. Best-effort: if Caido never answers, we log and come up in
# normal restricted mode (no chaining) rather than wedge the whole sandbox.
#
# The fetch MUST run as the proxy uid — it is the only identity the egress filter
# lets past; root's packets to Caido are dropped by the OUTPUT chain.
# ---------------------------------------------------------------------------
MITM_EXTRA=""
CAIDO_MARKER=/run/broker/caido-upstream
rm -f "$CAIDO_MARKER"
if [ -n "${CAIDO_UPSTREAM:-}" ]; then
  CAIDO_CA="$CONFDIR/caido-upstream-ca.pem"
  echo "gateway: CAIDO_UPSTREAM=$CAIDO_UPSTREAM — fetching Caido CA..."
  i=0; ok=0
  while [ "$i" -lt 60 ]; do
    if runuser -u "$PROXY_USER" -- curl -fsS --max-time 3 \
         "http://${CAIDO_UPSTREAM}/ca.crt" -o /tmp/caido-ca.in 2>/dev/null; then
      ok=1; break
    fi
    sleep 1; i=$((i + 1))
  done
  if [ "$ok" = "1" ] \
     && { openssl x509 -in /tmp/caido-ca.in -out /tmp/caido-ca.pem 2>/dev/null \
          || openssl x509 -inform DER -in /tmp/caido-ca.in -out /tmp/caido-ca.pem 2>/dev/null; }; then
    # Append the default trust bundle so any non-chained flow still verifies normally
    # (ssl_verify_upstream_trusted_ca REPLACES the store rather than adding to it).
    certifi="$(python3 -m certifi 2>/dev/null || true)"
    cat /tmp/caido-ca.pem ${certifi:+"$certifi"} > "$CAIDO_CA"
    chown "$PROXY_USER:$PROXY_USER" "$CAIDO_CA"
    printf '%s' "$CAIDO_UPSTREAM" > "$CAIDO_MARKER"
    chown "$PROXY_USER:$PROXY_USER" "$CAIDO_MARKER"
    # lazy: don't open the upstream connection before the addon can re-point `via`.
    # upstream_cert=false: mint the client-facing cert from SNI, don't probe upstream.
    MITM_EXTRA="--set connection_strategy=lazy --set upstream_cert=false --set ssl_verify_upstream_trusted_ca=$CAIDO_CA"
    echo "gateway: Caido CA trusted; chaining inspected flows -> $CAIDO_UPSTREAM."
  else
    echo "gateway: WARNING — Caido CA unavailable at $CAIDO_UPSTREAM; starting WITHOUT chaining." >&2
  fi
fi

# ---------------------------------------------------------------------------
# mitmproxy (transparent). The addon does allowlist + credential injection.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2086  # MITM_EXTRA is intentionally word-split into flags.
runuser -u "$PROXY_USER" -- mitmdump \
  --mode transparent --showhost \
  --listen-port "$MITM_PORT" \
  --set confdir="$CONFDIR" \
  --set block_global=false \
  --set stream_large_bodies=10m \
  $MITM_EXTRA \
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
