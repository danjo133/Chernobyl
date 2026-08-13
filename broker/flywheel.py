#!/usr/bin/env python3
"""flywheel.py — read the LLM traffic captured by the gateway (`sandbox up --flywheel`).

Runs on the HOST against a sandbox's capture dir (.broker-control-<name>/flywheel/),
which is where the gateway addon writes. Driven by `./sandbox flywheel ...`; stdlib only.

  stats                     what was called, by model and harness, with token totals
  tail [-n N] [--full]      the most recent calls
  export --format F -o OUT  turn the capture into a training/eval set

The point of the capture is the "specialize" step of the flywheel: you run the work you
actually do against a frontier model, then export those traces to fine-tune or evaluate a
local model on exactly that distribution — instead of guessing which open model is "good
at coding". Nothing here uploads anything; export writes a local file.

Caveat worth knowing before you train on it: exports flatten structured content (tool
results, images) into text, and records whose response body was streamed past mitmproxy's
stream_large_bodies threshold have no completion at all — those are skipped, and counted.
"""

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict

UA_HARNESS = (("claude-cli", "claudecode"), ("omp", "omp"), ("prime-agent", "prime-agent"), ("pi", "pi"))


def harness_of(record):
    ua = (record.get("user_agent") or "").lower()
    for needle, name in UA_HARNESS:
        if needle in ua:
            return name
    return "unknown"


def tokens_of(record):
    usage = (record.get("completion") or {}).get("usage") or {}
    return (
        usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0,
        usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0,
    )


def load(capture_dir, since_hours=None, model=None, harness=None):
    files = sorted(glob.glob(os.path.join(capture_dir, "capture-*.jsonl*")))
    if not files:
        sys.exit(f"flywheel: no captures in {capture_dir} — was the sandbox started with --flywheel?")
    cutoff = time.time() - since_hours * 3600 if since_hours else None
    for path in files:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if cutoff and record.get("ts", 0) < cutoff:
                    continue
                if model and model not in (record.get("model") or ""):
                    continue
                if harness and harness_of(record) != harness:
                    continue
                yield record


def cmd_stats(args):
    groups = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "ms": 0.0, "empty": 0})
    first = last = None
    for record in load(args.dir, args.since, args.model, args.harness):
        key = (harness_of(record), record.get("model") or "?")
        g = groups[key]
        tin, tout = tokens_of(record)
        g["calls"] += 1
        g["in"] += tin
        g["out"] += tout
        g["ms"] += record.get("duration_ms") or 0
        g["empty"] += 1 if record.get("empty_response") else 0
        ts = record.get("ts", 0)
        first = ts if first is None else min(first, ts)
        last = ts if last is None else max(last, ts)
    if not groups:
        print("flywheel: nothing matched")
        return
    print(f"{'harness':<12} {'model':<34} {'calls':>6} {'tok in':>10} {'tok out':>9} {'avg s':>7}")
    for (harness, model), g in sorted(groups.items(), key=lambda kv: -kv[1]["calls"]):
        print(
            f"{harness:<12} {model[:34]:<34} {g['calls']:>6} {g['in']:>10} {g['out']:>9} "
            f"{g['ms'] / g['calls'] / 1000:>7.1f}"
        )
    span = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(first))} .. {time.strftime('%H:%M', time.localtime(last))}"
    empty = sum(g["empty"] for g in groups.values())
    print(f"\n{sum(g['calls'] for g in groups.values())} calls  {span}")
    if empty:
        print(f"{empty} call(s) have no captured response (streamed past mitmproxy's buffer) — exports skip these")


def cmd_tail(args):
    records = list(load(args.dir, args.since, args.model, args.harness))[-args.n :]
    for record in records:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.get("ts", 0)))
        completion = record.get("completion") or {}
        text = (completion.get("text") or "").replace("\n", " ")
        tools = ",".join(t.get("name") or "?" for t in completion.get("tool_calls") or [])
        tin, tout = tokens_of(record)
        print(
            f"{stamp} {harness_of(record):<11} {record.get('model') or '?':<28} "
            f"{tin:>7}->{tout:<6} {('[' + tools + '] ') if tools else ''}{text[:100]}"
        )
        if args.full:
            print(json.dumps(record, indent=2, ensure_ascii=False))


# --- export ----------------------------------------------------------------
def flatten_content(content):
    """Anthropic/OpenAI content (str or block list) -> plain text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            parts.append(f"[tool_result] {flatten_content(block.get('content'))}")
        elif block.get("type") == "tool_use":
            parts.append(f"[tool_use {block.get('name')}] {json.dumps(block.get('input'))}")
        elif block.get("type") in ("image", "image_url"):
            parts.append("[image]")
    return "\n".join(p for p in parts if p)


def to_messages(record):
    """Normalise a captured call into an OpenAI-style message list, or None."""
    request = record.get("request") or {}
    completion = record.get("completion") or {}
    answer = completion.get("text") or ""
    calls = completion.get("tool_calls") or []
    if not answer and not calls:
        return None  # nothing was learned from this record (streamed past the buffer)

    messages = []
    system = request.get("system")
    if system:
        messages.append({"role": "system", "content": flatten_content(system)})
    for message in request.get("messages") or []:
        messages.append(
            {"role": message.get("role", "user"), "content": flatten_content(message.get("content"))}
        )
    if calls:
        answer = (answer + "\n" if answer else "") + "\n".join(
            f"[tool_use {c.get('name')}] {c.get('input')}" for c in calls
        )
    messages.append({"role": "assistant", "content": answer})
    return messages


def cmd_export(args):
    written = skipped = 0
    out = open(args.out, "w") if args.out != "-" else sys.stdout
    try:
        for record in load(args.dir, args.since, args.model, args.harness):
            messages = to_messages(record)
            if messages is None or len(messages) < 2:
                skipped += 1
                continue
            if args.format == "openai":
                row = {"messages": messages}
                if (record.get("request") or {}).get("tools"):
                    row["tools"] = record["request"]["tools"]
            else:  # sharegpt
                role_map = {"system": "system", "user": "human", "assistant": "gpt"}
                row = {
                    "conversations": [
                        {"from": role_map.get(m["role"], m["role"]), "value": m["content"]}
                        for m in messages
                    ]
                }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
            if args.limit and written >= args.limit:
                break
    finally:
        if out is not sys.stdout:
            out.close()
    print(f"flywheel: wrote {written} example(s) to {args.out} ({skipped} skipped)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(prog="flywheel")
    ap.add_argument("--dir", required=True, help="capture dir (.broker-control-<name>/flywheel)")
    ap.add_argument("--since", type=float, metavar="HOURS", help="only calls newer than this")
    ap.add_argument("--model", help="substring filter on model id")
    ap.add_argument("--harness", help="claudecode|pi|omp|prime-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats").set_defaults(fn=cmd_stats)

    p_tail = sub.add_parser("tail")
    p_tail.add_argument("-n", type=int, default=20)
    p_tail.add_argument("--full", action="store_true", help="dump each matching record as JSON")
    p_tail.set_defaults(fn=cmd_tail)

    p_export = sub.add_parser("export")
    p_export.add_argument("--format", choices=("openai", "sharegpt"), default="openai")
    p_export.add_argument("-o", "--out", default="-")
    p_export.add_argument("--limit", type=int, default=0)
    p_export.set_defaults(fn=cmd_export)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
