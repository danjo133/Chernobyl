#!/usr/bin/env bash
# Mount the gitignore-filtered view of SANDBOX_SOURCE at SANDBOX_MOUNT, idempotently,
# and persist SANDBOX_MOUNT into .devcontainer/.env so compose picks it up.
# Invoked by devcontainer.json initializeCommand (VS Code) and by the `sandbox` CLI.
# UNVERIFIED scaffold. See docs/SANDBOX-PLAN.md §3.3, §13.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DC_DIR="$(cd "$HERE/.." && pwd)"

# Source the env file if present (the CLI writes it; VS Code path may rely on defaults).
[ -f "$DC_DIR/.env" ] && set -a && . "$DC_DIR/.env" && set +a || true

# Defaults: expose the repo that contains .devcontainer, mount under the user runtime dir.
SANDBOX_SOURCE="${SANDBOX_SOURCE:-$(cd "$DC_DIR/.." && pwd)}"
SANDBOX_NAME="${SANDBOX_NAME:-cc-sandbox}"
SANDBOX_MOUNT="${SANDBOX_MOUNT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/devfilter/$SANDBOX_NAME}"

BIN="$HERE/gitignore-fuse"
if [ ! -x "$BIN" ]; then
  echo "mount.sh: building gitignore-fuse..."
  if command -v go >/dev/null; then
    ( cd "$HERE" && go build -o "$BIN" . )
  elif command -v nix >/dev/null; then
    ( cd "$HERE" && nix shell nixpkgs#go --command go build -o "$BIN" . )
  else
    echo "mount.sh: need go (or nix) to build gitignore-fuse" >&2; exit 1
  fi
fi

mkdir -p "$SANDBOX_MOUNT"

# Idempotent: if already a live mountpoint, do nothing.
if mountpoint -q "$SANDBOX_MOUNT"; then
  echo "mount.sh: $SANDBOX_MOUNT already mounted."
else
  # Rootless docker: no -allow-other needed. Rootful docker: add -allow-other and set
  # user_allow_other in /etc/fuse.conf (programs.fuse.userAllowOther on NixOS).
  # Auto-detect unless DOCKER_ROOTFUL is set explicitly.
  if [ -z "${DOCKER_ROOTFUL:-}" ]; then
    if docker info -f '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
      DOCKER_ROOTFUL=0
    else
      DOCKER_ROOTFUL=1
    fi
  fi
  ALLOW_OTHER_FLAG=""
  [ "$DOCKER_ROOTFUL" = "1" ] && ALLOW_OTHER_FLAG="-allow-other"
  echo "mount.sh: mounting $SANDBOX_SOURCE -> $SANDBOX_MOUNT"
  "$BIN" -source "$SANDBOX_SOURCE" -mount "$SANDBOX_MOUNT" ${ALLOW_OTHER_FLAG} &
  # wait for the mount to come live
  for _ in $(seq 1 50); do mountpoint -q "$SANDBOX_MOUNT" && break; sleep 0.1; done
  mountpoint -q "$SANDBOX_MOUNT" || { echo "mount.sh: mount failed" >&2; exit 1; }
fi

# Persist the resolved mount path for compose.
touch "$DC_DIR/.env"
grep -v '^SANDBOX_MOUNT=' "$DC_DIR/.env" > "$DC_DIR/.env.tmp" || true
echo "SANDBOX_MOUNT=$SANDBOX_MOUNT" >> "$DC_DIR/.env.tmp"
mv "$DC_DIR/.env.tmp" "$DC_DIR/.env"
echo "mount.sh: ready ($SANDBOX_MOUNT)"
