#!/usr/bin/env python3
"""Scope-guard PreToolUse hook for the bug bounty framework.

Reads a Claude Code PreToolUse hook payload on stdin and BLOCKS any Bash or
WebFetch call that targets a host outside the currently authorized program
scope (recon/active-scope.yaml).

Design: this is a *fail-safe heuristic safety net*, not a sandbox. It extracts
candidate hostnames from the command/URL, ignores tooling-infrastructure hosts
(package registries, source hosts, recon data feeds), and denies the call if any
remaining target host is not matched by `in_scope` (or is matched by
`out_of_scope`). With enforcement on and an empty in_scope, every target host is
blocked — you must authorize a program first.

Exit codes: 0 = allow, 2 = deny (stderr reason is shown to the model).

Run `python3 scope-guard.py --selftest` to verify the matching logic offline.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

# Last-label tokens that look like TLDs but are really file extensions — dropped
# from candidate hosts to avoid false positives (e.g. main.go, data.json).
_FILE_EXTS = {
    "py", "sh", "go", "rs", "js", "ts", "json", "yaml", "yml", "md", "txt",
    "html", "htm", "css", "png", "jpg", "jpeg", "gif", "svg", "conf", "cfg",
    "ini", "toml", "lock", "log", "db", "sqlite", "csv", "xml", "env", "pdf",
    "zip", "tar", "gz", "tgz", "bak", "tmp", "pem", "key", "crt", "sql",
}

_URL_RE = re.compile(r"https?://([^/\s'\"`)\]]+)", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9])\b",
    re.IGNORECASE,
)


def project_dir() -> Path:
    """Repo root: CLAUDE_PROJECT_DIR if Claude Code set it, else infer."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    # tooling/scope-guard.py -> repo root is the parent of tooling/
    return Path(__file__).resolve().parent.parent


def load_scope(path: Path) -> dict:
    """Minimal dependency-free parser for the flat active-scope.yaml structure.

    Handles top-level `key: value` scalars (bool/null/str) and `key:` followed by
    indented `- item` lists. Good enough for this fixed schema; not general YAML.
    """
    data: dict = {
        "enforce": True,
        "program": None,
        "in_scope": [],
        "out_of_scope": [],
        "infra_allowlist": [],
    }
    list_keys = {"in_scope", "out_of_scope", "infra_allowlist", "asset_types"}
    cur: str | None = None
    if not path.exists():
        return data  # fail-safe: enforce with empty scope -> block everything
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if (line.startswith((" ", "\t"))) and stripped.startswith("- "):
            if cur in data and isinstance(data.get(cur), list):
                data[cur].append(_unquote(stripped[2:]))
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, val = line.partition(":")
            key = key.strip()
            val = _strip_inline_comment(val.strip())
            if val == "":
                cur = key if key in list_keys else None
                if key in list_keys:
                    data.setdefault(key, [])
                continue
            cur = None
            if key in list_keys:
                data[key] = _parse_inline_list(val)
            else:
                data[key] = _coerce(_unquote(val))
    return data


def _parse_inline_list(val: str) -> list[str]:
    """Parse an inline list like `[]` or `["a", "b"]` from the flat scope file."""
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_unquote(x.strip()) for x in inner.split(",") if x.strip()]
    return [_unquote(val)]


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _strip_inline_comment(s: str) -> str:
    # Drop ` # comment` but keep '#' inside quotes (none expected in this schema).
    if s.startswith(("\"", "'")):
        return s
    return s.split(" #", 1)[0].strip()


def _coerce(val: str):
    low = val.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    return val


def host_matches(host: str, pattern: str) -> bool:
    host = host.lower().strip().strip(".")
    pattern = pattern.lower().strip().strip(".")
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return fnmatch.fnmatch(host, pattern)


def matches_any(host: str, patterns: list[str]) -> bool:
    return any(host_matches(host, p) for p in patterns)


def extract_hosts(text: str) -> set[str]:
    if not text:
        return set()
    hosts: set[str] = set()
    for m in _URL_RE.finditer(text):
        authority = m.group(1)
        authority = authority.split("@")[-1].split(":")[0]
        hosts.add(authority)
    for m in _DOMAIN_RE.finditer(text):
        hosts.add(m.group(1))
    cleaned: set[str] = set()
    for h in hosts:
        h = h.lower().strip().strip(".")
        if not h or h in ("localhost",):
            continue
        last = h.rsplit(".", 1)[-1]
        if last in _FILE_EXTS:
            continue
        cleaned.add(h)
    return cleaned


def candidate_text(payload: dict) -> str:
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}
    if tool == "Bash":
        return ti.get("command", "") or ""
    if tool == "WebFetch":
        return ti.get("url", "") or ""
    # Other tools: also scan a few common string fields defensively.
    return " ".join(str(v) for v in ti.values() if isinstance(v, str))


def evaluate(payload: dict, scope: dict) -> tuple[bool, str]:
    """Return (allow, reason)."""
    if not scope.get("enforce", True):
        return True, "enforcement disabled"

    infra = scope.get("infra_allowlist", []) or []
    in_scope = scope.get("in_scope", []) or []
    out_scope = scope.get("out_of_scope", []) or []

    hosts = extract_hosts(candidate_text(payload))
    # Drop infrastructure hosts (package/source/data feeds) — never targets.
    targets = sorted(h for h in hosts if not matches_any(h, infra))
    if not targets:
        return True, "no target host referenced"

    if not in_scope:
        return (
            False,
            "Scope guard: no program is authorized yet (recon/active-scope.yaml "
            "in_scope is empty). Refusing to touch external host(s): "
            + ", ".join(targets)
            + ". Authorize a program first.",
        )

    for h in targets:
        if matches_any(h, out_scope):
            return False, f"Scope guard: host '{h}' is explicitly OUT OF SCOPE."
        if not matches_any(h, in_scope):
            return (
                False,
                f"Scope guard: host '{h}' is not in the authorized scope for "
                f"program '{scope.get('program')}'. In-scope: {in_scope}. "
                "If this is a legitimate tooling host, add it to infra_allowlist.",
            )
    return True, "all target hosts in scope"


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Can't parse the hook payload -> don't block tooling on a parse error.
        return 0
    scope = load_scope(project_dir() / "recon" / "active-scope.yaml")
    allow, reason = evaluate(payload, scope)
    if allow:
        return 0
    print(reason, file=sys.stderr)
    return 2


def _selftest() -> int:
    scope = {
        "enforce": True,
        "program": "example",
        "in_scope": ["*.example.com", "api.example.org"],
        "out_of_scope": ["blog.example.com"],
        "infra_allowlist": ["github.com", "*.projectdiscovery.io"],
    }

    def ev(cmd, tool="Bash"):
        key = "url" if tool == "WebFetch" else "command"
        return evaluate({"tool_name": tool, "tool_input": {key: cmd}}, scope)[0]

    cases = [
        ("subfinder -d example.com", True),                 # apex in *.example.com
        ("httpx -u https://api.example.com/v1", True),      # subdomain in scope
        ("curl https://api.example.org/health", True),      # exact in scope
        ("nuclei -u https://blog.example.com", False),      # explicitly out of scope
        ("ffuf -u https://evil.com/FUZZ", False),           # not in scope
        ("go install github.com/x/y@latest", True),         # infra allowlisted
        ("chaos -d example.com", True),                     # apex
        ("cat main.go && ls data.json", True),              # file exts, no host
        ("echo hello world", True),                         # nothing
    ]
    ok = True
    for cmd, want in cases:
        got = ev(cmd)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[{flag}] allow={got!s:5} want={want!s:5}  {cmd}")

    # Empty in_scope must block any real target (fail-safe).
    empty = {**scope, "in_scope": []}
    blocked = not evaluate(
        {"tool_name": "Bash", "tool_input": {"command": "subfinder -d example.com"}},
        empty,
    )[0]
    print(f"[{'ok ' if blocked else 'FAIL'}] empty-scope blocks targets")
    ok = ok and blocked

    # WebFetch path.
    wf = ev("https://api.example.com/x", tool="WebFetch")
    print(f"[{'ok ' if wf else 'FAIL'}] webfetch in-scope allowed")
    ok = ok and wf

    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
