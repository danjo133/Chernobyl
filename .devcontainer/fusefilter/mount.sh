#!/usr/bin/env bash
# Mount the gitignore-filtered view of SANDBOX_SOURCE at SANDBOX_MOUNT, idempotently,
# and persist SANDBOX_MOUNT into the sandbox's env file so compose picks it up.
# Invoked by devcontainer.json initializeCommand (VS Code) and by the `sandbox` CLI.
# See docs/SANDBOX-PLAN.md §3.3, §13.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DC_DIR="$(cd "$HERE/.." && pwd)"

# Where the CLI keeps writable state. The install dir ($HERE) may be read-only and
# shared between users, so nothing below may write into it.
STATE="${SANDBOX_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-sandbox}"
# The per-sandbox env file. The `sandbox` CLI passes it; the VS Code
# initializeCommand path has no CLI, so fall back to the legacy in-repo location.
ENV_FILE="${SANDBOX_ENV_FILE:-$DC_DIR/.env}"

# Source the env file if present (the CLI writes it; VS Code path may rely on defaults).
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a || true

# Defaults: expose the repo that contains .devcontainer, mount under the user runtime dir.
SANDBOX_SOURCE="${SANDBOX_SOURCE:-$(cd "$DC_DIR/.." && pwd)}"
SANDBOX_NAME="${SANDBOX_NAME:-cc-sandbox}"
# The user runtime dir only exists for real login sessions — a user reached via
# su/sudo, a service account or cron has no /run/user/<uid>, and /run/user is
# root-owned so it cannot be created. Fall back to state, which is always writable.
runtime_root() {
  local d="${XDG_RUNTIME_DIR:-}"
  [ -n "$d" ] && [ -d "$d" ] && [ -w "$d" ] && { echo "$d"; return; }
  d="/run/user/$(id -u)"
  [ -d "$d" ] && [ -w "$d" ] && { echo "$d"; return; }
  echo "$STATE/run"
}
SANDBOX_MOUNT="${SANDBOX_MOUNT:-$(runtime_root)/devfilter/$SANDBOX_NAME}"

# Prefer a binary shipped by `make install`, then one built earlier into state, then
# build it. Building lands in $STATE/bin when $HERE is not writable (shared install).
BIN=""
for cand in "$HERE/gitignore-fuse" "$STATE/bin/gitignore-fuse"; do
  [ -x "$cand" ] && { BIN="$cand"; break; }
done
if [ -z "$BIN" ]; then
  if [ -w "$HERE" ]; then BIN="$HERE/gitignore-fuse"; else BIN="$STATE/bin/gitignore-fuse"; fi
  mkdir -p "$(dirname "$BIN")"
  echo "mount.sh: building gitignore-fuse -> $BIN"
  # CGO_ENABLED=0: go-fuse is pure Go; keep the binary static (and buildable without gcc).
  if command -v go >/dev/null; then
    ( cd "$HERE" && CGO_ENABLED=0 go build -o "$BIN" . )
  elif command -v nix >/dev/null; then
    ( cd "$HERE" && nix shell nixpkgs#go --command env CGO_ENABLED=0 go build -o "$BIN" . )
  else
    echo "mount.sh: need go (or nix) to build gitignore-fuse, or install it with 'make install'" >&2; exit 1
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
mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"
grep -v '^SANDBOX_MOUNT=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
echo "SANDBOX_MOUNT=$SANDBOX_MOUNT" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
echo "mount.sh: ready ($SANDBOX_MOUNT)"
