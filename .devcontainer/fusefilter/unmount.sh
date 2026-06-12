#!/usr/bin/env bash
# Unmount a gitignore-fuse view. Called by `sandbox down` (there is no host-side
# teardown hook in the devcontainer spec, so VS Code won't run this automatically).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DC_DIR="$(cd "$HERE/.." && pwd)"
[ -f "$DC_DIR/.env" ] && set -a && . "$DC_DIR/.env" && set +a || true

SANDBOX_NAME="${SANDBOX_NAME:-cc-sandbox}"
SANDBOX_MOUNT="${1:-${SANDBOX_MOUNT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/devfilter/$SANDBOX_NAME}}"

if mountpoint -q "$SANDBOX_MOUNT"; then
  fusermount3 -u "$SANDBOX_MOUNT" 2>/dev/null || fusermount -u "$SANDBOX_MOUNT"
  echo "unmount.sh: unmounted $SANDBOX_MOUNT"
else
  echo "unmount.sh: $SANDBOX_MOUNT not mounted"
fi
