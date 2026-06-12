#!/usr/bin/env bash
# Trust the gateway's MITM CA inside the workload, and point the toolchain at it.
#
# The workload has no-new-privileges + cap_drop:ALL, so root is deliberately
# powerless: no setuid (`sudo`/`su` fail — no CAP_SETUID/SETGID) and no
# CAP_DAC_OVERRIDE (root cannot write node-owned /home/node). So this runs in
# TWO passes, each writing only what its own uid owns:
#   - as root  -> the system trust store (/usr/local/share/ca-certificates).
#   - as node  -> node's NODE_EXTRA_CA_CERTS cert + npm/git config.
# The sandbox CLI invokes both; pass is auto-selected by uid. See SANDBOX-PLAN §12.
set -euo pipefail

SRC=/ca/mitmproxy-ca-cert.pem          # public cert only (ca-pub volume), populated by the gateway

if [ ! -f "$SRC" ]; then
  echo "trust-ca: $SRC not found — is the gateway healthy and the ca-pub volume mounted?" >&2
  exit 1
fi

if [ "$(id -u)" = "0" ]; then
  # ---- root pass: system trust store (covers curl/pip/go/cargo via OS certs) ----
  cp "$SRC" /usr/local/share/ca-certificates/mitmproxy.crt
  update-ca-certificates >/dev/null 2>&1 || update-ca-certificates >/dev/null
  echo "trust-ca[root]: system trust store updated."
else
  # ---- node pass: cert for NODE_EXTRA_CA_CERTS + per-tool config ----
  DEST_DIR="$HOME/.config/sandbox"     # matches NODE_EXTRA_CA_CERTS in the Dockerfile
  mkdir -p "$DEST_DIR"
  cp "$SRC" "$DEST_DIR/mitmproxy-ca-cert.pem"
  npm config set cafile "$DEST_DIR/mitmproxy-ca-cert.pem" || true
  git config --global http.sslCAInfo "$DEST_DIR/mitmproxy-ca-cert.pem" || true
  echo "trust-ca[$(id -un)]: NODE_EXTRA_CA_CERTS + npm/git CA configured."
fi
