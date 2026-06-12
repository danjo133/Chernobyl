"""
broker_addon.py — mitmproxy addon: egress allowlist + credential broker.

UNVERIFIED scaffold. This is the security-critical component: deny-by-default,
log every decision, and never log credential material. See docs/SANDBOX-PLAN.md §4, §7.

Model
-----
- allowed-domains.txt lists permitted hosts. A leading '!' marks a *passthrough*
  (spliced, NOT decrypted) host — for cert-pinned upstreams. Everything else that
  is allowed is decrypted ("inspect") so we can inject credentials.
- Anything not listed is DENIED (403 for HTTP; TLS to it simply fails).
- Credential injection (so the real secret never enters the workload):
    * phantom handle: workload sends header `X-Sandbox-Handle: <opaque>`; we look it
      up in redis, scope-check (host + path prefix + method + TTL), then swap in the
      real Authorization. The handle is stripped from the outgoing request.
    * inject-by-host: if no handle, attach cached headers registered for that host.
- Confused-deputy guards: injection is bound to the exact request host; handle scope
  must match host+path+method; we never carry a handle across hosts.

redis schema (populated out-of-band by the host-side filler — see broker/README.md)
-----------------------------------------------------------------------------------
  broker:handle:<handle>   -> JSON {"host","path_prefix","methods":[..],"headers":{..}}
                              (set with a redis TTL = the handle's expiry)
  broker:host:<host>       -> redis HASH of header-name -> header-value
"""

import json
import logging
import os
import time
from fnmatch import fnmatch

import redis
from mitmproxy import http

ALLOW_FILE = os.environ.get("ALLOWED_DOMAINS", "/etc/broker/allowed-domains.txt")
REDIS_SOCK = os.environ.get("REDIS_SOCK", "/run/broker/redis.sock")
HANDLE_HEADER = "x-sandbox-handle"

log = logging.getLogger("broker")
logging.basicConfig(level=logging.INFO)


class Broker:
    def __init__(self):
        self.inspect = []       # decrypt + may inject
        self.passthrough = []   # allowed but spliced (no decrypt)
        self._mtime = 0.0
        self.r = None

    # ---- lifecycle -------------------------------------------------------
    def running(self):
        self._load()
        try:
            self.r = redis.Redis(unix_socket_path=REDIS_SOCK, decode_responses=True)
            self.r.ping()
            log.info("broker: redis connected")
        except Exception as e:  # broker still enforces the allowlist without redis
            self.r = None
            log.warning("broker: redis unavailable (%s) — injection disabled, allowlist still enforced", e)
        log.info("broker: %d inspect / %d passthrough domains", len(self.inspect), len(self.passthrough))

    def _load(self):
        try:
            st = os.stat(ALLOW_FILE)
        except FileNotFoundError:
            log.error("broker: allowlist %s missing — DENYING ALL", ALLOW_FILE)
            self.inspect, self.passthrough = [], []
            return
        if st.st_mtime == self._mtime:
            return
        self._mtime = st.st_mtime
        inspect, passthrough = [], []
        with open(ALLOW_FILE) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("!"):
                    passthrough.append(line[1:].strip().lower())
                else:
                    inspect.append(line.lower())
        self.inspect, self.passthrough = inspect, passthrough
        log.info("broker: allowlist reloaded (%d inspect / %d passthrough)", len(inspect), len(passthrough))

    # ---- matching --------------------------------------------------------
    @staticmethod
    def _match(host, patterns):
        host = (host or "").lower()
        return any(fnmatch(host, p) or host == p or host.endswith("." + p) for p in patterns)

    # ---- TLS: splice passthrough (cert-pinned) hosts, no decryption ------
    def tls_clienthello(self, data):
        self._load()
        sni = data.client_hello.sni
        if self._match(sni, self.passthrough):
            data.ignore_connection = True  # allowed, opaque
        # Non-passthrough hosts are decrypted; allow/deny is enforced in requestheaders.

    # ---- HTTP: enforce allowlist, then inject ----------------------------
    def requestheaders(self, flow: http.HTTPFlow):
        self._load()
        host = flow.request.pretty_host
        if not self._match(host, self.inspect):
            log.warning("DENY %s %s%s", flow.request.method, host, flow.request.path)
            flow.response = http.Response.make(403, b"egress blocked by sandbox broker\n")
            return
        self._inject(flow)

    def _inject(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        method = flow.request.method
        path = flow.request.path
        handle = flow.request.headers.pop(HANDLE_HEADER, None)  # always strip; never forward upstream

        headers = None
        if handle and self.r is not None:
            headers = self._resolve_handle(handle, host, method, path)
            if headers is None:
                log.warning("DENY handle out of scope: host=%s method=%s path=%s", host, method, path)
                flow.response = http.Response.make(403, b"handle out of scope\n")
                return
        if headers is None and self.r is not None:
            headers = self._host_headers(host)

        if headers:
            for k, v in headers.items():
                flow.request.headers[k] = v
            log.info("INJECT %s %s (%d header(s))", method, host, len(headers))
        else:
            log.info("ALLOW %s %s%s", method, host, path)

    def _resolve_handle(self, handle, host, method, path):
        try:
            raw = self.r.get(f"broker:handle:{handle}")  # TTL on the key handles expiry
        except Exception as e:
            log.warning("broker: redis error on handle lookup: %s", e)
            return None
        if not raw:
            return None
        spec = json.loads(raw)
        # Confused-deputy guards: bind to exact host, path prefix, and method.
        if spec.get("host", "").lower() != host.lower():
            return None
        if not path.startswith(spec.get("path_prefix", "/")):
            return None
        methods = spec.get("methods")
        if methods and method.upper() not in [m.upper() for m in methods]:
            return None
        return spec.get("headers", {})

    def _host_headers(self, host):
        try:
            h = self.r.hgetall(f"broker:host:{host.lower()}")
        except Exception as e:
            log.warning("broker: redis error on host lookup: %s", e)
            return None
        return h or None


addons = [Broker()]
