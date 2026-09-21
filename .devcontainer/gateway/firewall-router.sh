#!/bin/sh
# Egress firewall, ROUTER mode — the gateway is a separate machine.
#
# Used where the workload cannot share a network namespace with the gateway:
# Incus instances (Omni D20), and anywhere else the two are separate hosts on a
# private link. The workload sits alone on GW_BOT_IF and routes through here.
#
# What changes from netns mode, and why it is stronger:
#
#   netns   one namespace. Filtering in `output`. The proxy is told apart from
#           the workload by uid, so a uid collision bypasses the firewall.
#   router  two machines. Filtering in `prerouting` and `forward`. The proxy is
#           told apart by interface, which cannot collide.
#
# Environment:
#   MITM_PORT         where mitmproxy listens (required)
#   ALLOW_INTERNET    1 = unrestricted escape hatch, as in netns mode
#   GW_UPLINK_IF      interface facing the network      (default eth0)
#   GW_BOT_IF         interface facing the workload     (default eth1)
#   GW_ADDR           this gateway on the bot link      (default 10.77.0.1)
#   GW_PREFIX         prefix length for GW_ADDR         (default 24)
set -eu

: "${MITM_PORT:?firewall-router: MITM_PORT not set}"

UPLINK_IF="${GW_UPLINK_IF:-eth0}"
BOT_IF="${GW_BOT_IF:-eth1}"
ADDR="${GW_ADDR:-10.77.0.1}"
PREFIX="${GW_PREFIX:-24}"
ALLOW_INTERNET="${ALLOW_INTERNET:-0}"

command -v nft >/dev/null || {
  echo "gateway: FATAL — router mode needs nftables and nft is not installed." >&2
  exit 1
}

# Both interfaces must exist. Without the bot link there is nothing to filter;
# without the uplink the proxy cannot reach anything. Either way, refuse to
# start rather than come up in a shape nobody designed.
for i in "$UPLINK_IF" "$BOT_IF"; do
  ip link show "$i" >/dev/null 2>&1 || {
    echo "gateway: FATAL — router mode expects interface '$i', which is not present." >&2
    exit 1
  }
done

sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1 || true

# The bot link. No NAT and no address on the host side of the bridge, so the
# only thing on this wire that answers is this gateway.
ip addr flush dev "$BOT_IF" 2>/dev/null || true
ip addr add "$ADDR/$PREFIX" dev "$BOT_IF"
ip link set "$BOT_IF" up

if [ "$ALLOW_INTERNET" = "1" ]; then
  # Unrestricted escape hatch, matching netns mode: no interception, no
  # allowlist, no MITM. Here it needs real NAT, because the workload's packets
  # now have to leave as themselves rather than terminate at the proxy.
  echo "gateway: ALLOW_INTERNET=1 — UNRESTRICTED egress (no allowlist, no MITM)."
  nft -f - <<EOF
flush ruleset
table inet omni_gw {
  chain forward { type filter hook forward priority filter; policy accept; }
}
table ip omni_gw_nat {
  chain postrouting { type nat hook postrouting priority srcnat; policy accept;
    oifname "$UPLINK_IF" masquerade }
}
EOF
  exit 0
fi

# One `inet` table, so IPv4 and IPv6 fail closed under the same rules instead
# of needing a separate ip6tables policy the way netns mode does.
nft -f - <<EOF
flush ruleset

table inet omni_gw {

  # The interception. A packet the workload sends to 443 arrives on the bot
  # link and has its destination rewritten to this gateway, where mitmproxy is
  # listening. After the rewrite it is a local connection, so it goes to
  # \`input\` and never reaches \`forward\`.
  #
  # \`iifname "$BOT_IF"\` is what makes this safe: mitmproxy's own traffic starts
  # locally and leaves by $UPLINK_IF, so it cannot match its own rule. That is
  # netns mode's uid exemption, done by topology instead.
  #
  # Port 80 is deliberately NOT redirected, exactly as in netns mode: on
  # cleartext the upstream is unauthenticated, so a workload that controls the
  # destination IP could point port 80 at an attacker, send \`Host: <allowed>\`,
  # and have the broker staple a real credential onto a cleartext request.
  # Unredirected, port 80 falls through to the forward drop below.
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    iifname "$BOT_IF" tcp dport 443 counter redirect to :$MITM_PORT
  }

  chain input {
    type filter hook input priority filter; policy drop;

    iif lo accept
    ct state established,related accept

    # What the workload may ask this gateway for: an address, a name, and the
    # intercepted TLS port. DNS is answered locally and forwarded to one
    # configured upstream, so there is no raw path to an arbitrary resolver —
    # the DNS-tunnel channel netns mode closes with its 127.0.0.11 rules.
    iifname "$BOT_IF" udp dport { 67, 53 } counter accept
    iifname "$BOT_IF" tcp dport { 53, $MITM_PORT } counter accept

    # The uplink offers no management surface. Control reaches this gateway out
    # of band (Incus API, docker exec), never across the network it filters.
    iifname "$UPLINK_IF" ct state new counter drop
  }

  # Nothing is forwarded, ever. Traffic that was not intercepted above dies
  # here, so a protocol nobody thought about fails closed instead of leaking
  # past the proxy.
  chain forward {
    type filter hook forward priority filter; policy drop;
    counter comment "the workload has no route past this gateway"
  }

  chain output {
    type filter hook output priority filter; policy accept;
  }

  # No masquerade chain on purpose. The workload's packets never leave as the
  # workload's packets — they terminate at mitmproxy, which opens its own
  # connection outward. NAT here would create a second, unfiltered path.
}
EOF

echo "gateway: router mode — $BOT_IF ($ADDR/$PREFIX) filtered, uplink $UPLINK_IF."
