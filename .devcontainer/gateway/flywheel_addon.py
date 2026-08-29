"""flywheel_addon.py — capture LLM traffic at the gateway (opt-in: `sandbox up --flywheel`).

Why here and not in the harness: the gateway already terminates TLS for every allowed
host, so ONE addon captures every model call from every harness against every backend —
Claude Code against Anthropic and pi against Ollama land in the same JSONL, in the same
shape. Nothing in the workload knows it is being recorded, and no harness needs a plugin.

This is the "observe" half of the flywheel (route -> observe -> evaluate -> specialize):
the corpus it produces is what you later distil a local model against, or replay as an
eval set. `sandbox flywheel stats|tail|export` reads it.

PRIVACY — read this before enabling. Unlike requests.log, which is redacted by
construction, these records contain full prompts and completions: your source code, file
contents, tool output, and any secret the agent happened to read. They are written to the
per-sandbox control dir on the HOST (never visible to the workload, never in git). Request
headers are NOT captured, so broker-injected credentials never land here — but a token
pasted into a prompt would. Treat the capture dir like the source tree it mirrors.
See docs/SANDBOX-PLAN.md §18.
"""

import json
import logging
import os
import time

from mitmproxy import http

ENABLED = os.environ.get("FLYWHEEL", "0") == "1"
CAPTURE_DIR = os.environ.get("FLYWHEEL_DIR", "/run/broker-control/flywheel")
# Per-file cap before rotation, and a total budget across the capture dir. Prompts are
# bulky; without a budget a long autonomous run quietly fills the host disk.
FILE_MAX = int(os.environ.get("FLYWHEEL_FILE_MAX", 128 * 1024 * 1024))
TOTAL_MAX = int(os.environ.get("FLYWHEEL_TOTAL_MAX", 2 * 1024 * 1024 * 1024))
# Individual bodies are truncated rather than dropped, so an outsized request still
# leaves a usable record of what was asked.
BODY_MAX = int(os.environ.get("FLYWHEEL_BODY_MAX", 4 * 1024 * 1024))

# Endpoints worth capturing: Anthropic Messages, OpenAI chat/responses, Ollama native.
LLM_PATHS = (
    "/v1/messages",
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/api/chat",
    "/api/generate",
)

log = logging.getLogger("flywheel")


def _truncate(text):
    if len(text) <= BODY_MAX:
        return text, False
    return text[:BODY_MAX], True


def _sse_events(text):
    """Yield parsed `data:` payloads from an SSE stream, skipping sentinels."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def _collect_anthropic_stream(events):
    """Assemble an Anthropic SSE stream into text, tool calls and usage."""
    text, tools, usage, stop, model = [], {}, {}, None, None
    for ev in events:
        kind = ev.get("type")
        if kind == "message_start":
            message = ev.get("message", {})
            model = message.get("model")
            usage.update(message.get("usage") or {})
        elif kind == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "tool_use":
                tools[ev.get("index")] = {"name": block.get("name"), "input": ""}
        elif kind == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                text.append(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                tool = tools.setdefault(ev.get("index"), {"name": None, "input": ""})
                tool["input"] += delta.get("partial_json", "")
        elif kind == "message_delta":
            usage.update(ev.get("usage") or {})
            stop = (ev.get("delta") or {}).get("stop_reason", stop)
    return {
        "text": "".join(text),
        "tool_calls": [tools[k] for k in sorted(tools)],
        "usage": usage,
        "stop_reason": stop,
        "model": model,
    }


def _collect_openai_stream(events):
    """Assemble an OpenAI chat-completions SSE stream."""
    text, tools, usage, stop, model = [], {}, {}, None, None
    for ev in events:
        model = ev.get("model", model)
        if ev.get("usage"):
            usage = ev["usage"]
        for choice in ev.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                tool = tools.setdefault(call.get("index", 0), {"name": None, "input": ""})
                fn = call.get("function") or {}
                if fn.get("name"):
                    tool["name"] = fn["name"]
                tool["input"] += fn.get("arguments") or ""
            stop = choice.get("finish_reason", stop)
    return {
        "text": "".join(text),
        "tool_calls": [tools[k] for k in sorted(tools)],
        "usage": usage,
        "stop_reason": stop,
        "model": model,
    }


def _collect_anthropic_json(body):
    text, tools = [], []
    for block in body.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tools.append({"name": block.get("name"), "input": json.dumps(block.get("input"))})
    return {
        "text": "".join(text),
        "tool_calls": tools,
        "usage": body.get("usage") or {},
        "stop_reason": body.get("stop_reason"),
        "model": body.get("model"),
    }


def _collect_openai_json(body):
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tools = [
        {
            "name": (call.get("function") or {}).get("name"),
            "input": (call.get("function") or {}).get("arguments", ""),
        }
        for call in message.get("tool_calls") or []
    ]
    return {
        "text": message.get("content") or "",
        "tool_calls": tools,
        "usage": body.get("usage") or {},
        "stop_reason": choice.get("finish_reason"),
        "model": body.get("model"),
    }


class Flywheel:
    def __init__(self):
        self.enabled = ENABLED
        self.dropped = 0

    def running(self):
        if not self.enabled:
            log.info("flywheel: disabled (FLYWHEEL != 1)")
            return
        try:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            os.chmod(CAPTURE_DIR, 0o777)  # same rationale as /run/broker-control
        except OSError as e:
            # WARNING, not ERROR, on purpose: mitmproxy's ErrorCheck addon aborts the whole
            # proxy if anything logs at ERROR during startup. An unusable capture dir must
            # degrade to "no captures", never take egress (and with it the sandbox) down.
            log.warning("flywheel: cannot create %s (%s) — capture disabled", CAPTURE_DIR, e)
            self.enabled = False
            return
        log.info("flywheel: capturing LLM traffic -> %s", CAPTURE_DIR)

    # ---- capture ---------------------------------------------------------
    def response(self, flow: http.HTTPFlow):
        if not self.enabled:
            return
        path = flow.request.path.split("?", 1)[0]
        if not any(path.endswith(p) for p in LLM_PATHS):
            return
        # A DENY/403 synthesised by the broker never reached a model — nothing to learn.
        if flow.response is None or flow.response.status_code >= 400:
            return
        try:
            self._record(flow, path)
        except Exception as e:  # noqa: BLE001 — capture must never break egress
            self.dropped += 1
            log.warning("flywheel: dropped a record (%s)", e)

    def _record(self, flow, path):
        req_text, req_truncated = _truncate(flow.request.get_text(strict=False) or "")
        try:
            request_body = json.loads(req_text)
        except ValueError:
            return  # not a JSON API call; nothing structured to learn from

        content_type = flow.response.headers.get("content-type", "")
        # mitmproxy streams bodies over stream_large_bodies (10m) — those arrive empty
        # here. The request alone is still worth keeping; mark it so exports can skip it.
        res_text, res_truncated = _truncate(flow.response.get_text(strict=False) or "")
        streamed = "event-stream" in content_type

        if streamed:
            events = list(_sse_events(res_text))
            anthropic_shaped = any("type" in e for e in events[:3])
            completion = (
                _collect_anthropic_stream(events)
                if anthropic_shaped
                else _collect_openai_stream(events)
            )
        else:
            try:
                body = json.loads(res_text) if res_text else {}
            except ValueError:
                body = {}
            completion = (
                _collect_anthropic_json(body)
                if "content" in body and "choices" not in body
                else _collect_openai_json(body)
            )

        started = flow.request.timestamp_start or 0
        ended = flow.response.timestamp_end or started
        record = {
            "ts": time.time(),
            "host": flow.request.pretty_host,
            "path": path,
            "model": request_body.get("model") or completion.get("model"),
            # The harness identifies itself in its UA (claude-cli/…, pi/…, omp/…), which
            # is how a mixed capture can later be split per harness. Only this one header
            # is kept: request headers can carry broker-injected credentials.
            "user_agent": flow.request.headers.get("user-agent", ""),
            "stream": streamed,
            "status": flow.response.status_code,
            "duration_ms": round((ended - started) * 1000, 1),
            "truncated": req_truncated or res_truncated,
            "empty_response": not res_text,
            "request": request_body,
            "completion": completion,
        }
        self._append(json.dumps(record, ensure_ascii=False))

    # ---- storage ---------------------------------------------------------
    def _path(self):
        return os.path.join(CAPTURE_DIR, time.strftime("capture-%Y%m%d.jsonl"))

    def _append(self, line):
        path = self._path()
        try:
            if os.path.exists(path) and os.path.getsize(path) > FILE_MAX:
                os.rename(path, f"{path}.{int(time.time())}")
                self._prune()
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("flywheel: write failed: %s", e)

    def _prune(self):
        """Keep the capture dir under TOTAL_MAX, oldest rotated file first."""
        try:
            files = sorted(
                (os.path.join(CAPTURE_DIR, f) for f in os.listdir(CAPTURE_DIR)),
                key=os.path.getmtime,
            )
            total = sum(os.path.getsize(f) for f in files)
            for f in files:
                if total <= TOTAL_MAX:
                    break
                size = os.path.getsize(f)
                os.remove(f)
                total -= size
                log.warning("flywheel: capture budget exceeded — removed %s", f)
        except OSError as e:
            log.warning("flywheel: prune failed: %s", e)


addons = [Flywheel()]
