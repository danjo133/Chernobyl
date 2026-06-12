# Workload image — Claude Code + toolchain. See docs/SANDBOX-PLAN.md §3.2.
# UNVERIFIED scaffold.
FROM mcr.microsoft.com/devcontainers/javascript-node:20

ARG CLAUDE_CODE_VERSION=latest

USER root

# Base tools. Add your project's toolchain here (or layer a project-specific image on top).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates jq ripgrep python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

# Pin Claude Code for reproducibility; auto-update is disabled via env below.
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# Organization policy (highest-precedence settings). See managed-settings.json.
RUN mkdir -p /etc/claude-code
COPY managed-settings.json /etc/claude-code/managed-settings.json

# CA trust helper (run at postCreate, once the gateway has generated its CA).
COPY trust-ca.sh /usr/local/bin/trust-ca.sh
RUN chmod +x /usr/local/bin/trust-ca.sh

ENV CLAUDE_CONFIG_DIR=/home/node/.claude \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    NODE_EXTRA_CA_CERTS=/home/node/.config/sandbox/mitmproxy-ca-cert.pem \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# TODO: to keep the IDE backend off the egress allowlist, pre-bake it here
#       (vscode-server / JetBrains backend). See docs/SANDBOX-PLAN.md §13.

USER node
