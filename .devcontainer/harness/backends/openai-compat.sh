# shellcheck shell=bash
# Generic OpenAI-compatible endpoint — llama.cpp, vLLM, LM Studio, OpenRouter, LiteLLM,
# a Switchyard/NIM router, or a cloud provider. Requires --llm-url.
#
# Claude Code speaks only the Anthropic Messages API, so it can use this backend ONLY if
# the endpoint also serves /v1/messages. That is not implied by OpenAI compatibility, so
# it is opt-in: pass --llm-anthropic-compat (or SANDBOX_LLM_ANTHROPIC=1) to assert it.
B_NAME=openai-compat
B_URL="${SANDBOX_LLM_URL:?--llm-url is required with --llm-backend openai-compat}"
B_URL="${B_URL%/}"
# Accept a URL given either with or without the /v1 suffix.
case "$B_URL" in
  */v1) B_OPENAI_URL="$B_URL"; B_ANTHROPIC_URL="${B_URL%/v1}" ;;
  *)    B_OPENAI_URL="$B_URL/v1"; B_ANTHROPIC_URL="$B_URL" ;;
esac
B_ANTHROPIC_COMPAT="${SANDBOX_LLM_ANTHROPIC:-0}"
B_API=openai-completions
B_KEY="${SANDBOX_LLM_API_KEY:-sandbox-broker-injects-this}"
B_DOMAINS="$(url_host "$B_URL")"
