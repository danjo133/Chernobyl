# Gateway = firewall + credential broker. See docs/SANDBOX-PLAN.md §3.1.
# UNVERIFIED scaffold.
FROM debian:bookworm-slim

# nftables + dnsmasq are for GATEWAY_MODE=router, where the workload is a
# separate machine on a private link: nft programs prerouting/forward, dnsmasq
# hands the workload its address and resolves for it. Neither is used in the
# default netns mode, and neither runs unless that mode is selected.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      iptables \
      nftables \
      dnsmasq \
      redis-server \
      ca-certificates \
      openssl \
      python3 python3-pip python3-venv \
      jq curl netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

# mitmproxy + redis client for the addon. redis-py 6.x fails to connect over
# unix sockets ("maintenance notifications" needs a host attribute) — stay on 5.x.
RUN pip3 install --break-system-packages --no-cache-dir mitmproxy "redis<6"

# Unprivileged identity that mitmproxy + redis run as. iptables owner-matching keys off
# this uid: only the proxy may egress freely; the workload (different uid, shared netns) cannot.
# (Named `mitm` — Debian already ships a system user called `proxy`, uid 13.)
#
# CRITICAL: the uid MUST NOT collide with the workload's uid. The workload shares this
# netns, and iptables owner-match keys on the kernel-global numeric uid — if they match,
# the workload's egress hits the proxy's RETURN exemption and bypasses the firewall
# entirely. The devcontainers base runs `node` as uid 1000, so pin mitm well clear of it.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 4242 mitm \
 && mkdir -p /opt/broker /etc/broker /var/lib/broker /ca-pub /run/broker \
 && chown mitm:mitm /var/lib/broker /run/broker

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY firewall-netns.sh /usr/local/bin/firewall-netns.sh
COPY firewall-router.sh /usr/local/bin/firewall-router.sh
COPY dnsmasq-router.sh /usr/local/bin/dnsmasq-router.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
             /usr/local/bin/firewall-netns.sh \
             /usr/local/bin/firewall-router.sh \
             /usr/local/bin/dnsmasq-router.sh

# dnsmasq's package starts it as a service; router mode runs it itself from the
# entrypoint, so keep the packaged unit out of the way.
RUN rm -f /etc/dnsmasq.d/README 2>/dev/null || true

# broker_addon.py / allowed-domains.txt / scopes.yaml are bind-mounted by compose
# (so you can edit + hot-reload without rebuilding).

# Unbuffered so mitmdump + addon logs reach `docker logs` immediately.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
