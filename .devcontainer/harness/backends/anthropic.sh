# shellcheck shell=bash
# Anthropic backend — the default, and the one the credential broker was built for.
#
# The workload never holds the real key. Claude Code uses the OAuth session in the
# shared home ($SANDBOX_CLAUDE_HOME); the pi-family harnesses want an ANTHROPIC_API_KEY
# to exist before they will list Anthropic models, so they get the placeholder below
# and the gateway swaps in the real credential by host (broker: `api.anthropic.com`).
# Injection overwrites whatever header the workload sent, so the placeholder never
# reaches the API. Alternatively run the harness's own /login inside the sandbox.
B_NAME=anthropic
B_URL="${SANDBOX_LLM_URL:-https://api.anthropic.com}"
B_URL="${B_URL%/}"
B_OPENAI_URL=""            # no OpenAI-compatible surface
B_ANTHROPIC_URL="$B_URL"
B_ANTHROPIC_COMPAT=1
B_API=anthropic-messages
B_KEY="${SANDBOX_LLM_API_KEY:-sandbox-broker-injects-this}"
B_DOMAINS="$(url_host "$B_URL")"
