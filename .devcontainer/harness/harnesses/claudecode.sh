# shellcheck shell=bash
# Claude Code — the original harness this sandbox was built around.
H_NAME=claudecode
H_BIN=claude
H_DESC="Claude Code — Anthropic's CLI (tools, subagents, hooks, MCP)"
H_HOME="$HOME/.claude"             # in-workload path (auth, settings, agents/skills, sessions)
H_HOME_ENV=SANDBOX_CLAUDE_HOME     # compose variable that binds the shared host home here
H_SKILLS="$H_HOME/skills"          # usually a symlink to the read-only Minions mount
H_HOST_HOME=claude-home            # subdir under $SANDBOX_HOMES on the host
H_DOMAINS="api.anthropic.com console.anthropic.com claude.ai platform.claude.com"

h_install() {
  npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-latest}"
}

h_configure() {
  # Teach the model that `websearch` exists (or withdraw it when search is off).
  sync_websearch_skill "$H_SKILLS"
  if [ "$B_NAME" = anthropic ] && [ -z "${SANDBOX_LLM_URL:-}" ]; then
    # Stock path: OAuth session from the shared home, or a broker-injected API key.
    # Emit nothing so a stale ANTHROPIC_BASE_URL can never shadow it.
    return 0
  fi
  [ "${B_ANTHROPIC_COMPAT:-0}" = 1 ] || die \
    "claudecode speaks only the Anthropic Messages API, and backend '$B_NAME' does not
   advertise /v1/messages. Either point --llm-url at an endpoint that serves it and pass
   --llm-anthropic-compat, or run this backend under --harness pi|omp|prime-agent."
  echo "ANTHROPIC_BASE_URL=$B_ANTHROPIC_URL"
  echo "ANTHROPIC_AUTH_TOKEN=$B_KEY"
  if [ -n "${SANDBOX_MODEL:-}" ]; then
    # ANTHROPIC_MODEL sets the default; the DEFAULT_* triple keeps /model and any
    # explicit sonnet|opus request inside the sandbox pointed at the same local model.
    echo "ANTHROPIC_MODEL=$SANDBOX_MODEL"
    echo "ANTHROPIC_DEFAULT_SONNET_MODEL=$SANDBOX_MODEL"
    echo "ANTHROPIC_DEFAULT_OPUS_MODEL=$SANDBOX_MODEL"
  fi
  local small="${SANDBOX_SMALL_MODEL:-${SANDBOX_MODEL:-}}"
  if [ -n "$small" ]; then
    echo "ANTHROPIC_DEFAULT_HAIKU_MODEL=$small"
    echo "ANTHROPIC_SMALL_FAST_MODEL=$small"   # older releases read this name
  fi
  # Claude Code assumes 200k for a model it does not know, then auto-compacts against
  # that number. Tell it the real window (probed from the backend at `up`) so a 32k local
  # model is not fed 200k of context, and a 256k one is not compacted at a quarter full.
  if [ -n "${SANDBOX_MODEL_CONTEXT:-}" ]; then
    echo "CLAUDE_CODE_MAX_CONTEXT_TOKENS=$SANDBOX_MODEL_CONTEXT"
  fi
}

h_launch() {
  # The container IS the boundary (cap_drop:ALL, allowlisted egress, FUSE-filtered
  # workspace), so the in-harness prompt would only be theatre. Same rationale as
  # managed-settings.json's bypassPermissions. See SECURITY.md.
  H_CMD=(claude --dangerously-skip-permissions)
}
