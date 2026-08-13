# Workload image — agent harnesses + toolchain. See docs/SANDBOX-PLAN.md §3.2, §17.
# UNVERIFIED scaffold.
FROM mcr.microsoft.com/devcontainers/javascript-node:22

# Harness versions. All four are baked in (one cache layer each) so switching
# `--harness` at runtime never triggers a rebuild and never needs egress.
ARG CLAUDE_CODE_VERSION=latest
ARG PI_VERSION=latest
ARG OMP_VERSION=latest
# omp's binary is a bun bundle — the harness installs bun alongside it.
ARG BUN_VERSION=latest
ARG PRIME_AGENT_VERSION=0.7.2
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

# --- Agent harnesses -----------------------------------------------------------
# The harness registry drives install, per-harness config and launch; the same files
# are read by the `sandbox` CLI on the host. See .devcontainer/harness/lib.sh.
COPY harness/ /usr/local/lib/sandbox-harness/
# `websearch` is one search command for every harness: omp has a native web_search tool,
# but pi and prime-agent ship none and Claude Code's is server-side (so it disappears
# with a local model). See docs/SANDBOX-PLAN.md §19.
RUN chmod +x /usr/local/lib/sandbox-harness/*.sh /usr/local/lib/sandbox-harness/websearch \
 && ln -sf /usr/local/lib/sandbox-harness/run.sh /usr/local/bin/sandbox-harness \
 && ln -sf /usr/local/lib/sandbox-harness/websearch /usr/local/bin/websearch

# One RUN each: an unrelated version bump only invalidates that harness's layer.
RUN /usr/local/lib/sandbox-harness/install.sh claudecode
RUN /usr/local/lib/sandbox-harness/install.sh pi
RUN /usr/local/lib/sandbox-harness/install.sh omp
RUN /usr/local/lib/sandbox-harness/install.sh prime-agent

# Organization policy for Claude Code (highest-precedence settings). Other harnesses
# have no equivalent — the container is their boundary. See managed-settings.json.
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
RUN mkdir -p /home/node/.claude /home/node/.pi /home/node/.omp /home/node/.prime \
             /workspace/node_modules /workspace/.venv /workspace/target /workspace/dist \
             /home/node/go /home/node/.local/share/containers \
 && chown -R node:node /home/node/.claude /home/node/.pi /home/node/.omp /home/node/.prime \
                      /workspace /home/node/go /home/node/.local \
 && chmod 700 /home/node/.claude /home/node/.pi /home/node/.omp /home/node/.prime

# TODO: to keep the IDE backend off the egress allowlist, pre-bake it here
#       (vscode-server / JetBrains backend). See docs/SANDBOX-PLAN.md §13.

USER node
