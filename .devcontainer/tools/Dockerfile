# Sandbox-integration layer over the standalone bb-hunter image (tooling/docker/).
# Adds ONLY a rootless SSH endpoint so Claude — in the sibling devcontainer, same
# gateway netns — can drive the tools over loopback. The hunting toolset itself
# lives in bb-hunter and is untouched. See docs/SANDBOX-PLAN.md §16.
#
# Why dropbear (not openssh-server): the sidecar runs cap_drop:ALL +
# no-new-privileges, like the workload, so OpenSSH sshd's setuid privilege
# separation cannot work. dropbear run AS `hunter`, serving only `hunter`, needs
# no setuid and no caps — and therefore cannot setuid to the proxy uid to bypass
# the gateway firewall.
ARG HUNTER_IMAGE=bb-hunter:latest
FROM ${HUNTER_IMAGE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends dropbear-bin \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/tools-entrypoint.sh
RUN chmod +x /usr/local/bin/tools-entrypoint.sh

USER hunter
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/tools-entrypoint.sh"]
