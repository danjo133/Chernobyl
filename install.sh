#!/usr/bin/env bash
#
# install.sh — install the agent sandbox on this machine.
#
# Usage:
#   ./install.sh                  install for the current user into ~/.local  (no root)
#   ./install.sh --system         install into /usr/local for every user      (uses sudo)
#   ./install.sh --prefix DIR     install into DIR
#   ./install.sh --check          only report on prerequisites, change nothing
#   ./install.sh --uninstall      remove the install (your sandboxes/state are kept)
#
# The install tree is read-only at runtime. Each user's sandboxes, allowlists, broker
# state and harness logins live under their own $SANDBOX_STATE_DIR (default
# ~/.local/state/agent-sandbox) and $HOME/.sandbox, so a single system-wide install is
# safely shared: users never write into it and never see each other's sandboxes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
MODE=install
SUDO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --system)     PREFIX=/usr/local; shift;;
    --prefix)     PREFIX="${2:?--prefix needs a directory}"; shift 2;;
    --check)      MODE=check; shift;;
    --uninstall)  MODE=uninstall; shift;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0;;
    *)            echo "install.sh: unknown argument: $1 (try --help)" >&2; exit 2;;
  esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }

MISSING=0

# --- prerequisites -----------------------------------------------------------
# Split by consequence: a missing REQUIRED tool means the sandbox cannot come up at
# all; a missing optional one only disables a feature, so it warns rather than fails.
check_prereqs() {
  echo "Required:"

  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      if docker info -f '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
        ok "docker (rootless — the supported configuration)"
      else
        ok "docker (rootful)"
        warn "  rootful docker needs '-allow-other' on the FUSE mount and"
        warn "  user_allow_other in /etc/fuse.conf; mount.sh detects this automatically."
      fi
    else
      bad "docker is installed but the daemon is not reachable (try: systemctl --user start docker)"
      MISSING=1
    fi
  else
    bad "docker — required; the sandbox is a compose project"
    MISSING=1
  fi

  if docker compose version >/dev/null 2>&1; then
    ok "docker compose (v2)"
  else
    bad "docker compose v2 — required ('docker-compose' v1 will not work: it ignores deploy.resources)"
    MISSING=1
  fi

  if command -v python3 >/dev/null 2>&1; then
    ok "python3 ($(python3 -V 2>&1 | cut -d' ' -f2)) — broker UI, flywheel, credential filler"
  else
    bad "python3 — required by the broker UI and the credential filler"
    MISSING=1
  fi

  if command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1; then
    ok "fusermount — required to mount the gitignore-filtered repo view"
  else
    bad "fusermount/fusermount3 — required (package: fuse3)"
    MISSING=1
  fi

  # Go is required only to BUILD the filter. A finished install ships the binary.
  if command -v go >/dev/null 2>&1; then
    ok "go ($(go version | awk '{print $3}')) — builds the FUSE filter"
  elif [ -x "$HERE/.devcontainer/fusefilter/gitignore-fuse" ]; then
    ok "gitignore-fuse already built (go not needed)"
  elif command -v nix >/dev/null 2>&1; then
    ok "nix — will supply go to build the FUSE filter"
  else
    bad "go (or nix) — required to build the FUSE filter"
    MISSING=1
  fi

  echo "Optional:"
  command -v git >/dev/null 2>&1 \
    && ok "git — 'sandbox up --worktree'" \
    || warn "git missing — 'sandbox up --worktree' unavailable"
  { command -v yq >/dev/null 2>&1 || python3 -c 'import yaml' 2>/dev/null; } \
    && ok "yq or PyYAML — 'sandbox up --policy'" \
    || warn "no yq / PyYAML — 'sandbox up --policy' unavailable (pip install pyyaml)"
  command -v mountpoint >/dev/null 2>&1 \
    && ok "mountpoint — mount readiness check" \
    || warn "mountpoint missing (package: util-linux) — mount.sh cannot confirm the mount"
}

echo "Agent Sandbox — checking host prerequisites"
echo
check_prereqs
echo

if [ "$MODE" = check ]; then
  [ "$MISSING" = 0 ] && echo "All required prerequisites present." \
                     || echo "Missing required prerequisites (see ✗ above)." >&2
  exit "$MISSING"
fi

# Writing outside $HOME needs root. Re-exec just the make step under sudo rather than
# the whole script, so the prerequisite checks still reflect the invoking user.
case "$PREFIX" in
  "$HOME"/*) ;;
  *) if [ "$(id -u)" -ne 0 ]; then
       command -v sudo >/dev/null 2>&1 || { echo "install.sh: $PREFIX needs root and sudo is not available" >&2; exit 1; }
       SUDO=sudo
     fi;;
esac

if [ "$MODE" = uninstall ]; then
  $SUDO make -C "$HERE" uninstall PREFIX="$PREFIX"
  exit 0
fi

[ "$MISSING" = 0 ] || { echo "install.sh: refusing to install with required prerequisites missing." >&2; exit 1; }

# Build as the invoking user (Go needs a writable cache; root often has none), then
# install. With sudo the build is already done, so `make install` only copies.
make -C "$HERE" build
$SUDO make -C "$HERE" install PREFIX="$PREFIX"

STATE="${SANDBOX_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-sandbox}"
cat <<EOF

Done. Try it:

  sandbox up --source ~/dev/somerepo    # build + start a sandbox for a repo
  sandbox open                          # interactive agent session inside it
  sandbox ls                            # what is running, ports, broker UI password
  sandbox down                          # tear it down

Your state (sandboxes, allowlists, broker):  $STATE
Your harness logins, shared by all sandboxes: $HOME/.sandbox/homes
Nothing is written into the install tree, so it can be shared or made read-only.
EOF
