# shellcheck shell=bash
# prime-agent — Prime Intellect's RLM harness: tools are function calls in a persistent
# IPython REPL, plus a "continual harness" (prompts/memories/skills) it rewrites itself.
H_NAME=prime-agent
H_BIN=prime-agent
H_DESC="prime-agent — RLM (IPython REPL) + self-refining continual harness"
H_HOME="$HOME/.prime"
H_HOME_ENV=SANDBOX_PRIME_HOME
H_SKILLS="$H_HOME/agent/skills"
H_HOST_HOME=prime-home
H_DOMAINS="app.primeintellect.ai api.primeintellect.ai models.dev pub-728493de92a943e2a9b2d17b4719f318.r2.dev"

# Not on the npm registry: releases are signed tarballs in an R2 bucket. This mirrors
# what install.sh does (fetch SHA256SUMS, verify, `npm install -g` the tarball) without
# piping a remote script into a shell. Checksums come from the same origin as the
# tarball, so they guard against a corrupt transfer, not against a hostile origin.
PRIME_AGENT_BASE_URL="${PRIME_AGENT_BASE_URL:-https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev}"

h_install() {
  local ver="${PRIME_AGENT_VERSION:-0.7.2}" tmp
  ver="${ver#v}"
  tmp="$(mktemp -d)"
  curl -fsSL "$PRIME_AGENT_BASE_URL/releases/v$ver/prime-agent-$ver.tgz" -o "$tmp/prime-agent.tgz"
  curl -fsSL "$PRIME_AGENT_BASE_URL/releases/v$ver/SHA256SUMS" -o "$tmp/SHA256SUMS"
  ( cd "$tmp" && grep " prime-agent-$ver.tgz\$" SHA256SUMS > selected \
    && sed -i "s| prime-agent-$ver.tgz| prime-agent.tgz|" selected \
    && sha256sum -c selected )
  # The bootstrap flags are the ones the official installer sets: they provision uv and
  # the IPython kernel the RLM runtime executes tools in.
  PRIME_AGENT_BOOTSTRAP_TOOLS_ON_INSTALL=1 \
  PRIME_AGENT_BOOTSTRAP_KERNEL_ON_INSTALL=1 \
  PRIME_AGENT_INSTALL_UV=1 \
    npm install -g --no-fund --no-audit "$tmp/prime-agent.tgz"
  rm -rf "$tmp"
}

h_configure() {
  # Teach the model that `websearch` exists (or withdraw it when search is off).
  sync_websearch_skill "$H_SKILLS"
  # Telemetry off, matching CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC for Claude Code:
  # prime-agent reports usage metrics by default, and no telemetry host is allowlisted
  # anyway — so leaving it on would only produce DENY noise in the egress log.
  echo "PRIME_AGENT_TELEMETRY=0"
  echo "DO_NOT_TRACK=1"
  case "$B_NAME" in
    anthropic)
      echo "ANTHROPIC_API_KEY=$B_KEY"
      ;;
    *)
      [ -n "$B_OPENAI_URL" ] || die "backend '$B_NAME' exposes no OpenAI-compatible URL"
      [ -n "${SANDBOX_MODEL:-}" ] || die "prime-agent needs --model with backend '$B_NAME'"
      set_provider_model_args
      write_provider_config "$H_HOME/agent/models.json" json "$B_NAME" \
        "$B_OPENAI_URL" "$B_API" "$B_KEY" "${PROVIDER_ARGS[@]}"
      ;;
  esac
}

h_launch() {
  H_CMD=(prime-agent)
  if [ "$B_NAME" != anthropic ]; then
    H_CMD+=(--provider "$B_NAME" --model "$(model_alias "$SANDBOX_MODEL")")
  elif [ -n "${SANDBOX_MODEL:-}" ]; then
    H_CMD+=(--model "$SANDBOX_MODEL")
  fi
}
