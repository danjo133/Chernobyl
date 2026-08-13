# shellcheck shell=bash
# Ollama backend. Serves BOTH an OpenAI-compatible surface (/v1/chat/completions, used
# by the pi-family harnesses) and — since Ollama 0.12 — an Anthropic-compatible one
# (/v1/messages), which is what lets Claude Code talk to it with no translating proxy.
#
# No API key: Ollama ignores it, but every harness insists on *some* credential being
# present before it will offer the models, hence the placeholder.
B_NAME=ollama
# Default is the conventional local daemon. For a box on your network, export
# SANDBOX_OLLAMA_URL=https://ollama.example.com once (or pass --llm-url per sandbox) —
# deliberately not hardcoded, so this repo carries no one's infrastructure hostnames.
# Use https for a remote host: the gateway does not redirect cleartext port 80.
B_URL="${SANDBOX_LLM_URL:-${SANDBOX_OLLAMA_URL:-http://localhost:11434}}"
B_URL="${B_URL%/}"
B_OPENAI_URL="$B_URL/v1"
B_ANTHROPIC_URL="$B_URL"
B_ANTHROPIC_COMPAT=1
B_API=openai-completions
B_KEY="${SANDBOX_LLM_API_KEY:-ollama}"
B_DOMAINS="$(url_host "$B_URL")"
