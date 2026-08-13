# shellcheck shell=bash
# pi — Mario Zechner's minimal harness (read/write/edit/bash and nothing else).
# Small system prompt, so it is the pick for local models with modest context.
H_NAME=pi
H_BIN=pi
H_DESC="pi — minimal harness, 4 tools, tiny system prompt (best fit for local models)"
H_HOME="$HOME/.pi"
H_HOME_ENV=SANDBOX_PI_HOME
H_SKILLS="$H_HOME/agent/skills"
H_HOST_HOME=pi-home
H_DOMAINS="pi.dev models.dev"

h_install() {
  # pi documents --ignore-scripts as safe: it needs no lifecycle scripts to install.
  npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION:-latest}"
}

h_configure() {
  # Teach the model that `websearch` exists (or withdraw it when search is off).
  sync_websearch_skill "$H_SKILLS"
  # Telemetry off, matching CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC for Claude Code —
  # no telemetry host is allowlisted, so leaving it on only produces DENY log noise.
  echo "PI_TELEMETRY=0"
  echo "DO_NOT_TRACK=1"
  case "$B_NAME" in
    anthropic)
      # pi requires a key to be *present* before it lists Anthropic models; the real
      # one is stapled on by the gateway (or use /login inside the sandbox).
      echo "ANTHROPIC_API_KEY=$B_KEY"
      ;;
    *)
      [ -n "$B_OPENAI_URL" ] || die "backend '$B_NAME' exposes no OpenAI-compatible URL"
      [ -n "${SANDBOX_MODEL:-}" ] || die "pi needs --model with backend '$B_NAME'"
      set_provider_model_args
      write_provider_config "$H_HOME/agent/models.json" json "$B_NAME" \
        "$B_OPENAI_URL" "$B_API" "$B_KEY" "${PROVIDER_ARGS[@]}"
      ;;
  esac
}

h_launch() {
  H_CMD=(pi)
  if [ "$B_NAME" != anthropic ]; then
    H_CMD+=(--provider "$B_NAME" --model "$(model_alias "$SANDBOX_MODEL")")
  elif [ -n "${SANDBOX_MODEL:-}" ]; then
    H_CMD+=(--model "$SANDBOX_MODEL")
  fi
}
