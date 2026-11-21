#!/usr/bin/env python3
"""Tests for the GitHub write guard in broker_addon.py: python3 broker_guard_test.py

Stubs mitmproxy and redis so this runs anywhere. The guard is what stops a sandboxed
lane from merging a PR or pushing to main when branch protection is unavailable, so
each refused request and each allowed one is pinned here, including the real git
push wire format (pkt-lines).
"""

import json
import sys
import types
import unittest

# --- stub mitmproxy + redis before importing the addon ---------------------------
for name in ("mitmproxy", "mitmproxy.http", "mitmproxy.connection", "mitmproxy.net", "mitmproxy.net.server_spec", "redis"):
    sys.modules.setdefault(name, types.ModuleType(name))


class _Resp:
    def __init__(self, code, body):
        self.status_code, self.content = code, body


sys.modules["mitmproxy.http"].HTTPFlow = object
sys.modules["mitmproxy.http"].Response = types.SimpleNamespace(make=lambda code, body: _Resp(code, body))
sys.modules["mitmproxy"].http = sys.modules["mitmproxy.http"]
sys.modules["mitmproxy.connection"].Server = object
sys.modules["mitmproxy.net.server_spec"].ServerSpec = object
sys.modules["redis"].Redis = object

import broker_addon as ba  # noqa: E402

P = "omni/"


def pkt(line: str) -> bytes:
    data = line.encode()
    return f"{len(data) + 4:04x}".encode() + data


def push(*refs, caps=True) -> bytes:
    out = b""
    for i, r in enumerate(refs):
        cap = "\0report-status side-band-64k" if caps and i == 0 else ""
        out += pkt(f"{'0' * 40} {'a' * 40} {r}{cap}\n")
    return out + b"0000PACK....binary...."


def reason(host, method, path, body=b""):
    return ba.github_guard_reason(host, method, path, body, P)


class GitPush(unittest.TestCase):
    def test_push_to_own_branch_is_allowed(self):
        self.assertIsNone(reason("github.com", "POST", "/O/r.git/git-receive-pack", push("refs/heads/omni/42-fix")))

    def test_push_to_main_is_refused(self):
        self.assertIn("refs/heads/main", reason("github.com", "POST", "/O/r.git/git-receive-pack", push("refs/heads/main")))

    def test_one_bad_ref_among_good_ones_is_refused(self):
        body = push("refs/heads/omni/a", "refs/heads/master")
        self.assertIn("master", reason("github.com", "POST", "/O/r/git-receive-pack", body))

    def test_tags_and_other_namespaces_are_refused(self):
        for ref in ("refs/tags/v1", "refs/heads/omnibus", "refs/heads/feature/omni/x"):
            self.assertIsNotNone(reason("github.com", "POST", "/O/r.git/git-receive-pack", push(ref)), ref)

    def test_garbage_body_is_refused(self):
        self.assertIn("unparseable", reason("github.com", "POST", "/O/r.git/git-receive-pack", b"zzzzjunk"))

    def test_fetch_and_clone_are_untouched(self):
        self.assertIsNone(reason("github.com", "POST", "/O/r.git/git-upload-pack", push("refs/heads/main")))
        self.assertIsNone(reason("github.com", "GET", "/O/r.git/info/refs?service=git-receive-pack"))


class RestApi(unittest.TestCase):
    def test_merge_endpoints_are_refused(self):
        self.assertEqual(reason("api.github.com", "PUT", "/repos/O/r/pulls/7/merge"), "merging a pull request")
        self.assertEqual(reason("api.github.com", "POST", "/repos/O/r/merges", b'{"base":"main"}'), "merging a branch")

    def test_opening_and_commenting_on_prs_is_allowed(self):
        self.assertIsNone(reason("api.github.com", "POST", "/repos/O/r/pulls", b'{"head":"omni/x","base":"main"}'))
        self.assertIsNone(reason("api.github.com", "POST", "/repos/O/r/issues/7/comments", b'{"body":"hi"}'))
        self.assertIsNone(reason("api.github.com", "GET", "/repos/O/r/pulls/7/merge"))       # "is it merged?" is a read

    def test_ref_moves_outside_the_prefix_are_refused(self):
        self.assertIsNotNone(reason("api.github.com", "PATCH", "/repos/O/r/git/refs/heads/main", b'{"sha":"x","force":true}'))
        self.assertIsNotNone(reason("api.github.com", "DELETE", "/repos/O/r/git/refs/heads/main"))
        self.assertIsNotNone(reason("api.github.com", "POST", "/repos/O/r/git/refs", b'{"ref":"refs/heads/main","sha":"x"}'))
        self.assertIsNone(reason("api.github.com", "POST", "/repos/O/r/git/refs", b'{"ref":"refs/heads/omni/9","sha":"x"}'))
        self.assertIsNone(reason("api.github.com", "PATCH", "/repos/O/r/git/refs/heads/omni/9", b'{"sha":"x"}'))

    def test_contents_api_only_on_own_branches(self):
        self.assertIn("default branch", reason("api.github.com", "PUT", "/repos/O/r/contents/a.txt", b'{"message":"m","content":"eA=="}'))
        self.assertIsNotNone(reason("api.github.com", "PUT", "/repos/O/r/contents/a.txt", b'{"branch":"main"}'))
        self.assertIsNone(reason("api.github.com", "PUT", "/repos/O/r/contents/a.txt", b'{"branch":"omni/3"}'))
        self.assertIsNone(reason("api.github.com", "GET", "/repos/O/r/contents/a.txt"))

    def test_hooks_and_deploy_keys_are_refused(self):
        self.assertIsNotNone(reason("api.github.com", "POST", "/repos/O/r/hooks", b"{}"))
        self.assertIsNotNone(reason("api.github.com", "POST", "/repos/O/r/keys", b"{}"))
        self.assertIsNone(reason("api.github.com", "GET", "/repos/O/r/hooks"))

    def test_graphql_merge_and_ref_mutations_are_refused_queries_are_not(self):
        m = json.dumps({"query": "mutation { mergePullRequest(input:{pullRequestId:\"x\"}) { clientMutationId } }"}).encode()
        self.assertIn("mergePullRequest", reason("api.github.com", "POST", "/graphql", m))
        q = json.dumps({"query": "query { viewer { login } }"}).encode()
        self.assertIsNone(reason("api.github.com", "POST", "/graphql", q))

    def test_other_hosts_are_not_the_guards_business(self):
        self.assertIsNone(reason("pypi.org", "PUT", "/repos/O/r/pulls/7/merge"))


class FakeRedis:
    def __init__(self, guard):
        self.guard = guard

    def get(self, key):
        return self.guard if key == "broker:guard:github" else None


class FakeReq:
    def __init__(self, host, method, path, body=b""):
        self.pretty_host, self.method, self.path, self._body = host, method, path, body

    def get_content(self, strict=False):
        return self._body


class Flow:
    def __init__(self, req):
        self.request, self.response = req, None


class BrokerHook(unittest.TestCase):
    def broker(self, guard):
        b = ba.Broker.__new__(ba.Broker)
        b.r = FakeRedis(guard)
        b.rlog = types.SimpleNamespace(record=lambda *a, **k: None)
        return b

    def test_guard_off_when_not_configured(self):
        f = Flow(FakeReq("api.github.com", "PUT", "/repos/O/r/pulls/7/merge"))
        self.assertFalse(self.broker(None)._github_guard(f))
        self.assertIsNone(f.response)

    def test_guard_on_refuses_with_403_before_anything_is_sent(self):
        f = Flow(FakeReq("api.github.com", "PUT", "/repos/O/r/pulls/7/merge"))
        self.assertTrue(self.broker(json.dumps({"push_prefix": "omni/"}))._github_guard(f))
        self.assertEqual(f.response.status_code, 403)

    def test_unreadable_guard_config_allows_nothing(self):
        f = Flow(FakeReq("github.com", "POST", "/O/r.git/git-receive-pack", push("refs/heads/omni/x")))
        self.assertTrue(self.broker("{not json")._github_guard(f))


if __name__ == "__main__":
    unittest.main(verbosity=1)
