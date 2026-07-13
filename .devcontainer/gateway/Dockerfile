# Gateway = firewall + credential broker. See docs/SANDBOX-PLAN.md §3.1.
# UNVERIFIED scaffold.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      iptables \
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
RUN chmod +x /usr/local/bin/entrypoint.sh

# broker_addon.py / allowed-domains.txt / scopes.yaml are bind-mounted by compose
# (so you can edit + hot-reload without rebuilding).

# Unbuffered so mitmdump + addon logs reach `docker logs` immediately.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
