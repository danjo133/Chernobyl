# Gateway = firewall + credential broker. See docs/SANDBOX-PLAN.md §3.1.
# UNVERIFIED scaffold.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      iptables \
      redis-server \
      ca-certificates \
      python3 python3-pip python3-venv \
      jq curl netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

# mitmproxy + redis client for the addon. (Pin in a real build.)
RUN pip3 install --break-system-packages --no-cache-dir mitmproxy redis

# Unprivileged identity that mitmproxy + redis run as. iptables owner-matching keys off
# this uid: only the proxy may egress freely; the workload (different uid, shared netns) cannot.
RUN useradd --create-home --shell /usr/sbin/nologin proxy \
 && mkdir -p /opt/broker /etc/broker /var/lib/broker /ca-pub /run/broker \
 && chown proxy:proxy /var/lib/broker /run/broker

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# broker_addon.py / allowed-domains.txt / scopes.yaml are bind-mounted by compose
# (so you can edit + hot-reload without rebuilding).

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
