# shellcheck shell=bash
# omp (oh-my-pi) — Can Bölük's maximalist fork of pi: LSP, DAP debugger, hashline edits,
# 30-odd built-in tools. The heavyweight of the three pi-family harnesses.
H_NAME=omp
H_BIN=omp
H_DESC="omp (oh-my-pi) — pi fork with LSP/DAP, subagents, 30+ tools"
H_HOME="$HOME/.omp"
H_HOME_ENV=SANDBOX_OMP_HOME
H_SKILLS="$H_HOME/agent/skills"
H_HOST_HOME=omp-home
H_DOMAINS="omp.sh models.dev"

h_install() {
  # omp's published binary is a bun-compiled bundle (`#!/usr/bin/env bun`,
  # engines.bun >= 1.3.14) — npm installs it but it will not RUN without bun. Install
  # bun from npm rather than bun.sh's shell installer: same artifact, no curl-into-shell,
  # and it lands on the PATH the image already has.
  command -v bun >/dev/null 2>&1 || npm install -g "bun@${BUN_VERSION:-latest}"
  # Ships prebuilt N-API natives per platform; lifecycle scripts stay enabled so the
  # right @oh-my-pi/pi-natives-<platform> optional dep is linked.
  npm install -g "@oh-my-pi/pi-coding-agent@${OMP_VERSION:-latest}"
}

h_configure() {
  # Teach the model that `websearch` exists (or withdraw it when search is off).
  sync_websearch_skill "$H_SKILLS"
  # Telemetry off (pi lineage, so it honours PI_TELEMETRY; DO_NOT_TRACK is the
  # cross-tool convention). No telemetry host is allowlisted either way.
  echo "PI_TELEMETRY=0"
  # omp is the one harness with a built-in web_search tool, and SearXNG is one of
  # its 23 providers — point it at the same endpoint the websearch command uses.
  if [ -n "${SANDBOX_SEARCH_URL:-}" ]; then
    echo "SEARXNG_ENDPOINT=$SANDBOX_SEARCH_URL"
  fi
  echo "DO_NOT_TRACK=1"
  case "$B_NAME" in
    anthropic)
      echo "ANTHROPIC_API_KEY=$B_KEY"
      ;;
    *)
      [ -n "$B_OPENAI_URL" ] || die "backend '$B_NAME' exposes no OpenAI-compatible URL"
      [ -n "${SANDBOX_MODEL:-}" ] || die "omp needs --model with backend '$B_NAME'"
      set_provider_model_args
      write_provider_config "$H_HOME/agent/models.yml" yaml "$B_NAME" \
        "$B_OPENAI_URL" "$B_API" "$B_KEY" "${PROVIDER_ARGS[@]}"
      ;;
  esac
}

h_launch() {
  # omp's --model is a fuzzy match that accepts the "provider/id" form; --provider is
  # legacy. Qualifying with the provider avoids matching a same-named catalog entry.
  H_CMD=(omp)
  if [ "$B_NAME" != anthropic ]; then
    H_CMD+=(--model "$B_NAME/$(model_alias "$SANDBOX_MODEL")")
    if [ -n "${SANDBOX_SMALL_MODEL:-}" ]; then
      H_CMD+=(--smol "$B_NAME/$(model_alias "$SANDBOX_SMALL_MODEL")")
    fi
  elif [ -n "${SANDBOX_MODEL:-}" ]; then
    H_CMD+=(--model "$SANDBOX_MODEL")
  fi
}
