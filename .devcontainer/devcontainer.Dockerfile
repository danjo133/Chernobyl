# Workload image — Claude Code + toolchain. See docs/SANDBOX-PLAN.md §3.2.
# UNVERIFIED scaffold.
FROM mcr.microsoft.com/devcontainers/javascript-node:20

ARG CLAUDE_CODE_VERSION=latest
# Bump as needed. GOTOOLCHAIN=local (below) pins builds to exactly this version.
ARG GO_VERSION=1.23.4

USER root

# Base tools + toolchains:
#   build-essential / make  — C toolchain + task running (cgo, native deps, Makefiles)
#   python3 venv/dev + pip  — run Python; venvs live in the .venv build volume
#   podman / buildah        — DAEMONLESS, rootless OCI image builds (see below)
#   slirp4netns             — rootless build networking (egress still via the gateway)
#   uidmap                  — ships newuidmap/newgidmap; inert here (single-UID mode)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl jq ripgrep make build-essential \
      python3 python3-pip python3-venv python3-dev \
      podman buildah slirp4netns uidmap \
 && rm -rf /var/lib/apt/lists/*

# --- Go toolchain (official tarball; apt's is stale) ---------------------------
# Installed at build time over the host network (the gateway isn't up yet during
# `compose build`). Runtime `go build`/`go mod download` egress via the gateway, so
# proxy.golang.org + sum.golang.org must be allowlisted — see gateway/allowed-domains.txt.
RUN arch="$(dpkg --print-architecture)" \
 && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz" -o /tmp/go.tgz \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz

# --- Rootless image builds (Podman/Buildah) ------------------------------------
# Daemonless and unprivileged: no Docker socket, no DinD, no extra caps. The
# workload keeps cap_drop:ALL + no-new-privileges. Builds run in a single-UID user
# namespace with vfs storage; compose.yaml grants seccomp=unconfined so the kernel
# permits the unshare(CLONE_NEWUSER) that rootless builds require. Config below.
COPY containers/storage.conf containers/containers.conf containers/registries.conf /etc/containers/

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
# Go env. GOTOOLCHAIN=local stops `go` from auto-fetching a different toolchain
# version over egress. GOPATH/GOCACHE live under /home/node/go, backed by the
# build-go volume so module + build caches persist across rebuilds.
ENV GOPATH=/home/node/go \
    GOCACHE=/home/node/go/.cache \
    GOTOOLCHAIN=local
# Put Go, its bin dir, and Minions CLI tools (minions-scan/minions-query) on PATH.
# Separate ENV so the base image's existing $PATH is expanded and preserved.
ENV PATH=/usr/local/go/bin:/home/node/go/bin:/home/node/dev/Minions/tools/bin:${PATH}

# Pre-create the volume mountpoints owned by node. Docker copies a path's ownership
# into a FRESH named volume on first mount, so node owns its config + build dirs
# without an init step or CAP_CHOWN. (Caveat: only applies to brand-new volumes — an
# already-root-owned volume from a prior run must be removed or chowned once.)
RUN mkdir -p /home/node/.claude \
             /workspace/node_modules /workspace/.venv /workspace/target /workspace/dist \
             /home/node/go /home/node/.local/share/containers \
 && chown -R node:node /home/node/.claude /workspace /home/node/go /home/node/.local \
 && chmod 700 /home/node/.claude

# TODO: to keep the IDE backend off the egress allowlist, pre-bake it here
#       (vscode-server / JetBrains backend). See docs/SANDBOX-PLAN.md §13.

USER node
