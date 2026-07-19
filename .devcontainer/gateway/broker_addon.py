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
import socket
import time
from fnmatch import fnmatch

import redis
from mitmproxy import http
from mitmproxy.connection import Server
from mitmproxy.net.server_spec import ServerSpec

ALLOW_FILE = os.environ.get("ALLOWED_DOMAINS", "/etc/broker/allowed-domains.txt")
REDIS_SOCK = os.environ.get("REDIS_SOCK", "/run/broker/redis.sock")
# Optional downstream proxy (the operator's Caido workbench). The gateway entrypoint
# writes "host:port" here ONLY after it has fetched + trusted Caido's CA — so its mere
# presence is the signal to chain inspected flows through Caido. Absent -> direct, as
# before. Kept out of the env so a failed CA fetch can't half-enable routing.
CAIDO_MARKER = os.environ.get("CAIDO_UPSTREAM_FILE", "/run/broker/caido-upstream")
# Redacted request log, written to the bind-mounted control dir so the host-side web
# UI can tail it. The workload cannot see this path (different mount namespace).
REQUEST_LOG = os.environ.get("REQUEST_LOG", "/run/broker-control/requests.log")
REQUEST_LOG_MAX = 5 * 1024 * 1024  # ~5 MB; trimmed to the recent half when exceeded
HANDLE_HEADER = "x-sandbox-handle"

log = logging.getLogger("broker")
logging.basicConfig(level=logging.INFO)


class RequestLog:
    """Append-only JSONL of egress *decisions*, redacted by construction.

    We log who-went-where and the verdict — NEVER credential material: no header
    values (only the names of injected headers), no request/response bodies, and the
    query string is stripped from the path (it can carry tokens). See SANDBOX-PLAN §15.
    """

    def __init__(self, path):
        self.path = path
        self._writes = 0

    def record(self, decision, method, host, path, **extra):
        rec = {
            "ts": time.time(),
            "decision": decision,
            "method": method,
            "host": host,
            "path": (path or "").split("?", 1)[0],  # drop query — may carry secrets
            **extra,
        }
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self._writes += 1
            if self._writes % 200 == 0:
                self._trim()
        except OSError as e:
            log.warning("broker: request-log write failed: %s", e)

    def _trim(self):
        try:
            if os.path.getsize(self.path) <= REQUEST_LOG_MAX:
                return
            with open(self.path, "rb") as f:
                f.seek(-REQUEST_LOG_MAX // 2, os.SEEK_END)
                f.readline()  # discard the partial line we landed in
                tail = f.read()
            with open(self.path, "wb") as f:
                f.write(tail)
        except OSError as e:
            log.warning("broker: request-log trim failed: %s", e)


class Broker:
    def __init__(self):
        self.inspect = []       # decrypt + may inject
        self.passthrough = []   # allowed but spliced (no decrypt)
        self._mtime = 0.0
        self.r = None
        self.rlog = RequestLog(REQUEST_LOG)
        self.caido = None       # (host, port) of the downstream Caido proxy, or None
        self._dns_cache = {}    # sni -> (set(ip), expiry) for splice dst-IP verification

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
        self.caido = self._load_caido()
        if self.caido:
            log.info("broker: chaining inspected flows -> caido %s:%d", *self.caido)

    @staticmethod
    def _load_caido():
        """Read the entrypoint-written marker ("host:port"). Present == enabled."""
        try:
            with open(CAIDO_MARKER) as f:
                spec = f.read().strip()
        except OSError:
            return None
        host, _, port = spec.rpartition(":")
        if host and port.isdigit():
            return (host, int(port))
        log.warning("broker: ignoring malformed caido marker %r", spec)
        return None

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
        if not self._match(sni, self.passthrough):
            return  # non-passthrough hosts are decrypted; allow/deny in requestheaders
        # SECURITY: splice ONLY if the real destination IP actually belongs to the SNI
        # host. In transparent mode the workload controls BOTH the SNI and the original
        # destination IP; matching on SNI alone would let it splice a raw TLS tunnel to an
        # attacker's IP (arbitrary egress / C2) just by sending a spoofed SNI for an
        # allowed passthrough host. Tie the two together: resolve the SNI and require the
        # actual destination to be one of its addresses.
        try:
            dst_ip = data.context.server.address[0]
        except Exception:  # noqa: BLE001 — unknown dst -> treat as mismatch, fail closed
            dst_ip = None
        if dst_ip and self._sni_matches_dst(sni, dst_ip):
            data.ignore_connection = True  # allowed, opaque
            self.rlog.record("SPLICE", "TLS", sni, "")
        else:
            # Fail closed: refuse to splice. mitmproxy then intercepts; a genuinely
            # cert-pinned host rejects the MITM cert (connection dies) and any decrypted
            # flow is DENIED in requestheaders (passthrough hosts are not in `inspect`).
            # Either way no un-inspected bytes reach the spoofed destination.
            log.warning("DENY splice: SNI/dst mismatch sni=%s dst=%s", sni, dst_ip)
            self.rlog.record("DENY", "TLS", sni or "", "", reason="sni/dst mismatch")

    _DNS_TTL = 30.0

    def _sni_matches_dst(self, sni, dst_ip):
        """True if dst_ip is one of the addresses the SNI host currently resolves to.

        Cached briefly and unioned across recent lookups so CDN round-robin (different A
        record than the workload got) doesn't spuriously deny a legitimate pinned host.
        On resolve failure we keep the previous answer rather than failing legit traffic.
        """
        if not sni:
            return False
        now = time.time()
        ips, exp = self._dns_cache.get(sni, (set(), 0.0))
        if now >= exp:
            fresh = set()
            try:
                for info in socket.getaddrinfo(sni, None):
                    fresh.add(info[4][0])
            except OSError as e:
                log.warning("broker: DNS resolve failed for splice check %s: %s", sni, e)
            if fresh:
                ips = ips | fresh  # union to tolerate CDN rotation within the TTL window
                self._dns_cache[sni] = (ips, now + self._DNS_TTL)
        return dst_ip in ips

    # ---- HTTP: enforce allowlist, then inject ----------------------------
    def requestheaders(self, flow: http.HTTPFlow):
        self._load()
        host = flow.request.pretty_host
        if not self._match(host, self.inspect):
            log.warning("DENY %s %s%s", flow.request.method, host, flow.request.path)
            self.rlog.record("DENY", flow.request.method, host, flow.request.path)
            flow.response = http.Response.make(403, b"egress blocked by sandbox broker\n")
            return
        self._inject(flow)

    def request(self, flow: http.HTTPFlow):
        """Chain allowed, decrypted flows through the operator's Caido proxy so it
        sees the exact on-wire request — post-injection, real credentials and all.

        Runs AFTER requestheaders: denied flows already carry a response (skip them);
        spliced/pinned hosts are ignored at TLS and never reach an HTTP hook. Requires
        `connection_strategy=lazy` so the upstream connection is still unopened here.
        """
        if self.caido is None or flow.response is not None:
            return
        via = ServerSpec(("http", self.caido))
        if flow.server_conn.via == via:
            return
        # mitmproxy refuses to change `via` on an already-open upstream connection;
        # reset to a fresh Server (same destination) before re-pointing it.
        if flow.server_conn.timestamp_start is not None:
            flow.server_conn = Server(address=flow.server_conn.address)
        flow.server_conn.via = via

    def _inject(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        method = flow.request.method
        path = flow.request.path
        handle = flow.request.headers.pop(HANDLE_HEADER, None)  # always strip; never forward upstream

        # Credentials ride ONLY verified TLS. On cleartext HTTP the upstream is
        # unauthenticated (no cert for mitmproxy to verify against the host), so injecting
        # could hand the real secret to an attacker-controlled box reached via a spoofed
        # Host header. Port 80 is already dropped at the firewall; this is defense in depth.
        if flow.request.scheme != "https":
            log.info("ALLOW %s %s%s (no inject: non-https)", method, host, path)
            self.rlog.record("ALLOW", method, host, path, note="no-inject-non-https")
            return

        headers = None
        if handle and self.r is not None:
            headers = self._resolve_handle(handle, host, method, path)
            if headers is None:
                log.warning("DENY handle out of scope: host=%s method=%s path=%s", host, method, path)
                self.rlog.record("DENY_SCOPE", method, host, path, via="handle")
                flow.response = http.Response.make(403, b"handle out of scope\n")
                return
        if headers is None and self.r is not None:
            headers = self._host_headers(host)

        if headers:
            for k, v in headers.items():
                flow.request.headers[k] = v
            log.info("INJECT %s %s (%d header(s))", method, host, len(headers))
            # Header NAMES only — never the injected values.
            self.rlog.record("INJECT", method, host, path,
                             injected=sorted(headers.keys()), via=("handle" if handle else "host"))
        else:
            log.info("ALLOW %s %s%s", method, host, path)
            self.rlog.record("ALLOW", method, host, path)

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

    @staticmethod
    def _pat_match(host, pat):
        # Subdomain forms first: '*.x.com' / '.x.com' / bare 'x.com' all match x.com
        # AND any subdomain of it. A '*' elsewhere falls back to a literal glob.
        if pat.startswith("*."):
            base = pat[2:]
        elif pat.startswith("."):
            base = pat[1:]
        elif "*" in pat:
            return fnmatch(host, pat)
        else:
            base = pat
        return host == base or host.endswith("." + base)

    def _host_headers(self, host):
        """Merge wildcard/subdomain patterns + the exact-host record (exact wins).

        So a scope-wide marker (`*.target.com`) and a host-specific credential
        (`api.target.com`) both apply, and the specific one overrides on conflict.
        Exact records stay strict (confused-deputy guard); wildcards are opt-in by
        the operator typing `*.`/`.` — meant for non-secret markers, not credentials.
        """
        host = host.lower()
        merged = {}
        try:
            pats = self.r.hgetall("broker:hostpats") or {}
        except Exception as e:
            log.warning("broker: redis error on pattern lookup: %s", e)
            pats = {}
        for pat, hjson in pats.items():
            if self._pat_match(host, pat):
                try:
                    merged.update(json.loads(hjson))
                except (ValueError, TypeError):
                    pass
        try:
            exact = self.r.hgetall(f"broker:host:{host}")
        except Exception as e:
            log.warning("broker: redis error on host lookup: %s", e)
            exact = None
        if exact:
            merged.update(exact)
        return merged or None


addons = [Broker()]
