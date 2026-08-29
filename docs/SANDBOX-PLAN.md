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
- **Owner rewriting (`-uid` / `-gid`):** the workload runs as uid 1000 (`node`), and
  the filter used to report each file's real owner. On a host where the invoking user is
  **not** uid 1000, every file in `/workspace` then looked alien and the agent could not
  write it — git, node and editors all `stat()` before they write. `gitignore-fuse -uid N
  -gid N` makes the view *report* that owner for every file; `mount.sh` passes
  `SANDBOX_FS_UID`/`SANDBOX_FS_GID` (default `1000`, so hosts where the user IS uid 1000
  see no change), and `sandbox up --fs-uid N --fs-gid N` persists them into the
  per-sandbox env file. Set both to `0` to report the true owner.
  Only the *view* lies: the daemon keeps doing the real syscalls as the invoking host
  user, so files land on disk owned by them and stay usable outside the sandbox. This
  works because the mount does **not** use `default_permissions`, so the kernel delegates
  every access check to the daemon rather than testing uid 1000 against the reported
  owner. An idmapped bind mount under the filter is NOT an alternative: the daemon's own
  uid is unmapped there, so every write fails with `EOVERFLOW`.
  Note `fs.Options.UID`/`GID` in go-fuse cannot do this — go-fuse applies them only when
  the underlying uid is `0` — so the override is applied per node, in each attr-returning
  op (`Lookup`, `Getattr`, `Setattr`, `Create`, `Mkdir`, `Mknod`, `Symlink`, `Link`).

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
Telemetry domains intentionally **omitted** (disabled instead).

The live file is **per-sandbox** (`sandboxes/<name>/allowlist.txt` in the state dir,
§10.1, seeded from this base on first `up`) and **hot-reloaded** by `broker_addon.py` on change. Add entries to a *running*
sandbox with `sandbox allow --name <n> --url DOMAIN | --ip ADDR` — no teardown, so broker
credentials (which are memory-only; see §8) are not lost. `sandbox up --allow-url …` seeds
and appends (deduped); it does not clobber entries added live.

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
6. Broker creds are **memory-only by design** (redis `--save "" --appendonly no`): nothing
   sensitive at rest, but `sandbox down` wipes them — re-enter via the broker UI after a
   real teardown. Use `sandbox allow` (hot-reload) for allowlist changes so you don't tear
   down needlessly. Opting into AOF persistence trades this for plaintext tokens on disk.

---

## 9. Host prerequisites & current blocker

- Runtime: **rootless Docker** (recommended) or Podman; `docker compose` /
  `@devcontainers/cli`.
- `fuse3` + (rootful only) `user_allow_other`
  (`programs.fuse.userAllowOther = true;` on NixOS).
- **Unprivileged user namespaces enabled** (for rootless in-workload image builds):
  `kernel.unprivileged_userns_clone=1`, or NixOS `security.unprivilegedUsernsClone = true;`.
  Without it, Podman/Buildah inside the workload can't create the namespace a rootless
  build needs.
- VS Code + Dev Containers extension (for the editor path).
- Go (or Nix) **to build** the FUSE filter. A finished install ships the binary, so a
  machine that only *runs* sandboxes does not need Go.

`./install.sh --check` reports on all of the above without changing anything, and
splits them by consequence: a missing *required* tool means no sandbox can come up, a
missing optional one only disables a feature (`--worktree` needs git, `--policy` needs
yq or PyYAML). See §10.1 for what installing puts where.

> **NixOS caveat — Claude Code's own bash sandbox.** When *driving* this tool from a
> Claude Code session on a NixOS host, bubblewrap may fail to initialize: every command
> returns `bwrap: Can't create file at /run/wrappers/bin: No such file or directory`.
> File writes still work, but building images, mounting FUSE and running tests do not.
> Fixes: ensure `/run/wrappers/bin` exists (the NixOS setuid wrapper path — a missing
> one suggests an incomplete activation or a session started outside a proper NixOS
> environment), or disable Claude Code's bash sandbox (`/sandbox`, or the sandbox
> setting) so commands run unwrapped.
>
> This affects the **host** session only. A Claude Code running *inside* a sandbox is
> on the workload image, not NixOS, so it is unaffected.

---

## 10. Proposed file layout

```
.devcontainer/
  devcontainer.json          # service=devcontainer, dockerComposeFile, initializeCommand, postCreate
  compose.yaml               # gateway + devcontainer + redis; networks
  devcontainer.Dockerfile    # workspace image: tools + claude-code + trust proxy CA
  gateway/
    Dockerfile               # mitmproxy + iptables + redis client + entrypoint
    entrypoint.sh            # ip_forward, REDIRECT+NAT rules, start redis + control server + mitmproxy
    broker_addon.py          # allowlist + scope-checked injection/phantom-swap + guards + redaction + request log
    control_server.py        # gateway-side credential control endpoint (unix socket → redis); write-only/masked
    allowed-domains.txt      # editable egress allowlist
    scopes.yaml              # per-handle / per-host scope rules
  fusefilter/
    gitignore-fuse(.go|.py)  # host-side filtered view
    mount.sh / unmount.sh    # helpers called by initializeCommand
  managed-settings.json      # → /etc/claude-code/managed-settings.json
  build-dirs.txt             # per-project list of build/dep dirs to back with volumes
broker/                       # host-side credential tooling (runs real OAuth, fills redis, mints handles)
  fill.py                     # CLI filler (prints redis commands / future control-socket client)
  webui.py                    # host-only web UI (127.0.0.1, password-gated): manage creds + watch egress log
sandbox                       # CLI: up | ls | down | open — worktree, project naming, ports, resource limits
Makefile / install.sh         # installer: prerequisite checks, builds the FUSE filter, puts `sandbox` on PATH
docs/SANDBOX-PLAN.md          # this document
```

### 10.1 Installed layout — the tree is read-only at runtime

The repo above is the *source*. `make install` copies it to
`$PREFIX/lib/agent-sandbox` and symlinks `$PREFIX/bin/sandbox` at it (`PREFIX`
defaults to `~/.local`; `--system` uses `/usr/local`). `sandbox` resolves symlinks
before locating `.devcontainer/`, so the symlink is the supported entry point.

**The CLI writes nothing into its own directory.** That is the property that makes a
single system-wide install shareable: users never write to it, never collide, and
never see each other's sandboxes. Everything mutable is keyed per user, then per
sandbox:

```
$PREFIX/lib/agent-sandbox/            the install (read-only at runtime)
$SANDBOX_STATE_DIR/                   default $XDG_STATE_HOME/agent-sandbox
  current                             name used by verbs given no --name
  bin/gitignore-fuse                  built here only if the install shipped no binary
  sandboxes/<name>/
    env                               compose --env-file for THIS sandbox
    allowlist.txt                     per-sandbox egress allowlist (hot-reloaded)
    harness, model, *-port, ttl-...   scratch scalars: selection, ports, TTL clock
    webui-password, webui.pid         host-side broker-UI bookkeeping
    control/                          BROKER_CONTROL_DIR — the ONLY dir bind-mounted
                                      into the gateway (socket, request log, flywheel)
    tools-ssh/                        tools-sidecar SSH key (private; §16)
$SANDBOX_HOMES/                       default ~/.sandbox/homes — per-harness logins,
                                      bind-mounted into every sandbox (§17)
$XDG_RUNTIME_DIR/devfilter/<name>/    the FUSE-filtered view (§3.3)
```

Only `control/` crosses into a container, so host-side material — the broker UI
password, pids, the sidecar's private key — stays outside every bind mount by
construction rather than by convention.

The FUSE binary is built at install time and shipped in the tree, so target machines
need Go only if they build. If the install carries no binary and its directory is not
writable, `mount.sh` falls back to building into `$SANDBOX_STATE_DIR/bin`.

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
- **State: one directory per sandbox, outside the install.** Each sandbox owns
  `$SANDBOX_STATE_DIR/sandboxes/<name>/` (§10.1) holding its compose env file,
  allowlist, broker control dir and port/TTL bookkeeping. This replaced a single
  shared `.devcontainer/.env` rewritten by every `up`, which was last-writer-wins:
  bringing up a second sandbox rewrote the first one's compose environment, and the
  "current sandbox" that `down`/`open` acted on with no `--name`. Parallel sandboxes
  now share no mutable file.
- **Ports & resources:** dynamic host-port assignment per sandbox (no collisions; see
  §12), per-sandbox CPU/mem limits. Port claims live in each sandbox's own state dir
  and are scanned across all of them, so assignment stays collision-free per user.
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
cleans up stale mounts. A third: `initializeCommand` runs `mount.sh` *without* the
CLI, so nothing tells it which sandbox's env file to use — on that path it falls back
to the legacy in-repo `.devcontainer/.env` (§10.1). The VS Code flow therefore assumes
a writable checkout, not a shared read-only install; drive a shared install from the
CLI and let the IDE attach to the running container (see below).

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
- **P11** Broker management UI (§15): gateway control endpoint over the control socket,
  redacted egress request log, host-side password-gated web app. Verify the surface is
  unreachable from the workload, secrets are never read back, and CSRF/rebinding guards hold.

---

## 15. Broker management UI — set credentials, watch egress

A password-gated web app to manage credentials (inject-by-host headers, scoped phantom
handles) and watch the redacted egress log, instead of hand-piping `redis-cli`. The
design is dominated by one constraint, and getting it wrong inverts the whole model.

### 15.1 The load-bearing decision: the UI runs on the HOST, not the gateway

The workload shares the gateway's **network namespace** (`network_mode: service:gateway`).
So anything listening on a port *inside* the gateway is reachable from the workload at
`localhost`. A credential-management server there — even password-protected — would put a
secret-bearing, secret-*setting* surface one `curl localhost:PORT` away from the very
autonomous code we keep credentials from. **That inverts the threat model.**

So the web app runs as a **host process**, bound to the host's `127.0.0.1` — a different
loopback than the container's, which the workload cannot reach. It never publishes a port
into the gateway netns. It reaches the gateway only over a **unix control socket** in the
bind-mounted control dir. Both channels exploit that the workload shares the gateway's
*network* ns but **not** its *mount* ns:

```
 BROWSER ──► 127.0.0.1:9999 (HOST loopback — invisible to the workload)
                │  served by broker/webui.py, launched by the `sandbox` CLI
                ▼
   sandboxes/<name>/control/broker.sock   (host ⇄ gateway, NOT in the workload's mount ns)
   sandboxes/<name>/control/requests.log  (gateway appends redacted JSONL; host tails)
                │
   GATEWAY: control_server.py ──► redis (real creds)   broker_addon.py ──► request log
                ▲
   WORKLOAD shares the gateway NETNS but not its MOUNT ns → cannot see the socket,
   the log, or the host's :9999. Same argument that already protects redis.
```

### 15.2 Components

| Where | File | Role |
|---|---|---|
| gateway | `control_server.py` | listens on `/run/broker-control/broker.sock`; the only write path into the cred store. Ops: `host_set/list/del`, `hostpat_set/del` (wildcard/subdomain records), `handle_mint/list/revoke`. Runs as the unprivileged proxy uid (same as redis). |
| gateway | `broker_addon.py` | also appends **redacted** JSONL to `/run/broker-control/requests.log`: ts, decision (`ALLOW/DENY/INJECT/DENY_SCOPE/SPLICE`), method, host, path. Never header *values* (only injected names), never bodies, query string stripped. Size-capped ring. |
| host | `broker/webui.py` | stdlib-only web app, binds `127.0.0.1`. Password login → session cookie. Forms for host-headers (one domain per line — plain host = exact; `*.x.com`/`.x.com` = that domain + all subdomains, for non-secret markers across a bug-bounty scope) + handles; auto-refreshing log table. Talks to the control socket; tails the log. |

> **Exact vs wildcard injection.** Exact records (`broker:host:<host>`) stay strict — the
> confused-deputy guard for real credentials. Wildcard/subdomain records
> (`broker:hostpats` HASH, opt-in by typing `*.`/`.`) match a host *and all its subdomains*
> and are intended for **non-secret markers** (e.g. a bug-bounty `X-Tag`), since they
> broadcast the header to every subdomain reached. On a request both tiers are merged, the
> exact record winning on conflict — so a scope-wide marker and a host-specific token coexist.
| host | `sandbox` CLI | `up` mints the password (`sandboxes/<name>/webui-password`, mode 600 — kept beside the control dir, not inside it, so it is not in the gateway's bind mount; §10.1) and launches the UI on an **auto-assigned free port** (first free from 9999, skipping ports other sandboxes claim; stable across restarts via `webui-port`); `ls` prints each sandbox's URL + password + status; `down` stops it; `web [--stop]` (re)starts after a host reboot. `--web-port N` overrides. |

### 15.3 Security properties

- **Crown-jewel rule — write-only / show-masked.** The control server *can* read secrets
  (it must, to store them) but **never returns them**: list ops mask to a last-4 hint. You
  change a secret by overwriting, remove it by deleting/revoking. A freshly minted handle
  is shown **once** (it must be dropped into the sandbox); the backing header is masked.
  A leaked password can revoke/overwrite but cannot **exfiltrate** stored creds.
- **Authn + CSRF + anti-rebinding.** Per-`up` high-entropy password (600), constant-time
  compare, `HttpOnly; SameSite=Strict` session cookie. Every mutating POST checks `Origin`
  (hostname must be exactly `127.0.0.1`/`localhost` — **port-agnostic**, so SSH-forwarding to
  any local port still works; a cross-site attacker is never served from loopback); every
  request checks the `Host` header against the same loopback allowlist (defeats DNS-rebinding
  from a malicious page). Body size capped.
- **Reachability.** Socket + log live in the host-private control dir, never bind-mounted
  into the workload. The dir is made writable by the proxy uid (cross-uid bind mount, no
  userns) — a **single-user-host assumption**: loose dir perms only expose other *local*
  host users, who could already read the password file. Not multi-tenant-host safe.
- **Redaction.** The log is decisions, not contents — safe to leave tailing in a browser.

### 15.4 Remote access — SSH-forward, never bind to the wire

The UI binds **`127.0.0.1` on the devbox only**, deliberately. To reach it from a desktop,
**SSH local-forward** — do not bind it to `0.0.0.0`/the external IP:

```
ssh -L 9999:127.0.0.1:9999 devbox      # then browse http://localhost:9999 on the desktop
```

Why not bind externally: this is a credential-*setting* surface. Putting it on the LAN/
external interface makes the app password the only thing between the whole network and your
secret store, and forces a firewall hole. SSH-forwarding keeps it off the wire entirely —
transport auth is SSH (keys), the tunnel is encrypted, no firewall change, and the
loopback `Host`/`Origin` guards still pass (the forwarded request arrives as
`Host: localhost`). The port-agnostic `Origin` check (§15.3) means you may forward to any
local port (`-L 8080:127.0.0.1:9999`) without breaking CSRF protection.

Note on the workload: binding `0.0.0.0` would *not* directly expose the UI to the sandbox
(its egress is dropped by the gateway firewall to non-allowlisted, non-80/443 destinations),
but it would expose it to every other host on the network — which is reason enough.

### 15.5 Status

Built (P11), unit-smoke-tested host-side (auth, cookie flags, Origin/Host rejection, masked
read-back, control round-trip). Still needs a real `docker compose up` pass end-to-end with
the gateway — same standing blocker as the rest of the scaffold (§9). The redis-cli seeding
path (§4, `broker/README.md`) remains as a fallback and for scripting.

---

## 16. Bug-bounty tools sidecar (bb-hunter)

The hunting toolset (`tooling/docker/`, a curated Kali image — subfinder/httpx/nuclei/
ffuf/katana/dalfox/sqlmap/jadx/…) is a **separate, independently-built image**, brought
into the sandbox as an **opt-in sidecar** rather than merged into the workload image
(different base, heavy independent build, its own bare-VM install path).

**Topology.** `sandbox up --tools` layers `compose.tools.yaml`, adding a `tools` service:
`network_mode: service:gateway` (so tool egress goes through the firewall + broker, same
containment as the workload), `cap_drop: ALL`, `no-new-privileges`, `user: hunter`, the
same FUSE `/workspace`, the gateway public CA at `/ca`, and a shared `bb-hunter-wordlists`
volume. The standalone `tooling/docker/compose.yml` (with its own mitmproxy + full egress)
is unchanged; this is the integrated variant.

**Claude drives it over SSH.** The sidecar runs **dropbear as `hunter`** on
`127.0.0.1:2222` — reachable only from the sibling devcontainer over the shared gateway
netns. OpenSSH sshd is unusable here (its setuid privsep can't run under cap_drop:ALL +
no-new-privileges); dropbear serving its own user needs no setuid and no caps, and so
*cannot* setuid to the proxy uid to bypass the firewall. The CLI mints an ed25519 keypair
per sandbox (`.tools-<name>/`), injects the pubkey as the sidecar's `authorized_keys`, and
installs an ssh alias + a `hunter` PATH wrapper in the devcontainer. Claude runs
`hunter nuclei -u https://…` (the wrapper loads the gateway-CA env, then execs in the
sidecar); output lands in the shared workspace. The scope-guard hook still sees the target
host in the command, so it keeps gating.

**`sandbox tools`** drops *you* into the contained sidecar (`exec tools bash -l`) for manual
checks. **`sandbox tools --unrestricted`** spins up an ephemeral, **full-internet**
bb-hunter box (own netns, NET_RAW+NET_ADMIN, repo mounted) for manual `nmap`/`dns`/raw
recon the gateway would otherwise drop — deliberately *outside* containment, human-driven
only.

**Egress for tools (current scope: http/https/ws/wss).** Tool HTTP(S) goes through the
gateway like everything else: each target must be allowlisted, and traffic is MITM'd unless
the host is marked passthrough (`!host`). Recommended for scanning: **passthrough the scope**
(`!target.com`, wildcard-capable) so tools get real TLS and accurate results, and set any
marker header via the tools' own `-H` flags. Raw port-scanning (`nmap`/`naabu` on arbitrary
ports) is still dropped by the gateway — that needs the future scope-passthrough egress tier
(§15-style), deferred; use `--unrestricted` for now.

**Two scope gates, keep in sync.** `tooling/scope-guard.py` (agent PreToolUse hook,
`recon/active-scope.yaml`) and the gateway allowlist are complementary defense-in-depth;
ideally drive both from one scope source. The hook only fires for *Claude*-run commands —
a manual `sandbox tools` shell is gated by the gateway only.

**Status:** built, static-checked (shell/YAML/wrapper-quoting). Needs a real host bring-up
to validate the rootless-dropbear login, cross-uid key mounts, and `hunter` wrapper — the
in-sandbox session has no Docker socket. Prereq: pre-build `bb-hunter:latest`
(`cd tooling/docker && docker compose build`).

---

## 17. Harness abstraction — swapping the agent and the model

The sandbox was built around Claude Code, but nothing in the security model is
Claude-specific: the gateway does not care which process makes the request. Four
Claude-shaped assumptions were what actually tied the workload to one harness — the
image's `npm i -g @anthropic-ai/claude-code`, the `~/.claude` home bind, `sandbox open`
hardcoding `claude --dangerously-skip-permissions`, and `api.anthropic.com` in the
allowlist. §17 lifts those four into a registry.

**Two orthogonal axes.** *Harness* = which agent CLI runs (`claudecode`, `pi`, `omp`,
`prime-agent`). *Backend* = where tokens come from (`anthropic`, `ollama`,
`openai-compat`). They compose: `--harness pi --llm-backend anthropic` and
`--harness claudecode --llm-backend ollama` are both meaningful.

**The registry** (`.devcontainer/harness/`) is sourced by *both* the host CLI and the
workload, so the two can never disagree about what a harness is called or needs:

| file | defines |
|---|---|
| `harnesses/<name>.sh` | `H_BIN`, `H_HOME`, `H_DOMAINS`, `h_install`, `h_configure`, `h_launch` |
| `backends/<name>.sh` | `B_URL`, `B_OPENAI_URL`, `B_ANTHROPIC_URL`, `B_API`, `B_KEY`, `B_DOMAINS` |
| `install.sh` | build-time installer, one Dockerfile `RUN` per harness |
| `run.sh` → `sandbox-harness` | in-workload: configure, then exec the agent |
| `models_config.py` | renders the pi-family provider block (JSON or YAML) |
| `probe_models.py` | host-side: list models, pick a default, read context windows |

Two install-time facts the registry has to encode, both discovered by actually running
them: **omp's published binary is a bun bundle** (`#!/usr/bin/env bun`, `engines.bun >=
1.3.14`) — npm installs it but it will not run without bun, so its `h_install` installs
bun from npm first; and **prime-agent is not on the npm registry**, it ships as a
sha256-listed tarball in an R2 bucket, so it is fetched and verified directly rather than
through its `curl | sh` installer.

**All four harnesses are baked into the image** (one cache layer each). Selecting one is
a runtime decision passed per `docker compose exec -e`, so `sandbox open --harness omp`
switches agents with no rebuild, no restart, and no loss of broker credentials. Each gets
its own shared host home (`$SANDBOX_HOMES/<harness>-home`, Claude Code keeping its
historical `~/.sandbox/claude-home`), so a login or learned skill follows you into every
sandbox — the §11 property, per harness.

**Config generation.** pi, omp and prime-agent share one provider schema
(`providers.<name>.{baseUrl,api,apiKey,models[]}`), differing only in path and encoding —
so one writer serves all three. Two details are load-bearing:

- *Colon-free aliases.* Ollama ids (`qwen3.5:35b`) collide with the pi-family model
  selector, where `:` introduces a thinking level (`--model sonnet:high`). Every generated
  entry carries a `name` with `:` replaced, and that alias is what `--model` is given.
- *Real context windows.* An unrecognised model gets a default window (128k pi-family,
  200k Claude Code). The CLI probes `/api/show` at `up` and writes the true number
  (`contextWindow`, or `CLAUDE_CODE_MAX_CONTEXT_TOKENS`) — otherwise a 32k model is fed
  200k of context, or a 262k model is compacted at a quarter full.

**Preamble size is a harness-selection criterion for local models.** Measured on the wire
(same task, same backend): pi sends ~1,400 tokens before your first word (644 of system
prompt, 4 tool schemas at ~755); omp sends ~14,837 (5,060 of system prompt, 11 tool schemas
at ~9,777) — 10.6x. On a frontier API that is free; on a local model it is the difference
between a 32k window being roomy and being nearly full on arrival. So the harness choice
and the served context window are one decision, not two: pi is comfortable at 32k, omp
wants 64k+. The fast/subagent slot therefore defaults to the *same* model on a local
backend — a second resident model costs VRAM that the first model's KV cache needs, and
under `OLLAMA_MAX_LOADED_MODELS=1` it thrashes (every subagent call evicts and reloads the
main model). `--small-model` splits them when the box has room.

**The served window is not the model's window.** `/api/show` reports what a model was
*trained* for (262144 for qwen3.5:35b); the server serves whatever its own `num_ctx` says,
and Ollama's default is a few thousand tokens. Exceed it and the prompt is silently
truncated **from the front** — which is exactly where every harness puts its system prompt
and tool schemas. The model then receives style instructions and no tools, and does the
only thing left: it *narrates* tool calls as prose or invented XML (`<exec>`,
`<evidence-and-output>`) that no harness parses. Nothing errors; the transcript merely
looks like a model too weak to use tools. Measured on a server capped at 2048: a
6,657-token payload and a 14,856-token payload both came back `prompt_eval_count=2050`,
and the identical payload at `num_ctx=16384` evaluated 7,626 tokens and produced a correct
tool call — same model, same request.

So `sandbox up` measures the *served* window rather than trusting the advertised one:
`probe_models.py --effective-context` sends a filler prompt of known size and compares it
against the `usage.prompt_tokens` the backend reports back (an OpenAI-compat field, so this
is not Ollama-specific). A shortfall is a hard failure signal — the CLI warns with the fix
(`OLLAMA_CONTEXT_LENGTH` / `PARAMETER num_ctx`) and caps the sandbox's context to what the
server will actually accept, so the harness does not plan for a window that is being thrown
away. `--model-context N` pins the window by hand and disables the capping;
`--no-context-probe` skips the check (it costs one prefill of ~6k tokens at `up`).

A generated config carries a sidecar marker; a file we did not write is moved to
`<file>.pre-sandbox` rather than clobbered, and providers you add by hand survive
regeneration.

**Credentials are unchanged, and that is the point.** With `--llm-backend anthropic` a
pi-family harness gets a placeholder `ANTHROPIC_API_KEY` in the workload while the gateway
injects the real credential by host (§4) — injection overwrites the placeholder header, so
it never reaches the API. Claude Code keeps using the OAuth session in its shared home.
Local backends need no credential at all, which is a real reduction in exposure: with
`--harness pi --llm-backend ollama` there is no model credential in the system.

**Claude Code against a local model works** because Ollama ≥ 0.12 serves the Anthropic
Messages API at `/v1/messages`, so `ANTHROPIC_BASE_URL` can point straight at it — no
translating proxy in the path. For a generic `openai-compat` endpoint that guarantee does
not hold, so `claudecode` refuses it unless `--llm-anthropic-compat` asserts it.

**Egress follows the selection.** `--harness`/`--llm-backend` append their `H_DOMAINS` /
`B_DOMAINS` to the per-sandbox allowlist automatically, including the backend host parsed
out of `--llm-url`; `open` tops it up on a switch (hot-reloaded, no restart).

**Non-goals.** No harness-agnostic skills/agents layer — Minions agents and skills stay
wired to Claude Code only; the others have their own formats. No cross-harness session
migration.

**Note on permission systems.** pi deliberately ships none, and omp/prime-agent do not
enforce one either. Inside this sandbox that changes nothing: the container, the FUSE
filter and the gateway *are* the boundary, and Claude Code already runs
`--dangerously-skip-permissions` here for the same reason (§3.5). Outside a sandbox that
is a very different proposition.

---

## 18. Flywheel — capturing model traffic (opt-in)

"Flywheel" here is the loop *route → observe → evaluate → specialize → deploy*: run real
work against a strong model, keep the traces, and use them to fine-tune or evaluate a
local model on **your** distribution instead of on a public benchmark. §18 implements the
*observe* step; routing and training stay outside the sandbox.

**Why the gateway is the right place.** It already terminates TLS for every allowed host,
so one addon captures every model call from every harness against every backend, in one
shape. No harness plugin, no wrapper process, nothing for the workload to notice or
disable. `sandbox up --flywheel` sets `FLYWHEEL=1`, which makes the gateway entrypoint
load `flywheel_addon.py` alongside the broker addon (the security-critical addon is
untouched when the flag is off).

**What a record holds:** timestamp, host, path, model, user-agent (which is how a mixed
capture is split per harness), stream flag, status, duration, the full request JSON, and
the assembled completion — text, tool calls, usage, stop reason. Anthropic and OpenAI SSE
transcripts are reassembled into that same shape; `flywheel_addon_test.py` covers both.

**Explicitly not redacted.** Unlike `requests.log`, which is redacted by construction
(§15), these records contain prompts, source, tool output — whatever the agent saw. They
are written to the per-sandbox control dir on the **host**, which the workload cannot see
and git ignores. Request headers are *not* captured, so a broker-injected credential never
lands in the corpus — but a secret pasted into a prompt would. Treat the capture dir like
the source tree it mirrors. It is off by default for exactly this reason.

**Bounded on disk:** per-file rotation and a total budget, oldest first, so a long
autonomous run cannot fill the host disk.

**Reading it back:** `sandbox flywheel stats | tail | export`. Export emits OpenAI-messages
or ShareGPT JSONL. Two honest caveats: structured content (tool results, images) is
flattened to text, and a response streamed past mitmproxy's `stream_large_bodies` threshold
arrives empty — those records keep the request, are skipped by export, and are counted in
`stats` so the gap is visible rather than silent.

**Status:** addon + reader unit-tested; the capture path needs a real bring-up to confirm
volume under load. Routing (a local/frontier splitter) is deliberately not built — with
`--model`/`--small-model` you can already put the cheap slot on a local model and keep the
main one on a frontier model, which is most of the benefit without a new component inside
the security boundary.

---

## 19. Web search — one endpoint, four harnesses

Search is the capability the harness split exposed. Claude Code's `WebSearch` is a
**server-side Anthropic tool**, so it evaporates the moment `ANTHROPIC_BASE_URL` points at
a local model; pi ships no web tool at all (4 tools, by design); prime-agent has none
documented, only an IPython REPL that can call anything; and omp has a built-in
`web_search` with 23 providers. Four different stories for one need.

**The unifying decision: one search endpoint, outside the sandbox.** A SearXNG instance
runs on the LAN/server box (see `tooling/searxng/`), and the sandbox reaches it as a single
allowlisted host. Why not a sidecar — the obvious symmetry with the tools sidecar (§16) —
is the whole argument: a sidecar shares the gateway netns, so **SearXNG's own upstream
traffic would be subject to the allowlist and MITM**. That means allowlisting google,
duckduckgo, startpage, bing, … (a wide hole in the boundary this project exists to defend)
*and* feeding MITM'd, datacenter-shaped traffic to engines that bot-detect exactly that.
Outside, the scraping happens beyond the containment, one hostname goes on the allowlist,
and every sandbox — plus anything else on the network — shares the instance.

**How each harness gets it:**

| harness | mechanism |
|---|---|
| omp | native `web_search`, pointed at the same instance via `SEARXNG_ENDPOINT` |
| pi, prime-agent, claudecode | the `websearch` command baked into the workload image |

`websearch` (stdlib Python, in `.devcontainer/harness/`) queries the JSON API and prints
ranked title/URL/snippet. Every harness has `bash`, so this is the common denominator —
no per-harness plugin, no MCP server, no extension ecosystem to track. Its error messages
name the three failures a fresh SearXNG produces (JSON format disabled, limiter rejecting
non-browser clients, `method: POST` breaking the GET API) so they are diagnosable from
inside the sandbox rather than looking like "search is broken".

**Telling the model it exists.** A command on `PATH` is invisible to a model. All four
harnesses happen to read the *same* `SKILL.md` convention — Claude Code's, adopted by pi,
omp and prime-agent — so one generated skill in each harness home covers all of them. It is
written when `--search-url` is set and withdrawn when search is turned off, so the model is
never advertised a command that can only fail. One exception: Claude Code's `skills/` is
usually a symlink to the read-only Minions mount, so the skill is skipped there with a note
— add it to the Minions skills repo if you want local Claude Code to reach for search on
its own.

**Snippets only, deliberately.** Search returns snippets; reading a linked page needs that
domain allowlisted with `sandbox allow --url`. The tempting next step — a reader/fetch
proxy on the search host, or a "GET anywhere, never inject credentials" lane in the gateway
— is by construction an exfiltration channel: the URL carries the payload out. Both are
implementable behind their own opt-in flag, but neither ships, and the generated skill
explicitly tells the model not to route around the allowlist but to name the domain it
needs.

**Alternatives considered.** Brave Search API / Tavily / Exa are more reliable than
scraping and fit the broker beautifully (the key lives in the gateway and never enters the
workload) — the cost is money and a cloud dependency, so they stay a documented option
rather than the default. omp's keyless engines (DuckDuckGo, Startpage, Ecosia, Google,
Mojeek) work with no infrastructure at all, but only for omp, and only by allowlisting
those engines directly — the sidecar problem again, minus the sidecar.

## 20. Budget — resource limits, lane policies, and a wall-clock TTL

The gateway answers *where the agent may reach*. It says nothing about *how much of the
host it may burn* or *how long it may run*. Three additions close that gap, all of them
per-sandbox and all of them enforced outside the workload.

**Resource limits.** The workload gets `--cpus` (default 4) and `--memory` (default 8g),
and a fixed pid ceiling of 2048; the gateway gets a fixed 1 cpu / 1g, because it only
proxies and a runaway workload must not be able to starve the firewall that contains it.
These are compose v2 `deploy.resources.limits`, which `docker compose` applies without
swarm. They are not a security boundary — a container under a cgroup limit is still a
container — but they turn "one sandbox wedged the machine" into "one sandbox got slow",
which is the failure mode that actually happens with agents that run `make -j` or fork.

**Lane policies.** `--policy FILE:LANE` reads one lane of a policy file (Omni's
`governance/policy/lanes.yaml` shape) and uses it as this sandbox's defaults: its
`egress_allow` list becomes allowlist entries, `harness`/`llm_backend` choose the agent,
and `defaults.sandbox.cpus/memory/max_minutes` (with lane-level overrides) set the
ceiling and the lifetime. The point is that the rules for a class of run live in one
reviewed, CODEOWNER'd file rather than in whatever flags the caller remembered. Explicit
flags still win, so the policy is a floor to work from, not a cage — the file is a
default-setter on the host CLI, not an enforcement point inside the sandbox, and
`OMNI_LANE` is exported into the workload only so in-container tooling can *see* which
lane it is in. Anything that must actually be enforced belongs in the gateway.

**Wall-clock TTL.** An agent that hangs costs money for as long as nobody looks. `spawn`
is the non-interactive entry point — one `up` under a policy, then the command detached
in the workload — and it records `SANDBOX_STARTED_AT`, `SANDBOX_TTL_MINUTES` and a
`SANDBOX_RUN_ID`. `reap` (cron, every 5 minutes) brings down everything past its TTL.
Deliberately a poll from outside rather than a timer inside: a workload that can kill its
own deadline has no deadline. Sandboxes with no recorded TTL are never touched unless
`reap --all` is used, so an interactive session is not swept away by the cron that exists
to catch runaway batch runs.

**Where the state lives.** The durable copy of the limits, lane and lifetime is the
sandbox's own state dir (`sandboxes/<name>/`, §10.1), exactly as harness/model selection
is. `ls` and `reap` both walk those dirs, which is why they can report on every sandbox
at once — a single shared env file could not, being last-writer-wins across parallel
sandboxes (§11).
