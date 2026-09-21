#!/bin/sh
# Router mode only: address and resolve for the workload on the private link.
#
# In netns mode the workload needs neither — it shares this namespace and uses
# Docker's embedded resolver. On a private link there is nothing else on the
# wire, so the gateway hands out the address, the default route and the DNS
# server itself. That keeps one generic workload image: it just asks for DHCP.
#
# Environment:
#   GW_BOT_IF         interface facing the workload   (default eth1)
#   GW_ADDR           this gateway on that link       (default 10.77.0.1)
#   GW_BOT_ADDR       the single address to hand out  (default 10.77.0.10)
#   GW_NETMASK        (default 255.255.255.0)
#   GW_DNS_UPSTREAM   where to forward queries; derived from the uplink if unset
#   GW_UPLINK_IF      (default eth0), used only to derive the upstream
set -eu

BOT_IF="${GW_BOT_IF:-eth1}"
ADDR="${GW_ADDR:-10.77.0.1}"
BOT_ADDR="${GW_BOT_ADDR:-10.77.0.10}"
NETMASK="${GW_NETMASK:-255.255.255.0}"
UPLINK_IF="${GW_UPLINK_IF:-eth0}"

command -v dnsmasq >/dev/null || {
  echo "gateway: FATAL — router mode needs dnsmasq and it is not installed." >&2
  exit 1
}

# dnsmasq must be told its upstream explicitly, and this is worth the words
# because the failure is so misleading. dnsmasq ignores any nameserver in
# /etc/resolv.conf that sits on one of its OWN interfaces, to avoid forwarding
# to itself. On a systemd host that file says 127.0.0.53 — exactly such an
# address. Left alone, dnsmasq comes up with no upstream at all and answers
# REFUSED to everything, which reads as a firewall fault and is not one.
UPSTREAM="${GW_DNS_UPSTREAM:-}"
if [ -z "$UPSTREAM" ] && command -v resolvectl >/dev/null 2>&1; then
  UPSTREAM=$(resolvectl status "$UPLINK_IF" 2>/dev/null \
    | sed -n 's/.*Current DNS Server: *//p' | head -1)
fi
if [ -z "$UPSTREAM" ]; then
  # Last resort: the first non-loopback nameserver the system knows about.
  UPSTREAM=$(sed -n 's/^nameserver *//p' /etc/resolv.conf 2>/dev/null \
    | grep -v '^127\.' | head -1)
fi
if [ -z "$UPSTREAM" ]; then
  echo "gateway: FATAL — no DNS upstream found; set GW_DNS_UPSTREAM." >&2
  exit 1
fi

mkdir -p /etc/dnsmasq.d
CONF=/etc/dnsmasq.d/omni-router.conf
cat > "$CONF" <<EOF
# Written by dnsmasq-router.sh. One workload per link, so the range is a
# single address and the lease is effectively a reservation.
interface=$BOT_IF
bind-interfaces
except-interface=$UPLINK_IF
dhcp-range=$BOT_ADDR,$BOT_ADDR,$NETMASK,12h
dhcp-option=3,$ADDR
dhcp-option=6,$ADDR

# See the comment in dnsmasq-router.sh: without these two lines dnsmasq has no
# upstream and answers REFUSED.
no-resolv
server=$UPSTREAM
EOF

echo "gateway: dnsmasq on $BOT_IF — offering $BOT_ADDR, router/DNS $ADDR, upstream $UPSTREAM."

# Load ONLY this file. Pointing dnsmasq at the packaged /etc/dnsmasq.conf does
# not work: on Debian its `conf-dir` line is commented out, so /etc/dnsmasq.d is
# never read, and dnsmasq comes up with no configuration at all. It then binds
# every interface on port 53 and collides with systemd-resolved
# ("failed to create listening socket for port 53: Address already in use"),
# which reads as a port conflict and is really a config that was never loaded.
#
# Foreground child of the entrypoint; the entrypoint waits on mitmproxy.
dnsmasq --keep-in-foreground --conf-file="$CONF" &
