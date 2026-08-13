#!/usr/bin/env python3
"""List (or pick) the models an LLM backend serves. Runs on the HOST, stdlib only.

Used by `sandbox models` and by `sandbox up` to choose a sensible default when you give
a local backend without --model. Tries Ollama's native /api/tags first (it reports
capabilities and on-disk size, which is what makes a good default pickable), then falls
back to the OpenAI-compatible /v1/models.

  probe_models.py URL            TSV: id, params, size(GB), capabilities
  probe_models.py URL --pick     just the id of the best tool-capable model
  probe_models.py URL --pick-small   smallest tool-capable model (for the fast/subagent slot)
  probe_models.py URL --context ID   that model's real context window, in tokens

Agentic harnesses are useless against a model that cannot call tools, so --pick only ever
returns a tool-capable one, and exits non-zero if the backend serves none.

--context matters more than it looks: every harness assumes a default window for a model
it does not recognise (128k for the pi family, 200k for Claude Code). Guess high and long
sessions fail or get silently truncated mid-tool-call; guess low and you waste the model.
"""

import json
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT = 15


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


def context_window(base, model_id):
    """Real context window from Ollama's /api/show, or None if it does not say."""
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    req = urllib.request.Request(
        f"{base}/api/show",
        data=json.dumps({"model": model_id}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        info = (json.load(resp).get("model_info") or {})
    # Key is architecture-prefixed, e.g. "qwen35moe.context_length".
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: probe_models.py URL [--pick|--pick-small|--context ID]")
    url, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")

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
