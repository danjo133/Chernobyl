#!/usr/bin/env bash
# sandbox-harness — configure and launch the selected agent INSIDE the workload.
# Installed to /usr/local/bin/sandbox-harness; driven by `./sandbox open` on the host.
#
#   sandbox-harness list              installed harnesses, one per line: name<TAB>bin<TAB>version
#   sandbox-harness info              what the current selection resolves to
#   sandbox-harness config            write provider config only (no launch)
#   sandbox-harness exec [-- ARGS]    config, then exec the harness (ARGS are passed on)
#
# Selection comes from the environment, injected per `docker compose exec -e` so it can
# change between sessions without recreating the container:
#   SANDBOX_HARNESS  SANDBOX_LLM_BACKEND  SANDBOX_LLM_URL  SANDBOX_LLM_API_KEY
#   SANDBOX_MODEL    SANDBOX_SMALL_MODEL  SANDBOX_LLM_ANTHROPIC
set -euo pipefail

HARNESS_LIB_DIR="${HARNESS_LIB_DIR:-/usr/local/lib/sandbox-harness}"
# shellcheck source=lib.sh
. "$HARNESS_LIB_DIR/lib.sh"

cmd="${1:-exec}"; shift || true
if [ "${1:-}" = "--" ]; then shift; fi

case "$cmd" in
  list)
    for name in $HARNESS_LIST; do
      ( harness_load "$name"
        command -v "$H_BIN" >/dev/null 2>&1 || exit 0
        ver="$(timeout 20 "$H_BIN" --version 2>/dev/null | head -1 || true)"
        printf '%s\t%s\t%s\t%s\n' "$H_NAME" "$H_BIN" "${ver:-?}" "$H_DESC" )
    done
    exit 0
    ;;
  info|config|exec) ;;
  *) die "unknown command '$cmd' (list|info|config|exec)" ;;
esac

harness_load "${SANDBOX_HARNESS:-$HARNESS_DEFAULT}"
backend_load "${SANDBOX_LLM_BACKEND:-$BACKEND_DEFAULT}"
command -v "$H_BIN" >/dev/null 2>&1 || die "$H_NAME is not installed in this image ('$H_BIN' not on PATH)"

if [ "$cmd" = info ]; then
  printf 'harness : %s (%s)\nbackend : %s\nurl     : %s\nmodel   : %s%s\nhome    : %s\n' \
    "$H_NAME" "$H_BIN" "$B_NAME" "$B_URL" "${SANDBOX_MODEL:-<harness default>}" \
    "${SANDBOX_SMALL_MODEL:+  (small: $SANDBOX_SMALL_MODEL)}" "$H_HOME"
  exit 0
fi

# h_configure writes any provider config and prints KEY=VALUE lines for the launch env.
# Values are exported literally — never eval'd — so a model id or URL cannot smuggle in
# shell syntax.
env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
h_configure > "$env_file"

if [ "$cmd" = config ]; then
  cat "$env_file"
  exit 0
fi

while IFS= read -r line; do
  [ -n "$line" ] || continue
  export "$line"
done < "$env_file"

h_launch
echo "sandbox: $H_NAME via $B_NAME${SANDBOX_MODEL:+ ($SANDBOX_MODEL)} -> ${H_CMD[*]}" >&2
exec "${H_CMD[@]}" "$@"
