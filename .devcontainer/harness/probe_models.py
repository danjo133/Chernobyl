#!/usr/bin/env python3
"""List (or pick) the models an LLM backend serves. Runs on the HOST, stdlib only.

Used by `sandbox models` and by `sandbox up` to choose a sensible default when you give
a local backend without --model. Tries Ollama's native /api/tags first (it reports
capabilities and on-disk size, which is what makes a good default pickable), then falls
back to the OpenAI-compatible /v1/models.

  probe_models.py URL            TSV: id, params, size(GB), capabilities
  probe_models.py URL --pick     just the id of the best tool-capable model
  probe_models.py URL --pick-small   smallest tool-capable model (for the fast/subagent slot)
  probe_models.py URL --context ID   that model's advertised context window, in tokens
  probe_models.py URL --input ID     its input modalities ("text" / "text,image")
  probe_models.py URL --served-context ID   the window the server is ENFORCING for a
                                 model it already has loaded (Ollama /api/ps)
  probe_models.py URL --effective-context ID [--probe-tokens N]
                                 what the SERVER actually accepts (see below)

Agentic harnesses are useless against a model that cannot call tools, so --pick only ever
returns a tool-capable one, and exits non-zero if the backend serves none.

--context matters more than it looks: every harness assumes a default window for a model
it does not recognise (128k for the pi family, 200k for Claude Code). Guess high and long
sessions fail or get silently truncated mid-tool-call; guess low and you waste the model.

--effective-context exists because --context can be a lie. /api/show reports the model's
TRAINED maximum (262144 for qwen3.5:35b), while the server serves whatever its own num_ctx
says — Ollama's default is a few thousand tokens. When a prompt exceeds that, it is
silently truncated FROM THE FRONT, and the front is where tool definitions live: the model
then sees style instructions and no tools, and narrates tool calls as prose or invented
XML instead of emitting them. No error is raised anywhere. This probe sends a filler
prompt of a known size and reads back how many tokens the server says it evaluated
(`usage.prompt_tokens`), so the mismatch is caught at `sandbox up` rather than diagnosed
from a confusing transcript hours later.
"""

import json
import ssl
import sys
import urllib.error
import urllib.request

import os

TIMEOUT = 15
# Prefill of the probe prompt is real work for the server, so the timeout is generous and
# separate from the metadata timeout above. A box whose KV cache has spilled out of VRAM
# can take minutes for even a small prompt — raise this rather than concluding it is down.
PROBE_TIMEOUT = int(os.environ.get("SANDBOX_PROBE_TIMEOUT", "180"))
# Big enough to catch the common misconfigurations (2k/4k defaults), small enough that
# the prefill costs seconds rather than minutes. Override with --probe-tokens.
PROBE_TOKENS_DEFAULT = 6000


def fetch(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        return json.load(resp)


def models(base):
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        data = fetch(f"{base}/api/tags")
        out = []
        for m in data.get("models") or []:
            details = m.get("details") or {}
            out.append(
                {
                    "id": m.get("name") or m.get("model"),
                    "params": details.get("parameter_size", "?"),
                    "size": m.get("size", 0),
                    "caps": m.get("capabilities") or [],
                }
            )
        if out:
            return out
    except (urllib.error.URLError, ValueError, OSError):
        pass
    data = fetch(f"{base}/v1/models")
    return [
        {"id": m.get("id"), "params": "?", "size": 0, "caps": []}
        for m in (data.get("data") or [])
        if m.get("id")
    ]


def api_root(base):
    base = base.rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def filler_prompt(tokens):
    """~`tokens` tokens of unique, boring text.

    Unique so nothing along the path can dedupe or cache it away, and boring so the model
    has nothing interesting to do with it — we only care how much of it comes back
    counted. ~4 characters per token is close enough for a tripwire.
    """
    lines, n = [], 0
    i = 0
    while n < tokens * 4:
        line = f"line {i:06d}: the quick brown fox jumps over the lazy dog near the river bank\n"
        lines.append(line)
        n += len(line)
        i += 1
    return "".join(lines)


def effective_context(base, model_id, probe_tokens):
    """(sent, counted) — how many prompt tokens the server says it evaluated.

    Uses the OpenAI-compatible endpoint so this works for any backend that reports usage,
    not just Ollama. `counted` well below `sent` means the server truncated the prompt.
    """
    prompt = filler_prompt(probe_tokens)
    body = {
        "model": model_id,
        "stream": False,
        "max_tokens": 1,  # generation is irrelevant; we are measuring the prefill
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        f"{api_root(base)}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
        data = json.load(resp)
    usage = data.get("usage") or {}
    counted = usage.get("prompt_tokens")
    if not isinstance(counted, int):
        raise ValueError("backend did not report usage.prompt_tokens")
    # Our 4-chars-per-token estimate is rough; the server's own count of an untruncated
    # prompt is the honest measure of what we sent.
    return len(prompt) // 4, counted


def show(base, model_id):
    """Ollama's /api/show for one model."""
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    req = urllib.request.Request(
        f"{base}/api/show",
        data=json.dumps({"model": model_id}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


# Ollama capability -> the modality name the pi-family `input:` list uses. Text is implied
# for every chat model and always leads; anything unmapped is dropped rather than guessed.
CAP_INPUT = {"vision": "image", "audio": "audio"}


def input_modalities(base, model_id):
    """Input modalities for a model, or None if the backend does not say.

    A harness that is not told a model accepts images treats it as text-only — omp then
    refuses vision-dependent work (its snapcompact summariser is the visible one) and
    silently falls back. Ollama knows the answer; it just calls it `capabilities`.
    """
    caps = show(base, model_id).get("capabilities")
    if not caps:
        return None
    return ["text"] + [CAP_INPUT[c] for c in caps if c in CAP_INPUT]


def served_context(base, model_id):
    """The window Ollama is ACTUALLY serving for a loaded model, or None.

    /api/ps reports `context_length` per loaded instance — the number the server will
    enforce, as opposed to the trained maximum /api/show advertises. Exact and instant,
    where --effective-context can only bracket it by prefilling a prompt of known size
    (and so only catches a wall below whatever --probe-tokens was). Only loaded models
    appear here, so call this AFTER something has loaded the model.
    """
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    for m in (fetch(f"{base}/api/ps").get("models") or []):
        if model_id in (m.get("model"), m.get("name")):
            value = m.get("context_length")
            return value if isinstance(value, int) and value > 0 else None
    return None


def context_window(base, model_id):
    """Real context window from Ollama's /api/show, or None if it does not say."""
    info = show(base, model_id).get("model_info") or {}
    # Key is architecture-prefixed, e.g. "qwen35moe.context_length".
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit(
            "usage: probe_models.py URL [--pick|--pick-small|--context ID|--input ID"
            "|--served-context ID|--effective-context ID [--probe-tokens N]]"
        )
    url, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")

    if mode == "--effective-context":
        # stdout: the token count the server actually evaluated.
        # exit 0 = no truncation up to the probe size; 3 = TRUNCATED (stdout is the cap);
        # 1 = could not measure (unreachable, or no usage reported).
        if len(sys.argv) < 4:
            sys.exit("usage: probe_models.py URL --effective-context MODEL_ID [--probe-tokens N]")
        probe_tokens = PROBE_TOKENS_DEFAULT
        if "--probe-tokens" in sys.argv:
            try:
                probe_tokens = int(sys.argv[sys.argv.index("--probe-tokens") + 1])
            except (IndexError, ValueError):
                sys.exit("--probe-tokens needs an integer")
        try:
            sent, counted = effective_context(url, sys.argv[3], probe_tokens)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            print(f"sandbox: could not measure the served context window ({exc})", file=sys.stderr)
            sys.exit(1)
        print(counted)
        # A little slack: tokenisers disagree with a 4-chars-per-token estimate, and chat
        # templates add a few tokens of their own. Only a real shortfall is truncation.
        if counted < sent * 0.8:
            print(
                f"sandbox: sent ~{sent} tokens, the server evaluated only {counted} — it is "
                "TRUNCATING prompts",
                file=sys.stderr,
            )
            sys.exit(3)
        sys.exit(0)

    if mode == "--served-context":
        # stdout: the window the server is enforcing for this (loaded) model.
        # exit 1 = it does not say / the model is not loaded.
        if len(sys.argv) < 4:
            sys.exit("usage: probe_models.py URL --served-context MODEL_ID")
        try:
            window = served_context(url, sys.argv[3])
        except (urllib.error.URLError, ValueError, OSError):
            window = None
        if window is None:
            sys.exit(1)
        print(window)
        return

    if mode == "--input":
        # stdout: comma-separated input modalities ("text,image"). exit 1 = unknown, and
        # the caller omits the field rather than asserting a wrong answer.
        if len(sys.argv) < 4:
            sys.exit("usage: probe_models.py URL --input MODEL_ID")
        try:
            mods = input_modalities(url, sys.argv[3])
        except (urllib.error.URLError, ValueError, OSError):
            mods = None
        if not mods:
            sys.exit(1)
        print(",".join(mods))
        return

    if mode == "--context":
        if len(sys.argv) < 4:
            sys.exit("usage: probe_models.py URL --context MODEL_ID")
        try:
            window = context_window(url, sys.argv[3])
        except (urllib.error.URLError, ValueError, OSError):
            window = None
        if window is None:
            sys.exit(1)  # caller falls back to the harness default
        print(window)
        return

    try:
        found = models(url)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        sys.exit(f"sandbox: cannot reach {url} ({exc})")
    if not found:
        sys.exit(f"sandbox: {url} reports no models")

    if mode in ("--pick", "--pick-small"):
        # Unknown capabilities (a non-Ollama OpenAI endpoint) can't be filtered on —
        # assume tools rather than refusing to pick anything at all.
        usable = [m for m in found if not m["caps"] or "tools" in m["caps"]]
        if not usable:
            sys.exit(f"sandbox: no tool-capable model at {url} — an agent harness needs one")
        usable.sort(key=lambda m: m["size"], reverse=(mode == "--pick"))
        print(usable[0]["id"])
        return

    for m in sorted(found, key=lambda m: -m["size"]):
        gb = f"{m['size'] / 1e9:.1f}G" if m["size"] else "?"
        tools = "tools" if (not m["caps"] or "tools" in m["caps"]) else "NO-TOOLS"
        print(f"{m['id']}\t{m['params']}\t{gb}\t{tools}\t{','.join(m['caps'])}")


if __name__ == "__main__":
    main()
