#!/bin/sh
# Egress firewall, NETNS mode — the original Docker shape, moved here unchanged.
#
# The workload shares this network namespace (`network_mode: service:gateway`),
# so its packets are born here and are filtered in OUTPUT. The proxy is told
# apart from the workload by uid, which is why the tripwire below matters: if
# the two uids ever match, the workload inherits the proxy's exemption and
# egress stops being filtered at all.
#
# Router mode has no such split. See firewall-router.sh.
#
# Reads from the environment: PROXY_UID, MITM_PORT, ALLOW_INTERNET.
set -eu

: "${PROXY_UID:?firewall-netns: PROXY_UID not set}"
: "${MITM_PORT:?firewall-netns: MITM_PORT not set}"

# Tripwire: the proxy uid must not collide with the workload's uid (default node=1000).
# A collision makes the workload match the proxy's iptables RETURN exemption and bypass
# the firewall (see Dockerfile). Refuse to start rather than run wide open.
if [ "$PROXY_UID" = "1000" ]; then
  echo "gateway: FATAL — proxy uid 1000 collides with the workload; egress filter would be bypassed." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# IPv6: no egress at all (prevents bypassing the IPv4 filter over v6).
# Fail CLOSED: the IPv4 NAT/REDIRECT below is v4-only, so if we cannot install a
# v6 DROP policy AND the container actually has global IPv6, the workload would get
# unfiltered, un-MITM'd v6 egress. Refuse to start in that case (mirrors the
# uid-collision tripwire) rather than silently running wide open. If there is no
# global IPv6 at all, a missing ip6tables is harmless — warn and continue.
# ---------------------------------------------------------------------------
if ip6tables -F 2>/dev/null && ip6tables -P OUTPUT DROP 2>/dev/null; then
  ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
elif ip -6 addr show scope global 2>/dev/null | grep -q "inet6"; then
  echo "gateway: FATAL — ip6tables unavailable but the container has global IPv6; egress would bypass the filter over v6." >&2
  exit 1
else
  echo "gateway: ip6tables unavailable and no global IPv6 present — continuing (no v6 egress path)." >&2
fi

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
  # HTTPS only. We deliberately do NOT redirect cleartext port 80: on HTTP the upstream
  # is unauthenticated (mitmproxy has no cert to verify), so a workload that controls the
  # original destination IP could point port 80 at an attacker box, send `Host: <allowed>`,
  # and have the broker staple a real credential onto a request delivered IN CLEARTEXT to
  # the attacker. Injecting only over verified TLS closes that confused-deputy exfil path.
  # With no REDIRECT here, port-80 egress falls through to the OUTPUT DROP below (fails).
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
  # DNS: allow ONLY Docker's embedded resolver (127.0.0.11, reached over lo and already
  # accepted above). Do NOT allow port 53 to arbitrary resolvers — that is a high-bandwidth
  # DNS-tunnel exfil/C2 channel (iodine/dnscat) straight past the allowlist. The embedded
  # resolver forwards external lookups host-side, so no outbound 53 from this netns is needed
  # for normal name resolution. These explicit rules are belt-and-braces over the `-o lo` rule.
  iptables -A OUTPUT -p udp -d 127.0.0.11 --dport 53 -j ACCEPT
  iptables -A OUTPUT -p tcp -d 127.0.0.11 --dport 53 -j ACCEPT
  # (REDIRECT'd 443 packets now have a local destination -> matched by lo/ESTABLISHED.)
fi
