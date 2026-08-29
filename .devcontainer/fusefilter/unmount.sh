#!/usr/bin/env bash
# Unmount a gitignore-fuse view. Called by `sandbox down` (there is no host-side
# teardown hook in the devcontainer spec, so VS Code won't run this automatically).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DC_DIR="$(cd "$HERE/.." && pwd)"
STATE="${SANDBOX_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-sandbox}"
ENV_FILE="${SANDBOX_ENV_FILE:-$DC_DIR/.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a || true

SANDBOX_NAME="${SANDBOX_NAME:-cc-sandbox}"
# Same runtime-dir fallback as mount.sh: no login session means no /run/user/<uid>.
runtime_root() {
  local d="${XDG_RUNTIME_DIR:-}"
  [ -n "$d" ] && [ -d "$d" ] && [ -w "$d" ] && { echo "$d"; return; }
  d="/run/user/$(id -u)"
  [ -d "$d" ] && [ -w "$d" ] && { echo "$d"; return; }
  echo "$STATE/run"
}
# Precedence: the path `sandbox down` passes (empty if never recorded), then the env
# file, then the default. ${1:-X} covers both unset and empty.
SANDBOX_MOUNT="${1:-${SANDBOX_MOUNT:-$(runtime_root)/devfilter/$SANDBOX_NAME}}"

if mountpoint -q "$SANDBOX_MOUNT"; then
  fusermount3 -u "$SANDBOX_MOUNT" 2>/dev/null || fusermount -u "$SANDBOX_MOUNT"
  echo "unmount.sh: unmounted $SANDBOX_MOUNT"
else
  echo "unmount.sh: $SANDBOX_MOUNT not mounted"
fi
