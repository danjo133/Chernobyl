#!/usr/bin/env bash
# Configure the devcontainer to drive the bb-hunter sidecar over SSH. Two passes,
# each writing only what its uid owns (root in the workload is powerless to write
# node-owned paths — same constraint as trust-ca.sh). The sandbox CLI runs both.
#   root -> the `hunter` wrapper on PATH (loads the sidecar's CA env per command)
#   node -> the `hunter` ssh alias + key perms
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
  # ---- root pass: PATH wrapper so `hunter <cmd>` runs in the sidecar with CA env.
  cat > /usr/local/bin/hunter <<'W'
#!/bin/bash
# Run a tool in the bb-hunter sidecar as `hunter`, with the gateway-CA env loaded.
# printf %q quotes each arg so it survives the remote shell verbatim.
exec ssh -q hunter ". ~/.hunter-env 2>/dev/null; exec $(printf '%q ' "$@")"
W
  chmod 0755 /usr/local/bin/hunter
  echo "tools-client[root]: 'hunter' wrapper installed (use: hunter nuclei -u https://...)."
else
  # ---- node pass: ssh alias 'hunter' -> loopback dropbear, key from the ro mount.
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  port="${TOOLS_SSH_PORT:-2222}"
  # Replace any prior managed block, keep the rest of the user's config.
  tmp="$(mktemp)"
  sed '/# >>> sandbox tools >>>/,/# <<< sandbox tools <<</d' "$HOME/.ssh/config" 2>/dev/null > "$tmp" || true
  cat >> "$tmp" <<CFG
# >>> sandbox tools >>>
Host hunter
  HostName 127.0.0.1
  Port $port
  User hunter
  IdentityFile /home/node/.ssh-hunter/id_hunter
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
# <<< sandbox tools <<<
CFG
  install -m 600 "$tmp" "$HOME/.ssh/config"; rm -f "$tmp"
  command -v ssh >/dev/null 2>&1 || \
    echo "tools-client[node]: WARNING — no ssh client; add 'openssh-client' to devcontainer.Dockerfile." >&2
  echo "tools-client[node]: ssh alias 'hunter' configured."
fi
