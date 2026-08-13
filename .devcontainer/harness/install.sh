#!/usr/bin/env bash
# Build-time harness installer — invoked from devcontainer.Dockerfile, one RUN per
# harness so each gets its own cache layer and switching harness never triggers a
# rebuild. Runs during `docker build`, i.e. over the HOST network: the gateway does
# not exist yet, so none of these downloads touch the egress allowlist.
#
#   install.sh <harness>...      (no args = every known harness)
set -euo pipefail

HARNESS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HARNESS_LIB_DIR/lib.sh"

# `docker build` runs a non-login shell, where the image's npm-global bin dir may not be
# on PATH — which would make the post-install "is it on PATH?" check fail spuriously.
if command -v npm >/dev/null 2>&1; then
  PATH="$(npm config get prefix)/bin:$PATH"
  export PATH
fi

targets=("$@")
[ ${#targets[@]} -gt 0 ] || read -r -a targets <<< "$HARNESS_LIST"

for name in "${targets[@]}"; do
  ( harness_load "$name"
    echo "sandbox-harness: installing $H_NAME ..."
    h_install
    command -v "$H_BIN" >/dev/null || die "$H_NAME installed but '$H_BIN' is not on PATH"
    echo "sandbox-harness: $H_NAME -> $(command -v "$H_BIN")" )
done
