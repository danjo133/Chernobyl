# LiteLLM gateway

One host-local gateway (`127.0.0.1:4000`) that fronts your coding clients and routes
each request to **Anthropic (API key)**, **OpenRouter**, or your **local 5090 model**.
It speaks both wire formats — Anthropic `/v1/messages` *and* OpenAI
`/v1/chat/completions` — so Claude Code, Codex, Copilot, Antigravity, and Pi can all
share it.

> **Your Claude Max subscription does not flow through here.** LiteLLM can't ride the
> subscription OAuth — anything it sends to Anthropic is **API-key billed**. So keep
> Claude Code on `/login` talking **directly** to `api.anthropic.com` for normal use,
> and point clients at this gateway only for OpenRouter / local / programmatic paths.

## Quick start

```sh
cd tooling/litellm
cp env.example .env        # then fill in keys (see below)
# Pin the image: edit compose.yaml -> a verified >=1.83.0 tag (NOT the 1.82.7/1.82.8
# pip wheels — those were malware; the Docker image was unaffected).
docker compose up -d
curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Keys go in `.env`: `ANTHROPIC_API_KEY` (optional, API-billed Claude), `OPENROUTER_API_KEY`,
and `LITELLM_MASTER_KEY` (the token your clients present to the gateway — your invention,
not a provider secret).

## Wiring each client

The gateway exposes both APIs on the same port. Point Anthropic-style clients at the
root; OpenAI-style clients at `/v1`. The "API key" each client sends is your
`LITELLM_MASTER_KEY`. Pick a backend by requesting the matching **alias** from
`config.yaml` (`claude-opus`, `or-glm`, `local-coder`, …).

| Client | Env / setting |
|---|---|
| **Claude Code — subscription (default)** | *Nothing.* Stay on `/login`, no `ANTHROPIC_BASE_URL`. This keeps the Max flat rate. |
| **Claude Code — via gateway** (2nd profile, for local/cheap) | `ANTHROPIC_BASE_URL=http://127.0.0.1:4000` · `ANTHROPIC_AUTH_TOKEN=$LITELLM_MASTER_KEY` · `ANTHROPIC_MODEL=local-coder` (and `ANTHROPIC_DEFAULT_HAIKU_MODEL=local-ollama` for the background model) |
| **Codex / OpenAI-style CLIs** | `OPENAI_BASE_URL=http://127.0.0.1:4000/v1` · `OPENAI_API_KEY=$LITELLM_MASTER_KEY` · model = an alias |
| **Copilot / Antigravity / Pi** | In each tool's "custom/OpenAI-compatible endpoint" setting: base `http://127.0.0.1:4000/v1`, key `$LITELLM_MASTER_KEY`, model = an alias |

> Two Claude Codes coexist cleanly: run the gateway profile with a separate config dir,
> e.g. `CLAUDE_CONFIG_DIR=~/.claude-litellm ANTHROPIC_BASE_URL=… claude`. The default
> `claude` stays on your subscription.

## Local model on the 5090

LiteLLM points at whatever you run on the host (`host.docker.internal` from the
container). Register both in `config.yaml` and switch by alias.

### vLLM — recommended for the agentic workload
Best concurrency (parallel subagents) and tool-call fidelity. One model per instance.

```sh
# Needs recent vLLM + CUDA 12.8+ for Blackwell (sm_120). 30-32B at 4-bit (~18-20GB)
# leaves headroom for KV cache on 32GB; FP8 fits ~14-24B comfortably.
vllm serve Qwen/Qwen2.5-Coder-32B-Instruct-AWQ \
  --served-model-name qwen-coder \
  --quantization awq_marlin \
  --max-model-len 32768 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --port 8000
```
`--served-model-name qwen-coder` must match the `hosted_vllm/qwen-coder` entry in
`config.yaml`. `--enable-auto-tool-choice` is what makes tool-calling work for agents.

### Ollama — for fast model-shopping
Hot-swaps models with zero ceremony; weaker under concurrency. Good for trying
Qwen3-Coder / Nemotron / GLM before committing one to vLLM.

```sh
ollama pull qwen2.5-coder:32b
# already mapped as `local-ollama` in config.yaml
```

## What's NOT here yet (Phase 2)

Letting the **contained sandbox** consume this gateway — its leg routed through
mitmproxy → Caido, with one narrow audited allowance for the host gateway. Tracked
separately; this host gateway works standalone in the meantime.
