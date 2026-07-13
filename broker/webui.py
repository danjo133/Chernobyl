#!/usr/bin/env python3
"""
webui.py — host-side credential & egress-log web UI for one sandbox.

Runs ON THE HOST (started by the `sandbox` CLI), binds 127.0.0.1 only, and is the
human-facing management surface for a single sandbox's gateway:

  - set inject-by-host headers / mint+revoke scoped phantom handles
  - watch the redacted egress request log

Why it lives on the host and NOT on the gateway: the workload shares the gateway's
NETWORK namespace, so anything listening inside the gateway is reachable from the
workload at localhost. A credential-management surface there would hand creds to the
very thing we keep them from. The host's loopback is a different localhost the workload
cannot reach. This process talks to the gateway only over the control unix socket in
the bind-mounted control dir. See docs/SANDBOX-PLAN.md §15.

Security model:
  - Bind 127.0.0.1 only; never 0.0.0.0.
  - Password (generated per `sandbox up`, mode 0600) gates everything; constant-time
    compare; success sets a per-run session cookie (HttpOnly, SameSite=Strict).
  - Host-header allowlist on every request (anti DNS-rebinding) and Origin check on
    every mutating POST (anti-CSRF), on top of SameSite=Strict.
  - The UI never displays real secrets: the gateway masks them to a last-4 hint. A
    freshly minted handle is shown once (it must be dropped into the sandbox).
"""

import argparse
import hmac
import json
import secrets
import socket
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 64 * 1024


# --------------------------------------------------------------------------- #
# control-socket client                                                        #
# --------------------------------------------------------------------------- #
def control_call(sock_path, req, timeout=5.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
    except OSError as e:
        return {"ok": False, "error": f"gateway control socket unreachable ({e}). Is the sandbox up?"}
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode() or "{}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        s.close()


def tail_log(path, limit=300):
    try:
        with open(path, "rb") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    out.reverse()  # most recent first
    return out


# --------------------------------------------------------------------------- #
# request handler                                                              #
# --------------------------------------------------------------------------- #
class App(BaseHTTPRequestHandler):
    server_version = "sandbox-broker-ui"

    # injected by make_handler()
    cfg = None

    def log_message(self, *a):  # quiet; the request log is the gateway's, not ours
        pass

    # ---- helpers ---------------------------------------------------------
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["session"].value if "session" in c else ""
        return bool(tok) and hmac.compare_digest(tok, self.cfg["session"])

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser / same-origin fetch may omit it
        # CSRF guard: the request must originate from our own page. A loopback host
        # is the tell — a cross-site attacker is never served from localhost. We
        # ignore the port so SSH-forwarding to a different local port still works
        # (an attacker can't forge the hostname; SameSite=Strict backstops this).
        try:
            u = urlsplit(origin)
        except ValueError:
            return False
        return u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="text/html; charset=utf-8", cookie=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _control(self, req):
        return control_call(self.cfg["socket"], req)

    # ---- routing ---------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(421, "bad host\n", "text/plain")
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, DASHBOARD if self._authed() else LOGIN)
        if not self._authed():
            return self._json({"ok": False, "error": "auth required"}, 401)
        if self.path == "/api/hosts":
            return self._json(self._control({"op": "host_list"}))
        if self.path == "/api/handles":
            return self._json(self._control({"op": "handle_list"}))
        if self.path == "/api/log":
            return self._json({"ok": True, "log": tail_log(self.cfg["log"])})
        return self._send(404, "not found\n", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return self._send(421, "bad host\n", "text/plain")
        if not self._origin_ok():
            return self._json({"ok": False, "error": "bad origin"}, 403)

        if self.path == "/login":
            data = self._read_body()
            supplied = (data.get("password") or "").encode()
            if hmac.compare_digest(supplied, self.cfg["password"].encode()):
                cookie = f"session={self.cfg['session']}; HttpOnly; SameSite=Strict; Path=/"
                return self._redirect("/", cookie)
            return self._send(200, LOGIN_BAD)

        if not self._authed():
            return self._json({"ok": False, "error": "auth required"}, 401)

        data = self._read_body()
        if self.path == "/api/host-set":
            headers = data.get("headers")
            if isinstance(headers, str):
                headers = _parse_header_lines(headers)
            return self._json(self._control({
                "op": "host_set", "host": data.get("host"), "headers": headers}))
        if self.path == "/api/host-set-many":
            headers = data.get("headers")
            if isinstance(headers, str):
                headers = _parse_header_lines(headers)
            if not headers:
                return self._json({"ok": False, "error": "enter at least one header (Name: value)"}, 400)
            seen, items = set(), []
            for line in str(data.get("hosts") or "").splitlines():
                h = _norm_host(line)
                if h is None or h in seen:
                    continue
                seen.add(h)
                # '*.x.com' / '.x.com' -> subdomain pattern; plain host -> exact.
                if "*" in h or h.startswith("."):
                    items.append(("hostpat_set", "pattern", h))
                else:
                    items.append(("host_set", "host", h))
            if not items:
                return self._json({"ok": False, "error": "no valid domains"}, 400)
            results = []
            for op, field, key in items:
                r = self._control({"op": op, field: key, "headers": headers})
                results.append({"host": key, "wildcard": op == "hostpat_set",
                                "ok": bool(r.get("ok")), "error": r.get("error")})
            done = sum(1 for x in results if x["ok"])
            return self._json({"ok": done == len(items), "count": done,
                               "total": len(items), "results": results})
        if self.path == "/api/host-del":
            return self._json(self._control({"op": "host_del", "host": data.get("host")}))
        if self.path == "/api/hostpat-del":
            return self._json(self._control({"op": "hostpat_del", "pattern": data.get("pattern")}))
        if self.path == "/api/handle-mint":
            headers = data.get("headers")
            if isinstance(headers, str):
                headers = _parse_header_lines(headers)
            methods = data.get("methods")
            if isinstance(methods, str):
                methods = [m.strip().upper() for m in methods.replace(",", " ").split() if m.strip()]
            return self._json(self._control({
                "op": "handle_mint", "host": data.get("host"),
                "path_prefix": data.get("path_prefix") or "/",
                "methods": methods or None, "ttl": int(data.get("ttl") or 3600),
                "headers": headers}))
        if self.path == "/api/handle-revoke":
            return self._json(self._control({"op": "handle_revoke", "handle": data.get("handle")}))
        if self.path == "/logout":
            return self._redirect("/", "session=; Max-Age=0; Path=/")
        return self._send(404, "not found\n", "text/plain")

    def _redirect(self, loc, cookie=None):
        self.send_response(303)
        self.send_header("Location", loc)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _parse_header_lines(text):
    """'Authorization: Bearer x' / 'Name=Value', one per line -> dict."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        sep = ":" if ":" in line else ("=" if "=" in line else None)
        if not sep:
            continue
        k, v = line.split(sep, 1)
        out[k.strip()] = v.strip()
    return out


def _norm_host(line):
    """One pasted scope line -> a bare hostname the broker matches on, or None.

    Injection keys on mitmproxy's pretty_host (hostname, no scheme/port/path), so we
    normalize accordingly: drop comments/blanks, strip scheme, userinfo, path, and port.
    'https://api.example.com:8443/v1' -> 'api.example.com'.
    """
    h = line.strip().lower()
    if not h or h.startswith("#"):
        return None
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0]      # drop path/query
    if "@" in h:
        h = h.rsplit("@", 1)[1]  # drop userinfo
    h = h.split(":", 1)[0]       # drop port
    return h or None


# --------------------------------------------------------------------------- #
# pages (data is fetched as JSON and rendered with textContent — no server-side  #
# interpolation of dynamic values, so no injection surface)                     #
# --------------------------------------------------------------------------- #
_STYLE = """<style>
 body{font:14px/1.5 system-ui,sans-serif;max-width:920px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
 h1{font-size:1.3rem} h2{font-size:1.05rem;margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}
 input,textarea,button{font:inherit;padding:.4rem;box-sizing:border-box}
 textarea{width:100%;height:4rem} label{display:block;margin:.5rem 0 .15rem;font-weight:600}
 table{border-collapse:collapse;width:100%;margin-top:.5rem} td,th{border:1px solid #e2e2e2;padding:.35rem .5rem;text-align:left;vertical-align:top}
 .row{display:flex;gap:1rem;flex-wrap:wrap} .row>div{flex:1;min-width:240px}
 button{cursor:pointer;border:1px solid #999;border-radius:4px;background:#f4f4f4}
 button.danger{color:#a00} code{background:#f3f3f3;padding:.1rem .3rem;border-radius:3px}
 .mono{font-family:ui-monospace,monospace} .muted{color:#777} .ok{color:#070} .bad{color:#a00}
 .pill{padding:.05rem .4rem;border-radius:3px;font-size:.8rem}
 .DENY,.DENY_SCOPE{background:#fde2e2;color:#a00} .INJECT{background:#e2ecfd;color:#0356b6}
 .ALLOW{background:#eee} .SPLICE{background:#fff3d6;color:#8a6d00}
</style>"""

LOGIN = f"""<!doctype html><meta charset=utf-8><title>sandbox broker</title>{_STYLE}
<h1>sandbox broker</h1><p class=muted>Enter the password shown by <code>./sandbox ls</code>.</p>
<form method=post action=/login>
 <label>Password</label><input type=password name=password autofocus style=width:320px>
 <button>Unlock</button>
</form>"""

LOGIN_BAD = LOGIN.replace("<form", "<p class=bad>Wrong password.</p><form")

DASHBOARD = f"""<!doctype html><meta charset=utf-8><title>sandbox broker</title>{_STYLE}
<form method=post action=/logout style=float:right><button>Log out</button></form>
<h1>sandbox broker</h1>
<p class=muted>Credentials are injected on egress and never enter the container. Stored
values are shown masked; only a freshly minted handle is shown in full (once).</p>

<h2>Inject-by-host headers</h2>
<p class=muted>The workload sends bare requests; the gateway staples these headers onto every allowed request to each host. One domain per line; scheme/port/path stripped (<code>https://api.x.com:8443/v1</code> &rarr; <code>api.x.com</code>). A plain host = <b>exact</b> match; <code>*.x.com</code> or <code>.x.com</code> = that domain <b>and all subdomains</b> (paste a bug-bounty <code>*.target.com</code> scope directly). Wildcard + exact records merge, exact winning on conflict. <b>Use wildcards for non-secret markers; keep real credentials on exact hosts</b> (a wildcard broadcasts the header to every subdomain reached).</p>
<div class=row>
 <div>
  <label>Hosts (one per line — exact <code>api.x.com</code> or wildcard <code>*.x.com</code>)</label>
  <textarea id=h_hosts style=height:7rem placeholder="*.intigriti.com&#10;api.intigriti.com&#10;staging.intigriti.com"></textarea>
  <label>Headers (one per line: <code>Name: value</code>) — applied to every host above</label>
  <textarea id=h_hdr placeholder="X-Bug-Bounty: my-researcher-tag"></textarea>
  <button onclick=hostSet()>Set on all</button> <span id=h_msg class=muted></span>
 </div>
</div>
<table id=h_tbl><thead><tr><th>Host / pattern</th><th>Match</th><th>Headers (masked)</th><th></th></tr></thead><tbody></tbody></table>

<h2>Scoped phantom handles</h2>
<p class=muted>The workload carries an opaque <code>X-Sandbox-Handle</code>; the gateway swaps in the real header only for matching host + path-prefix + method, until the TTL expires.</p>
<div class=row>
 <div>
  <label>Host</label><input id=p_host style=width:100%>
  <label>Path prefix</label><input id=p_path value=/ style=width:100%>
  <label>Methods (blank = any)</label><input id=p_meth placeholder="GET LIST" style=width:100%>
  <label>TTL (seconds)</label><input id=p_ttl value=3600 style=width:100%>
 </div>
 <div>
  <label>Headers (one per line: <code>Name: value</code>)</label>
  <textarea id=p_hdr placeholder="Authorization: Bearer REAL_TOKEN"></textarea>
  <button onclick=handleMint()>Mint handle</button>
  <p id=p_minted class=ok></p>
 </div>
</div>
<table id=p_tbl><thead><tr><th>Handle</th><th>Host</th><th>Path</th><th>Methods</th><th>TTL</th><th>Headers</th><th></th></tr></thead><tbody></tbody></table>

<h2>Egress log <span class=muted style=font-weight:400>(redacted — no secrets, no bodies)</span> <button onclick=refreshLog() style=float:right>Refresh</button></h2>
<table id=l_tbl><thead><tr><th>Time</th><th>Decision</th><th>Method</th><th>Host</th><th>Path</th><th>Injected</th></tr></thead><tbody></tbody></table>

<script>
const $=id=>document.getElementById(id);
async function api(path,body){{const o=body?{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}}:{{}};const r=await fetch(path,o);return r.json();}}
function cell(t){{const td=document.createElement('td');td.textContent=t==null?'':t;return td;}}
async function hostSet(){{
 const r=await api('/api/host-set-many',{{hosts:$('h_hosts').value,headers:$('h_hdr').value}});
 let msg = r.error ? ('error: '+r.error) : ('set on '+r.count+'/'+r.total+' entr'+(r.total==1?'y':'ies')+' ✓');
 if(r.results){{const bad=r.results.filter(x=>!x.ok).map(x=>x.host); if(bad.length) msg+=' — failed: '+bad.join(', ');}}
 $('h_msg').textContent=msg;$('h_msg').className=(r.ok?'ok':'bad');refreshHosts();}}
async function hostDel(host){{await api('/api/host-del',{{host}});refreshHosts();}}
async function hostPatDel(pat){{await api('/api/hostpat-del',{{pattern:pat}});refreshHosts();}}
function hostRow(tb,label,kind,headers,onDel){{const tr=document.createElement('tr');
 tr.appendChild(cell(label));tr.appendChild(cell(kind));
 tr.appendChild(cell(Object.entries(headers).map(([k,v])=>k+': '+v).join('\\n')));
 const td=document.createElement('td');const b=document.createElement('button');b.textContent='delete';b.className='danger';b.onclick=onDel;td.appendChild(b);tr.appendChild(td);tb.appendChild(tr);}}
async function refreshHosts(){{
 const r=await api('/api/hosts');const tb=$('h_tbl').querySelector('tbody');tb.innerHTML='';
 (r.patterns||[]).forEach(p=>hostRow(tb,p.pattern,'+ subdomains',p.headers,()=>hostPatDel(p.pattern)));
 (r.hosts||[]).forEach(h=>hostRow(tb,h.host,'exact',h.headers,()=>hostDel(h.host)));}}
async function handleMint(){{
 const r=await api('/api/handle-mint',{{host:$('p_host').value.trim(),path_prefix:$('p_path').value.trim()||'/',methods:$('p_meth').value,ttl:$('p_ttl').value,headers:$('p_hdr').value}});
 $('p_minted').textContent=r.ok?('handle (give to the sandbox as X-Sandbox-Handle): '+r.handle):('error: '+r.error);
 $('p_minted').className=r.ok?'ok mono':'bad';refreshHandles();}}
async function handleRevoke(h){{await api('/api/handle-revoke',{{handle:h}});refreshHandles();}}
async function refreshHandles(){{
 const r=await api('/api/handles');const tb=$('p_tbl').querySelector('tbody');tb.innerHTML='';
 (r.handles||[]).forEach(h=>{{const tr=document.createElement('tr');
  tr.appendChild(cell(h.handle.slice(0,10)+'…'));tr.appendChild(cell(h.host));tr.appendChild(cell(h.path_prefix));
  tr.appendChild(cell((h.methods||['any']).join(',')));tr.appendChild(cell(h.ttl+'s'));
  tr.appendChild(cell(Object.entries(h.headers).map(([k,v])=>k+': '+v).join('\\n')));
  const td=document.createElement('td');const b=document.createElement('button');b.textContent='revoke';b.className='danger';b.onclick=()=>handleRevoke(h.handle);td.appendChild(b);tr.appendChild(td);tb.appendChild(tr);}});}}
async function refreshLog(){{
 const r=await api('/api/log');const tb=$('l_tbl').querySelector('tbody');tb.innerHTML='';
 (r.log||[]).forEach(e=>{{const tr=document.createElement('tr');
  tr.appendChild(cell(new Date(e.ts*1000).toLocaleTimeString()));
  const d=cell(e.decision);d.firstChild&&(d.firstChild.remove());const sp=document.createElement('span');sp.className='pill '+e.decision;sp.textContent=e.decision;d.appendChild(sp);tr.appendChild(d);
  tr.appendChild(cell(e.method));tr.appendChild(cell(e.host));tr.appendChild(cell(e.path));
  tr.appendChild(cell((e.injected||[]).join(', ')));tb.appendChild(tr);}});}}
refreshHosts();refreshHandles();refreshLog();setInterval(refreshLog,4000);
</script>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True, help="gateway control unix socket")
    ap.add_argument("--log", required=True, help="redacted request-log file to tail")
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    with open(args.password_file) as f:
        password = f.read().strip()
    if not password:
        sys.exit("webui: empty password file")

    App.cfg = {
        "socket": args.socket,
        "log": args.log,
        "password": password,
        "session": secrets.token_urlsafe(32),
    }
    httpd = ThreadingHTTPServer((args.host, args.port), App)
    print(f"webui: http://127.0.0.1:{args.port}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
