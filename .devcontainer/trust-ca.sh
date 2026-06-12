#!/usr/bin/env bash
# Trust the gateway's MITM CA inside the workload, and point the toolchain at it.
# Runs at postCreate (as `node`, which has passwordless sudo on the devcontainers base).
# UNVERIFIED scaffold. See docs/SANDBOX-PLAN.md §12.
set -euo pipefail

SRC=/ca/mitmproxy-ca-cert.pem          # public cert only (ca-pub volume), populated by the gateway
DEST_DIR="$HOME/.config/sandbox"

if [ ! -f "$SRC" ]; then
  echo "trust-ca: $SRC not found — is the gateway healthy and the ca-pub volume mounted?" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST_DIR/mitmproxy-ca-cert.pem"

# System trust store (covers cargo, go, curl, and anything using OS certs).
sudo cp "$SRC" /usr/local/share/ca-certificates/mitmproxy.crt
sudo update-ca-certificates

# Per-tool config for the ones that don't read the OS store by default.
npm config set cafile "$DEST_DIR/mitmproxy-ca-cert.pem" || true
git config --global http.sslCAInfo "$DEST_DIR/mitmproxy-ca-cert.pem" || true

echo "trust-ca: MITM CA installed. Toolchain now trusts the gateway."
