#!/usr/bin/env python3
"""
control_server.py — gateway-side credential control endpoint.

Listens on a unix socket in the bind-mounted control dir
(/run/broker-control/broker.sock) and writes credential records into the broker's
redis (the same schema broker_addon.py reads). This is the ONLY write path into the
credential store, and it is reachable ONLY from the host:

  - redis lives on /run/broker/redis.sock in the gateway's MOUNT namespace, which the
    workload does not share -> the workload cannot touch redis directly.
  - this socket lives in /run/broker-control, bind-mounted to the HOST (and NOT into
    the workload). The workload shares the gateway's NETWORK namespace but not its
    mount namespace, so it cannot see this socket either.

So the management surface never enters the workload's reach. Authn/CSRF live in the
host-side web app (broker/webui.py); this endpoint trusts anything that can reach the
socket (gated by the host-private control dir's filesystem perms — single-user-host
assumption, documented in docs/SANDBOX-PLAN.md §15).

Crown-jewel rule: this process can read real secrets (it must, to store them), but it
NEVER returns them. Listing operations mask values to a last-4 hint. Set is
write-only; the way to change a secret is to overwrite it, the way to remove one is to
delete/revoke it. See docs/SANDBOX-PLAN.md §4, §7, §15.

Wire protocol: one JSON request object per connection (the server reads to EOF or the
first newline), one JSON response object back. Operations:

  {"op":"ping"}
  {"op":"host_set",   "host":H, "headers":{name:value,...}}
  {"op":"host_list"}
  {"op":"host_del",   "host":H}
  {"op":"handle_mint","host":H, "path_prefix":P, "methods":[..]|null, "ttl":N,
                      "headers":{name:value,...}}
  {"op":"handle_list"}
  {"op":"handle_revoke","handle":HANDLE}
"""

import json
import os
import secrets
import socket
import socketserver
import sys
import time

import redis

REDIS_SOCK = os.environ.get("REDIS_SOCK", "/run/broker/redis.sock")
CONTROL_SOCK = os.environ.get("CONTROL_SOCK", "/run/broker-control/broker.sock")
MAX_REQUEST = 64 * 1024  # a credential record is small; cap to avoid abuse


def mask(value):
    """Never echo a real secret. Keep only a last-4 hint for recognizability."""
    s = str(value)
    if len(s) <= 8:
        return "•" * len(s)
    return "•" * (len(s) - 4) + s[-4:]


def mask_headers(headers):
    return {k: mask(v) for k, v in (headers or {}).items()}


def connect_redis():
    """Redis may not be up yet on first boot; retry briefly."""
    last = None
    for _ in range(50):
        try:
            r = redis.Redis(unix_socket_path=REDIS_SOCK, decode_responses=True)
            r.ping()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.2)
    raise SystemExit(f"control_server: redis unavailable at {REDIS_SOCK}: {last}")


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            raw = self._read_request()
            req = json.loads(raw or "{}")
            resp = self.dispatch(req)
        except Exception as e:  # noqa: BLE001 — never leak a stack/secret to the caller
            resp = {"ok": False, "error": str(e)}
        self.request.sendall((json.dumps(resp) + "\n").encode())

    def _read_request(self):
        buf = b""
        while b"\n" not in buf and len(buf) < MAX_REQUEST:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", "replace").strip()

    # ---- ops -------------------------------------------------------------
    def dispatch(self, req):
        op = req.get("op")
        fn = getattr(self, f"op_{op}", None) if op else None
        if fn is None:
            return {"ok": False, "error": f"unknown op {op!r}"}
        return fn(req)

    def op_ping(self, req):
        return {"ok": True}

    def op_host_set(self, req):
        host = _need_host(req.get("host"))
        headers = _need_headers(req.get("headers"))
        key = f"broker:host:{host}"
        r = self.server.redis
        r.delete(key)  # replace wholesale so removed headers don't linger
        r.hset(key, mapping=headers)
        return {"ok": True, "host": host, "headers": mask_headers(headers)}

    def op_host_list(self, req):
        r = self.server.redis
        out = []
        for key in r.scan_iter(match="broker:host:*", count=100):
            host = key[len("broker:host:"):]
            out.append({"host": host, "headers": mask_headers(r.hgetall(key))})
        out.sort(key=lambda x: x["host"])
        pats = []
        for pat, hjson in (r.hgetall("broker:hostpats") or {}).items():
            try:
                hdrs = json.loads(hjson)
            except (ValueError, TypeError):
                hdrs = {}
            pats.append({"pattern": pat, "headers": mask_headers(hdrs)})
        pats.sort(key=lambda x: x["pattern"])
        return {"ok": True, "hosts": out, "patterns": pats}

    # Wildcard/subdomain records (e.g. '*.target.com') live in one HASH so the addon
    # fetches them in a single call per request. For non-secret markers — see addon.
    def op_hostpat_set(self, req):
        pat = _need_pattern(req.get("pattern") or req.get("host"))
        headers = _need_headers(req.get("headers"))
        self.server.redis.hset("broker:hostpats", pat, json.dumps(headers))
        return {"ok": True, "pattern": pat, "headers": mask_headers(headers)}

    def op_hostpat_del(self, req):
        pat = (req.get("pattern") or req.get("host") or "").strip().lower()
        if not pat:
            return {"ok": False, "error": "pattern required"}
        self.server.redis.hdel("broker:hostpats", pat)
        return {"ok": True, "pattern": pat}

    def op_host_del(self, req):
        host = _need_host(req.get("host"))
        self.server.redis.delete(f"broker:host:{host}")
        return {"ok": True, "host": host}

    def op_handle_mint(self, req):
        host = _need_host(req.get("host"))
        headers = _need_headers(req.get("headers"))
        ttl = int(req.get("ttl") or 3600)
        if ttl <= 0 or ttl > 30 * 24 * 3600:
            return {"ok": False, "error": "ttl must be 1..2592000 seconds"}
        methods = req.get("methods") or None
        if methods is not None:
            methods = [str(m).upper() for m in methods]
        spec = {
            "host": host,
            "path_prefix": req.get("path_prefix") or "/",
            "methods": methods,
            "headers": headers,
        }
        handle = secrets.token_urlsafe(32)
        key = f"broker:handle:{handle}"
        r = self.server.redis
        r.set(key, json.dumps(spec))
        r.expire(key, ttl)
        # The handle itself is shown ONCE here (it must be dropped into the sandbox).
        # The backing header is masked.
        return {
            "ok": True,
            "handle": handle,
            "spec": {**spec, "headers": mask_headers(headers), "ttl": ttl},
        }

    def op_handle_list(self, req):
        r = self.server.redis
        out = []
        for key in r.scan_iter(match="broker:handle:*", count=100):
            handle = key[len("broker:handle:"):]
            raw = r.get(key)
            if not raw:
                continue
            spec = json.loads(raw)
            out.append({
                "handle": handle,                      # host app truncates for display
                "host": spec.get("host"),
                "path_prefix": spec.get("path_prefix", "/"),
                "methods": spec.get("methods"),
                "ttl": r.ttl(key),
                "headers": mask_headers(spec.get("headers")),
            })
        out.sort(key=lambda x: (x["host"] or "", x["path_prefix"]))
        return {"ok": True, "handles": out}

    def op_handle_revoke(self, req):
        handle = req.get("handle")
        if not handle:
            return {"ok": False, "error": "handle required"}
        self.server.redis.delete(f"broker:handle:{handle}")
        return {"ok": True}


def _need_host(host):
    host = (host or "").strip().lower()
    if not host or "/" in host or " " in host:
        raise ValueError("valid host required")
    return host


def _need_pattern(pat):
    # Like a host but '*' and a leading '.' are allowed (wildcard/subdomain syntax).
    pat = (pat or "").strip().lower()
    if not pat or "/" in pat or " " in pat or "\n" in pat or "\r" in pat:
        raise ValueError("valid host pattern required")
    return pat


def _need_headers(headers):
    if not isinstance(headers, dict) or not headers:
        raise ValueError("headers (non-empty object) required")
    clean = {}
    for k, v in headers.items():
        k = str(k).strip()
        if not k or "\n" in k or "\r" in k or "\n" in str(v) or "\r" in str(v):
            raise ValueError("invalid header name/value")
        clean[k] = str(v)
    return clean


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, redis_conn):
        if os.path.exists(path):
            os.unlink(path)
        super().__init__(path, Handler)
        # Host app runs as a different uid (bind-mount, no userns) — allow it to connect.
        # The control dir itself is host-private (see entrypoint.sh / §15).
        os.chmod(path, 0o666)
        self.redis = redis_conn


def main():
    r = connect_redis()
    os.makedirs(os.path.dirname(CONTROL_SOCK), exist_ok=True)
    srv = Server(CONTROL_SOCK, r)
    print(f"control_server: listening on {CONTROL_SOCK}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
