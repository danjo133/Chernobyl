#!/usr/bin/env python3
"""Tests for the flywheel capture addon: python3 flywheel_addon_test.py

Stubs `mitmproxy` so this runs anywhere (the addon only needs the module for a type
hint). What is worth testing here is the stream assembly — an SSE transcript is the
normal case for every harness, and a silent mis-parse would poison the whole corpus.
"""

import json
import sys
import types
import unittest
from unittest import mock

# --- stub mitmproxy before importing the addon -------------------------------
mitmproxy = types.ModuleType("mitmproxy")
http_mod = types.ModuleType("mitmproxy.http")
http_mod.HTTPFlow = object
mitmproxy.http = http_mod
sys.modules.setdefault("mitmproxy", mitmproxy)
sys.modules.setdefault("mitmproxy.http", http_mod)

import flywheel_addon as fw  # noqa: E402


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


class FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


class FakeMsg:
    def __init__(self, text, headers=None):
        self._text = text
        self.headers = FakeHeaders(headers or {})

    def get_text(self, strict=True):
        return self._text


class FakeRequest(FakeMsg):
    def __init__(self, text, path="/v1/messages", host="api.anthropic.com", headers=None):
        super().__init__(text, headers)
        self.path = path
        self.pretty_host = host
        self.timestamp_start = 1000.0


class FakeResponse(FakeMsg):
    def __init__(self, text, headers=None, status=200):
        super().__init__(text, headers)
        self.status_code = status
        self.timestamp_end = 1001.5


class FakeFlow:
    def __init__(self, request, response):
        self.request = request
        self.response = response


class StreamAssembly(unittest.TestCase):
    def test_anthropic_stream(self):
        events = [
            {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 11}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hel"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "name": "bash"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        ]
        got = fw._collect_anthropic_stream(fw._sse_events(sse(*events)))
        self.assertEqual(got["text"], "hello")
        self.assertEqual(got["tool_calls"], [{"name": "bash", "input": '{"cmd":"ls"}'}])
        self.assertEqual(got["stop_reason"], "tool_use")
        self.assertEqual(got["usage"], {"input_tokens": 11, "output_tokens": 7})

    def test_openai_stream(self):
        events = [
            {"model": "qwen", "choices": [{"delta": {"content": "a"}}]},
            {"model": "qwen", "choices": [{"delta": {"content": "b"}}]},
            {"model": "qwen", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "read", "arguments": '{"p":1}'}}]}}]},
            {"model": "qwen", "choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        ]
        got = fw._collect_openai_stream(fw._sse_events(sse(*events)))
        self.assertEqual(got["text"], "ab")
        self.assertEqual(got["tool_calls"], [{"name": "read", "input": '{"p":1}'}])
        self.assertEqual(got["usage"]["completion_tokens"], 2)

    def test_malformed_sse_lines_are_skipped(self):
        text = 'data: {"broken\n\ndata: {"type":"x"}\n\n: comment\n\n'
        self.assertEqual(list(fw._sse_events(text)), [{"type": "x"}])

    def test_non_stream_json_shapes(self):
        anthropic = fw._collect_anthropic_json(
            {"model": "m", "content": [{"type": "text", "text": "hi"}],
             "usage": {"input_tokens": 1, "output_tokens": 2}, "stop_reason": "end_turn"}
        )
        self.assertEqual((anthropic["text"], anthropic["stop_reason"]), ("hi", "end_turn"))
        openai = fw._collect_openai_json(
            {"model": "m", "choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}]}
        )
        self.assertEqual((openai["text"], openai["stop_reason"]), ("yo", "stop"))


class Recording(unittest.TestCase):
    def _record(self, flow):
        addon = fw.Flywheel()
        addon.enabled = True
        written = []
        with mock.patch.object(addon, "_append", written.append):
            addon.response(flow)
        return [json.loads(w) for w in written]

    def test_captures_streamed_anthropic_call(self):
        body = sse(
            {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 3}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        )
        flow = FakeFlow(
            FakeRequest(json.dumps({"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}),
                        headers={"user-agent": "claude-cli/2.0", "authorization": "Bearer REAL-SECRET"}),
            FakeResponse(body, headers={"content-type": "text/event-stream"}),
        )
        (record,) = self._record(flow)
        self.assertTrue(record["stream"])
        self.assertEqual(record["completion"]["text"], "ok")
        self.assertEqual(record["model"], "claude-x")
        self.assertEqual(record["user_agent"], "claude-cli/2.0")
        self.assertEqual(record["duration_ms"], 1500.0)
        # Only the user-agent is kept: an injected credential must never be recorded.
        self.assertNotIn("REAL-SECRET", json.dumps(record))

    def test_skips_non_llm_and_failed_calls(self):
        ok_body = json.dumps({"choices": [{"message": {"content": "x"}}]})
        self.assertEqual(
            self._record(FakeFlow(FakeRequest("{}", path="/repos/foo"), FakeResponse(ok_body))), []
        )
        self.assertEqual(
            self._record(FakeFlow(FakeRequest("{}", path="/v1/messages"),
                                  FakeResponse("denied", status=403))), []
        )

    def test_streamed_past_buffer_still_records_the_request(self):
        flow = FakeFlow(
            FakeRequest(json.dumps({"model": "m", "messages": []}), path="/v1/chat/completions"),
            FakeResponse("", headers={"content-type": "text/event-stream"}),
        )
        (record,) = self._record(flow)
        self.assertTrue(record["empty_response"])
        self.assertEqual(record["completion"]["text"], "")


if __name__ == "__main__":
    unittest.main()
