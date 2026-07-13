#!/bin/bash
# bb-hunter sidecar entrypoint — runs as the unprivileged `hunter` user (no root,
# no caps). Brings up a loopback-only dropbear so the devcontainer can
# `ssh hunter <cmd>`, and prepares a CA bundle trusting the gateway MITM for any
# inspected (non-passthrough) host. See docs/SANDBOX-PLAN.md §16.
set -eu

SSH_PORT="${TOOLS_SSH_PORT:-2222}"
SSHDIR="$HOME/.ssh"
mkdir -p "$SSHDIR"; chmod 700 "$SSHDIR"

# Authorized key: Claude's public key, generated + injected by the sandbox CLI,
# mounted read-only at /etc/hunter-ssh. Copy it where dropbear looks for it.
if [ -f /etc/hunter-ssh/authorized_keys ]; then
  install -m 600 /etc/hunter-ssh/authorized_keys "$SSHDIR/authorized_keys"
else
  echo "tools: WARNING — no authorized_keys mounted; SSH login will fail." >&2
fi

# Per-container dropbear host key (loopback only; host identity churn is harmless).
HOSTKEY="$SSHDIR/dropbear_ed25519"
[ -f "$HOSTKEY" ] || dropbearkey -t ed25519 -f "$HOSTKEY" >/dev/null 2>&1

# Trust the gateway MITM CA for outbound TLS, via env vars only (no root needed).
# Hosts you mark passthrough (`!host`) use real TLS and don't need this; inspected
# hosts (default allowlist tier) do. `hunter` wrapper + login shells source it.
if [ -f /ca/mitmproxy-ca-cert.pem ]; then
  mkdir -p "$HOME/.config"
  BUNDLE="$HOME/.config/ca-bundle.crt"
  cat /etc/ssl/certs/ca-certificates.crt /ca/mitmproxy-ca-cert.pem > "$BUNDLE" 2>/dev/null \
    || cp /ca/mitmproxy-ca-cert.pem "$BUNDLE"
  cat > "$HOME/.hunter-env" <<EOF
export SSL_CERT_FILE=$BUNDLE
export CURL_CA_BUNDLE=$BUNDLE
export REQUESTS_CA_BUNDLE=$BUNDLE
export GIT_SSL_CAINFO=$BUNDLE
export NODE_EXTRA_CA_CERTS=/ca/mitmproxy-ca-cert.pem
EOF
  grep -q hunter-env "$HOME/.bashrc" 2>/dev/null || echo '[ -f ~/.hunter-env ] && . ~/.hunter-env' >> "$HOME/.bashrc"
fi

echo "tools: dropbear on 127.0.0.1:$SSH_PORT (user hunter). Drive via 'ssh hunter' / the 'hunter' wrapper."
# -F foreground, -E log->stderr (docker logs), -s key-only (no passwords),
# -j -k no port forwarding, -p bind loopback only.
exec dropbear -F -E -s -j -k -p 127.0.0.1:"$SSH_PORT" -r "$HOSTKEY"
