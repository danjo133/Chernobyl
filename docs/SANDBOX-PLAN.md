# Reusable Claude Code Sandbox — Design & Plan

> A re-usable, hardened devcontainer for running Claude Code (incl.
> `--dangerously-skip-permissions`) against untrusted-ish autonomous work, with:
> - an **external** egress firewall/proxy that traffic *must* traverse (not an
>   in-container firewall the workload could tamper with);
> - a **host-side gitignore-driven FUSE filter** so secrets never enter the
>   container in the first place;
> - a **credential broker** so the container can *use* credentials (Anthropic,
>   GitHub, Kubernetes, cloud) without ever *holding* the real secret.

Status: **design approved, not yet built.** Target host is NixOS, runtime Docker
(rootless preferred) with Podman deltas noted.

---

## 1. Goals & non-goals

**Goals**
- Run Claude Code contained, so a bypassed/auto session can do bounded damage.
- Egress restricted to an explicit domain allowlist, enforced *outside* the
  workload's trust boundary.
- Sensitive files (secrets, anything git-ignored) never bind into the container.
- Credentials are *usable from inside* but *not readable/exfiltratable* from inside.
- Drop-in re-usable across repos, from both VS Code Dev Containers and a headless CLI.

**Non-goals**
- Defending against kernel/container-runtime 0-days. (Dev containers are not a
  hard sandbox; see Anthropic's own warning.)
- Preventing *misuse of access that has been granted*. We prevent credential
  **theft**, not authorized **use** — see the threat-model table (§7).

---

## 2. Architecture overview

```
                    ┌──────────────────────────────────────────────────┐
   HOST             │  gateway container — the firewall + credential     │
 ┌──────────┐       │  broker (the only path to the internet)            │
 │ ~/dev/   │       │  • NET_ADMIN/NET_RAW  • mitmproxy (transparent)    │──┐ egress
 │  myrepo  │       │  • iptables REDIRECT 80/443 → mitmproxy            │  │ network
 └────┬─────┘       │  • allowlist + scope-checked credential injection │  │ (internet)
      │ FUSE        │  • redis/vault: handle→real cred, scope, TTL       │──┘
      │ (gitignore  │  • flow log (redacted for auth)                    │
      │  filter,    └───────────────────────▲────────────────────────────┘
      │  host-side) shared net namespace     │
 ┌────▼─────────┐   ┌───────────────────────┴────────────────────────────┐
 │/run/.../view │──►│  devcontainer — the workload                        │
 │ filtered repo│bind│  network_mode: service:gateway                     │
 └──────────────┘   │  • NO net caps — cannot read/alter egress rules     │
                    │  • Claude Code + toolchain                          │
   HOST-side filler │  • holds opaque handles / nothing — never real creds│
   tool / MCP ─────►│  • trusts mitmproxy CA                              │
   (runs real OAuth)└──────────────────────────────────────────────────────┘
```

**Two structural decisions carry the security model:**

1. **`network_mode: "service:gateway"`** — the workload *shares the gateway's
   network namespace* instead of owning one. The gateway's `iptables`/mitmproxy
   govern the workload's traffic, but the workload holds **no `NET_ADMIN`/
   `NET_RAW`**, so it cannot read, flush, or rewrite those rules. The firewall is
   genuinely *outside* the workload's reach — strictly stronger than the
   reference design, where the firewall lives inside the same container Claude
   runs in.

2. **The gateway is a credential broker, not just a filter.** The real
   credential never enters the workload. The gateway injects/swaps it on egress
   from a store the workload cannot reach. The human authenticates *outside* the
   container (browser, MFA, hardware keys); the container only ever *uses* the
   result through the gateway.

---

## 3. Components

### 3.1 Gateway = firewall + credential broker (`.devcontainer/gateway/`)

| File | Purpose |
|---|---|
| `Dockerfile` | mitmproxy + iptables + redis client + entrypoint |
| `entrypoint.sh` | enable `ip_forward`, install REDIRECT+NAT rules, start redis (or attach to redis svc), exec mitmproxy |
| `broker_addon.py` | mitmproxy addon: allowlist + scope-checked credential injection / phantom-token swap + confused-deputy guards + redaction |
| `allowed-domains.txt` | editable egress allowlist |
| `scopes.yaml` | per-handle / per-host scope rules (host, path, method, TTL) |

- **Transparent MITM:** `iptables -t nat … -j REDIRECT` sends all forwarded
  80/443 to mitmproxy (excluding mitmproxy's own uid to avoid loops). Because it's
  transparent, **every** TLS client is captured — `git`, `npm`, `pip`, `node`,
  `curl`, `kubectl` — no app-level proxy cooperation required. UDP/443 (QUIC) and
  raw TCP to other ports are dropped, so nothing sidesteps interception.
- **CA persistence:** mitmproxy's CA lives in a named volume (`gateway-ca`) so it
  is stable across rebuilds; otherwise the workload would need re-trusting every
  rebuild.
- **Credential store:** `redis`/KeyDB (or references into a vault) on a network
  the workload cannot reach. Holds `handle → {upstream, real_secret_ref, scope,
  expiry}` and/or `host → injected_headers`.

> **The gateway is now the crown-jewel component.** In MITM mode it assembles
> plaintext credentials. Protect it accordingly: redis isolated from the workload,
> no body/auth-header logging, secrets encrypted at rest or held in a vault with
> redis storing only references. This concentrates secrets by design — that store
> must be the *most*-hardened part of the system, not the least.

### 3.2 Devcontainer = the workload (`.devcontainer/devcontainer.Dockerfile`)

- Non-root `remoteUser`; `no_new_privs`, `cap_drop: ALL`.
- Your toolchain + Claude Code, pinned via npm, `DISABLE_AUTOUPDATER=1`.
- **Trust the mitmproxy CA everywhere** (more than one knob): OS trust store,
  `NODE_EXTRA_CA_CERTS`, `git http.sslCAInfo`, `npm config cafile`,
  `PIP_CERT`/`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`. CA read from the shared
  `gateway-ca` volume in `postCreate` so it exists before first use.
- `~/.claude` on a per-project named volume (`…-${devcontainerId}`) for
  auth/session persistence across rebuilds.
- Holds **opaque handles or nothing** — never the real credential.

### 3.3 Host-side FUSE filter — gitignore-driven (`.devcontainer/fusefilter/`)

- A passthrough FUSE fs (Go preferred for a static, dependency-free binary; Python
  `pyfuse3` fallback) presenting `~/dev/myrepo` as a **filtered view**.
- **Visibility rule:** a path is visible iff `git` would *not* ignore it
  (evaluated via libgit2 / `git check-ignore`), **minus a hard-deny layer** for
  sensitive-even-if-tracked files: `.env*`, `*.pem`, `*.key`, `id_*`, `.aws/`,
  `.ssh/`, `.git-credentials`, token-bearing `.npmrc`. The hard-deny matters
  because gitignore alone won't hide a *committed* secret.
- **Runs on the host**, so the workload needs zero FUSE privileges — it only sees
  the already-filtered mount, bind-mounted at `/workspace`. Writes pass through to
  the real repo; files Claude creates persist unless they match an ignore rule.
- `.git/` stays visible by default (needed for gitignore evaluation and for Claude
  to use git); option to hide it documented. Git auth flows through the broker, not
  stored creds.
- **Host prereq (rootful Docker only):** the daemon runs as root and reading a
  user FUSE mount needs `user_allow_other` (`programs.fuse.userAllowOther = true;`
  on NixOS) + `-o allow_other`. **Rootless Docker / Podman avoids this** — the
  daemon runs as you, so the bind "just works." Hence the rootless recommendation.

### 3.4 Wiring — VS Code + headless CLI

- `compose.yaml`: `gateway` + `devcontainer` (+ `redis`) services; the workload via
  `network_mode: service:gateway`.
- `devcontainer.json`: `dockerComposeFile` + `service: devcontainer`, plus a
  host-side **`initializeCommand`** that mounts the FUSE view before the container
  starts (and a teardown that unmounts). VS Code "Reopen in Container" works directly.
- `sandbox` CLI wrapper: mount filter → `docker compose up` → `exec claude`,
  parameterized by target repo path + allowlist, for headless/scriptable use.

### 3.5 Policy (`managed-settings.json` → `/etc/claude-code/managed-settings.json`)

- Workload is network-contained + file-filtered, so `--dangerously-skip-permissions`
  is reasonable here.
- Telemetry off (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`) so statsig/sentry
  need not be allowlisted.

---

## 4. Credential broker model (the core of the egress design)

The gateway generalizes two well-known techniques, repurposed defensively:

- **mitmproxy credential capture/replay** (pentest) → defensive **auth injection**:
  the workload sends bare requests; the gateway attaches the credential on egress.
- **OAuth phantom-token / token-handler pattern** (redis-backed swap; aka BFF
  token handling) → the workload carries an **opaque handle**; the gateway swaps it
  for the real token at the edge, after a scope check.

### 4.1 Authenticate outside, use inside

```
HOST (you + browser + MFA)            GATEWAY trust domain                  UPSTREAM
┌────────────────────────┐     ┌───────────────────────────────┐
│ run real OAuth/login    │     │ redis/vault: handle→real cred   │
│ flows manually  ────────┼────►│  + scope + TTL + refresh token  │
│ (or an MCP broker does) │     │                                 │
└────────────────────────┘     │ mitmproxy broker_addon:         │
                               │  • resolve handle / host        │──► api.anthropic.com
WORKLOAD (Claude)              │  • check scope (host/path/      │──► api.github.com
┌────────────────────────┐     │    method/expiry)               │──► kube-apiserver
│ holds: opaque handle    │────►│  • swap in real Authorization   │──► cloud APIs
│   OR nothing at all     │     │  • deny + log on scope miss     │
│ NEVER the real cred     │     │  • refresh out-of-band          │
└────────────────────────┘     └───────────────────────────────┘
```

The interactive/risky auth happens where the browser, MFA, and long-lived secrets
live (the host). The container — which may run autonomous code — is never trusted
with the flow. A host-side tool or **MCP server** runs the flow, writes the real
cred into the broker, and mints the scoped handle dropped into the container (env
var, kubeconfig field, `.netrc`). Claude may *request* a handle from the broker and
get back only a scoped handle, never the underlying secret.

### 4.2 Two injection modes (choose per upstream)

1. **Inject-by-host** (simplest): workload sends bare requests; addon attaches the
   cached cred for that host from redis. Workload holds **nothing**.
2. **Phantom handle** (scoped): workload carries a high-entropy opaque token; addon
   resolves it in redis to the real token only after validating `host + path +
   method + not-expired`. The handle is **inert if exfiltrated** — meaningless
   directly upstream, only works *through the gateway*, for one purpose, revoked by
   deleting the redis key.

### 4.3 OAuth refresh (why phantom tokens shine)

The **refresh token stays in the broker**, never near the container. The broker
refreshes the access token out-of-band and updates redis; the gateway always
injects the current valid token. The container's handle never changes and never
visibly expires, yet it is backed by rotating, short-lived real tokens.

---

## 5. Kubernetes access

**Do not setuid `kubectl`.** It is a false boundary: a setuid `kubectl` whose argv
the workload controls will disclose the credential —
`kubectl config view --raw` prints the whole kubeconfig; `kubectl <x>` execs a
workload-planted `kubectl-x` plugin with the privileged euid; `--kubeconfig`/
`KUBECONFIG`/`--token` are all attacker-controlled. It also fights hardening:
setuid is ignored on `nosuid` mounts and disabled entirely under `no_new_privs`
(which we set). `sudo`-to-kube has the same argv-injection problem.

**Use the broker instead** (preferred for token-based clusters — EKS/GKE/AKS):

- Claude's kubeconfig has the server URL and a **phantom handle / no token**.
- The gateway injects the real bearer token (from redis, refreshed out-of-band) for
  the apiserver host.
- The token never enters the workload; revoke by deleting the redis key.

**Fallback for mTLS (client-cert) clusters** — `kubectl proxy` sidecar:

```
kubectl proxy --port=8001 \
  --reject-methods='POST,PUT,PATCH,DELETE' \
  --reject-paths='^/api/v1/namespaces/[^/]+/(secrets|pods/[^/]+/exec)'
```

A sidecar (different user/container) holds the kubeconfig (mode `600`, unreadable
by Claude) and authenticates upstream; Claude's kubeconfig points at
`http://localhost:8001` with no credentials. `--reject-*` constrains verbs/paths.

Either way, **scope the cluster identity with least-privilege RBAC** (read-only,
namespace-scoped) and prefer short-lived tokens — see §7.

---

## 6. Egress allowlist (initial)

`api.anthropic.com`, `claude.ai`, `platform.claude.com`, `downloads.claude.ai`,
`raw.githubusercontent.com`, `registry.npmjs.org` (+ npm CDN),
`github.com` / `api.github.com` / `codeload.github.com` / `*.githubusercontent.com`,
the kube apiserver host, plus per-toolchain (`pypi.org`, `files.pythonhosted.org`, …).
Editable file. Telemetry domains intentionally **omitted** (disabled instead).

---

## 7. Threat model — what each technique buys (and doesn't)

| Technique | Theft prevented | Use still possible | Residual risk / required mitigation |
|---|---|---|---|
| **External firewall (shared netns, no caps)** | Workload can't disable/rewrite egress rules | Reaching allowlisted hosts | Exfil *to* an allowlisted host (e.g. push to attacker's GitHub repo). Tighten allowlist; audit via MITM log. |
| **MITM allowlist** | Non-allowlisted egress blocked | Any allowlisted host | Gateway sees plaintext → protect gateway logs/store. Body-logging off by default. |
| **Host gitignore FUSE filter** | Secrets / ignored files never bind in | n/a | Committed secrets need the **hard-deny layer**; gitignore alone won't hide them. |
| **Credential broker — inject-by-host** | Real cred never enters workload | Any request to that host while live | Confused-deputy (see below). Workload holds nothing, so nothing to steal. |
| **Credential broker — phantom handle** | Real cred never enters; handle inert if leaked | Requests within `host+path+method` scope until TTL | Handle must be unguessable, single-purpose; scope is the real lever. |
| **OAuth refresh in broker** | Refresh token never near container | Access within scope until revoked | Broker is now the high-value target; harden + revoke path. |
| **Kube via broker / `kubectl proxy`** | Kubeconfig/token never readable by workload | Any API call the bound RBAC allows (incl. `get secret`, `exec` if permitted) | **Least-privilege RBAC + short-lived tokens.** Cluster-authz is a separate axis from credential theft. |

**The load-bearing caveat:** every technique above prevents credential **theft**,
not authorized **use**. While a handle/identity is live, Claude can do whatever its
*scope* permits. Scope (allowlist tightness + RBAC + path/method limits + TTL) is
the real lever; the broker shrinks blast radius, it does not make use safe.

### Confused-deputy (the subtle failure of any auth-injecting proxy)

The gateway attaches creds based on **destination**. If the workload can influence
the destination — DNS rebinding, an open redirect on an allowed host, an allowlisted
host that proxies onward, a path mimicking another endpoint — it can trick the
gateway into stapling a credential onto a request it shouldn't. **Mitigations,
designed in from the start:**

- Resolve DNS **in the gateway**; never trust workload-supplied resolution.
- Pin SNI/IP for credentialed hosts.
- Scope handles to exact `host + path + method`.
- **Never inject on redirects.**
- Keep `broker_addon.py` small, deny-by-default, well-tested; log every inject/deny.

> If you ever want the rigorous "the workload proves who it is to obtain a
> credential" version: SPIFFE/SPIRE workload identity + token exchange. Overkill for
> a personal setup, but the same shape scaled up.

---

## 8. Residual risks (summary)

1. Dev containers aren't a hard sandbox; trust the host and the runtime.
2. MITM gateway sees plaintext creds — it is the crown-jewel component; protect its
   store and logs, redact auth.
3. `--dangerously-skip-permissions` can still write workspace files (host-visible via
   the filtered bind) and exfil *to allowlisted domains*. MITM log is the audit trail.
4. Committed secrets rely on the FUSE hard-deny layer, not gitignore.
5. Cluster/cloud authz (RBAC/IAM) limits *use* and is orthogonal to credential theft
   — scope it least-privilege.

---

## 9. Host prerequisites & current blocker

- Runtime: **rootless Docker** (recommended) or Podman; `docker compose` /
  `@devcontainers/cli`.
- `fuse3` + (rootful only) `user_allow_other`
  (`programs.fuse.userAllowOther = true;` on NixOS).
- VS Code + Dev Containers extension (for the editor path).

> **Current blocker — Bash is non-functional in this Claude Code session.** Every
> command returns `bwrap: Can't create file at /run/wrappers/bin: No such file or
> directory` — Claude Code's own bubblewrap command-sandbox failing to initialize on
> NixOS. File writes work; building images, mounting FUSE, and running tests do not,
> until resolved. Likely fixes: ensure `/run/wrappers/bin` exists (NixOS setuid
> wrapper path — a missing one suggests an incomplete activation or running outside a
> proper NixOS session), or disable Claude Code's bash sandbox (`/sandbox`, or the
> sandbox setting) so commands run unwrapped.

---

## 10. Proposed file layout

```
.devcontainer/
  devcontainer.json          # service=devcontainer, dockerComposeFile, initializeCommand, postCreate
  compose.yaml               # gateway + devcontainer + redis; networks
  devcontainer.Dockerfile    # workspace image: tools + claude-code + trust proxy CA
  gateway/
    Dockerfile               # mitmproxy + iptables + redis client + entrypoint
    entrypoint.sh            # ip_forward, REDIRECT+NAT rules, start mitmproxy
    broker_addon.py          # allowlist + scope-checked injection/phantom-swap + guards + redaction
    allowed-domains.txt      # editable egress allowlist
    scopes.yaml              # per-handle / per-host scope rules
  fusefilter/
    gitignore-fuse(.go|.py)  # host-side filtered view
    mount.sh / unmount.sh    # helpers called by initializeCommand
  managed-settings.json      # → /etc/claude-code/managed-settings.json
  build-dirs.txt             # per-project list of build/dep dirs to back with volumes
  .env.example
broker/                       # host-side / MCP credential filler (runs real OAuth, fills redis, mints handles)
sandbox                       # CLI: up | ls | down | open — worktree, project naming, ports, resource limits
docs/SANDBOX-PLAN.md          # this document
```

---

## 11. Multiple concurrent sandboxes

Working on several things at once = several independent sandboxes running in parallel.

- **One gateway per sandbox — forced and preferred.** `network_mode: service:gateway`
  makes gateway↔workload 1:1, so N tasks = N gateway+workload pairs, each its own
  compose *project* (`COMPOSE_PROJECT_NAME`), netns, allowlist, MITM log, and broker
  partition. A compromised workload shares a netns only with *its* gateway — it never
  sees another sandbox's traffic or handles. Cost: mitmproxy ~50–100 MB + redis ~10 MB
  per sandbox; fine for a handful in parallel.
- **Shared gateway (many workloads) is *not* the default.** It would force a routed
  model (each workload on its own internal net, gateway NATs, per-source-IP
  attribution) — more complex, weaker isolation, harder per-workload scoping. Only
  worth it under real resource pressure.
- **Same repo, multiple tasks → git worktrees.** Two sandboxes must not write the same
  working tree (FUSE passthrough writes back to the host repo → clobber). Each sandbox
  gets its own `git worktree` (separate dir + branch, shared object store); its FUSE
  mount targets that worktree. Different repos are trivially independent. The `sandbox`
  CLI manages worktree lifecycle.
- **Credentials: authenticate once, isolated use.** Shared host-side broker *store*,
  but the filler mints **per-sandbox scoped handles** — sandbox A's handle resolves
  only to repo-A's creds. Each gateway authenticates as its sandbox and resolves only
  its own partition.
- **Ports & resources:** dynamic host-port assignment per sandbox (no collisions; see
  §12), per-sandbox CPU/mem limits.
- **CA:** one shared trusted CA (simpler images) or per-sandbox ephemeral (tighter);
  default shared.
- **Management:** `sandbox up | ls | down | open` handles project naming, worktrees,
  port offsets, and limits.

---

## 12. Local development — building & running

**The firewall is egress-only.** Loopback, compilation, local dev servers, test
runners, and intra-sandbox traffic are untouched — the gateway only gates connections
*leaving to the internet*. Running the app on `:3000` and hitting it never involves
the firewall.

**Fetching dependencies (the one real friction).** Every toolchain registry must be
allowlisted — `registry.npmjs.org`, `pypi.org`/`files.pythonhosted.org`,
`crates.io`/`static.crates.io`, `proxy.golang.org`/`sum.golang.org`, apt mirror, etc.
By design. The **MITM deny-log shows exactly what was blocked** → add the line,
hot-reload. Two MITM specifics:
- Package managers must **trust the proxy CA**: `npm config cafile`, pip
  `PIP_CERT`/`REQUESTS_CA_BUNDLE`, cargo/go via the OS trust store.
- **Cert-pinned hosts break under MITM** → the gateway supports per-host
  **passthrough (splice)**: allow the domain without decrypting. The allowlist has two
  tiers — *allow+inspect* (default) and *allow+passthrough* (pinned hosts).

**Build artifacts & deps — intersects the FUSE filter.** Gitignore-driven filtering
would *hide* `node_modules/`, `target/`, `.venv/`, `dist/` (they're ignored), yet the
container must build into them. Rather than a routing FUSE, use the standard
devcontainer trick: **mount a per-sandbox named volume over each build/dep dir**
(listed in `build-dirs.txt`). One move gives a writable persistent build dir, keeps
the host's platform-specific artifacts out of the container (no ABI mismatch), makes
builds fast across restarts, and isolates parallel sandboxes. The FUSE filter stays a
simple *source* read-filter + secret hard-deny; build dirs are volumes on top.

**Reaching the dev server from the host.**
- VS Code auto-forwards listening ports (tunnels over `docker exec`, so it works
  regardless of `network_mode`) and picks a free host port per container — **parallel
  sandboxes don't collide.**
- **CLI gotcha:** with `network_mode: service:gateway` you cannot put `ports:` on the
  workload service — published ports must be declared on the **gateway** service (it
  owns the interfaces). The `sandbox` CLI does this and assigns a per-sandbox port
  offset.

**Dev-dependency containers (local Postgres/Redis for the app).** Put them in the same
sandbox sharing the gateway netns. Uniform rule: **everything inside one sandbox talks
over `localhost`**; egress still only via mitmproxy. The app reaches its dev DB at
`localhost:5432` — no egress, firewall irrelevant.

**Inbound from the internet (webhooks, OAuth callbacks, tunnels).** A deliberate hole:
a tunnel is an egress connection that bridges inbound traffic. Allowlist the tunnel
provider explicitly and treat it as a known exception, not a default.

---

## 13. IDE integration

**VS Code — first-class.** Compose devcontainers, host-side `initializeCommand` (our
FUSE-mount hook), `network_mode: service:gateway` (VS Code attaches over `docker exec`,
so shared-netns is transparent), and port forwarding (tunneled over `docker exec`,
auto free host port per container) are all fully supported. The Claude Code extension
runs inside the container. Caveats: `initializeCommand` fires every reopen → make the
FUSE mount idempotent; there is **no host-side teardown hook** → the `sandbox` CLI
cleans up stale mounts.

**JetBrains — works, with caveats.** Dev container support is a spec *subset*; the two
risky pieces are ours:
1. Host-side `initializeCommand` support is historically spotty — if it doesn't run,
   the FUSE view never mounts. Main integration risk.
2. The JetBrains **backend downloads into the container** (heavier than VS Code's
   server) and makes plugin/license calls — `download.jetbrains.com`,
   `cache-redirector.jetbrains.com`, `plugins.jetbrains.com`, `account.jetbrains.com`
   — which must trust the MITM CA and be allowlisted, or be **pre-baked** into the image.
3. Multi-service compose devcontainers are less battle-tested than in VS Code.

**Decoupling move (makes IDE choice nearly irrelevant): CLI orchestrates, IDE
attaches.** The `sandbox` CLI is the source of truth — it creates the worktree, mounts
FUSE, and brings up compose. The IDE then **attaches to an already-running container**
(VS Code "Attach to Running Container"; JetBrains attach flow), sidestepping each IDE's
devcontainer-lifecycle gaps. Keep `devcontainer.json` for the VS Code one-click path;
the CLI-orchestrates/IDE-attaches path is the portable fallback for VS Code, JetBrains,
or terminal-only Claude Code.

**Regardless of IDE:** the IDE's server/backend runs *inside* the container and egresses
through the gateway → **pre-bake it into the image** (reproducible, smaller allowlist)
or allowlist its download/marketplace endpoints.

---

## 14. Phased implementation

- **P0** Host prereqs + fix the Bash/bwrap blocker (§9).
- **P1** Gateway image: mitmproxy + iptables REDIRECT/NAT + allowlist addon. Verify
  egress allow/deny standalone.
- **P2** Compose + `network_mode: service:gateway` + CA trust. Verify the workload
  reaches only allowlisted hosts and holds no net caps.
- **P3** Devcontainer image + Claude Code + managed-settings + `~/.claude` persistence.
- **P4** Host FUSE gitignore filter + `initializeCommand` wiring. Verify secrets
  excluded, writes pass through, hard-deny works on committed secrets.
- **P5** Credential broker: redis store, `broker_addon.py` injection/phantom-swap +
  confused-deputy guards, host-side/MCP filler tool. Verify real creds never enter
  the workload (inspect env/files/memory), handles are scoped + revocable.
- **P6** Kube access: broker injection for token clusters; `kubectl proxy` sidecar
  fallback for mTLS; least-privilege RBAC.
- **P7** Local-dev ergonomics: per-sandbox build-dir volumes (§12), CA trust for
  package managers, allow+passthrough tier for cert-pinned hosts, port forwarding
  (gateway-service `ports:` + VS Code auto-forward). Verify a real build + dev server.
- **P8** Concurrency: compose project naming, git-worktree lifecycle, per-sandbox
  broker partitions + scoped handles, dynamic port assignment, resource limits.
  Verify two sandboxes run isolated in parallel.
- **P9** Re-usability (`sandbox up|ls|down|open`) + IDE integration (§13): VS Code
  one-click path, CLI-orchestrates/IDE-attaches fallback for JetBrains/terminal,
  pre-bake IDE backend into the image + docs + allowlist tuning.
- **P10** Hardening + tests: egress-denial, secret-leak, credential-non-readability,
  confused-deputy, cross-sandbox isolation, rebuild-persistence.
