# Claude Code Sandbox

A reusable, hardened devcontainer for running Claude Code — including
`--dangerously-skip-permissions` — against untrusted-ish autonomous work. It wraps
the workload in three independent boundaries so a bypassed or fully-autonomous
session can do only bounded damage:

1. **An external egress firewall** the workload's traffic *must* traverse — enforced
   in a separate container the workload shares a network namespace with but holds no
   capabilities over, so it cannot read, flush, or rewrite the rules.
2. **A host-side, gitignore-driven FUSE filter** so secrets and ignored files never
   bind into the container in the first place — plus a hard-deny layer for
   sensitive-even-if-committed files (`.env*`, `*.pem`, `*.key`, `id_*`, `.ssh/`, …).
3. **A credential broker** so the container can *use* credentials (Anthropic, GitHub,
   Kubernetes, cloud) without ever *holding* the real secret — the human
   authenticates on the host, the container carries only an opaque, scoped handle.

The full design, rationale, and threat model live in
[`docs/SANDBOX-PLAN.md`](docs/SANDBOX-PLAN.md). Read that for the *why*; this README
is the *what's here and how to run it*.

> **Status: bring-up works end-to-end** on the target host (rootless Docker on NixOS):
> `./sandbox up` builds the images, mounts the FUSE view, installs the MITM CA, and
> starts the compose project. The gateway firewall/broker, the tools sidecar, and the
> host-side broker web UI are wired and driveable. Still treat the hardening as
> *load-bearing but young* — the threat model in plan §7–8 has not been red-teamed.

## Architecture

```
                    ┌────────────────────────────────────────────────────┐
   HOST             │  gateway container — firewall + credential broker   │
 ┌──────────┐       │  (the only path to the internet)                    │
 │ ~/dev/   │       │  • NET_ADMIN/NET_RAW  • mitmproxy (transparent)     │──┐ egress
 │  myrepo  │       │  • iptables REDIRECT 80/443 → mitmproxy             │  │ to
 └────┬─────┘       │  • allowlist + scope-checked credential injection   │  │ internet
      │ FUSE        │  • redis: handle→real cred, scope, TTL              │──┘
      │ (gitignore  └───────────────────────▲────────────────────────────┘
      │  filter)     shared net namespace    │
 ┌────▼─────────┐    ┌──────────────────────┴────────────────────────────┐
 │/run/.../view │──► │  devcontainer — the workload                       │
 │ filtered repo│bind│  network_mode: service:gateway                     │
 └──────────────┘    │  • NO net caps — cannot alter egress rules         │
                     │  • Claude Code + toolchain                         │
   HOST-side filler  │  • holds opaque handles / nothing — never creds    │
   tool / MCP ─────► │  • trusts mitmproxy CA                             │
   (runs real OAuth) └───────────────────────────────────────────────────┘
```

The two structural decisions carrying the security model — `network_mode:
service:gateway` (firewall outside the workload's reach) and the gateway-as-broker
(real credentials never enter the workload) — are detailed in plan §2.

## Repository layout

| Path | What |
|---|---|
| [`sandbox`](sandbox) | the CLI — `up \| ls \| allow \| web \| tools \| open \| down`. Manages worktrees, project naming, port auto-assignment, egress flags. Start here. |
| [`.devcontainer/`](.devcontainer/) | the sandbox itself: compose topology, workload image, gateway, FUSE filter, tools sidecar. See its [README](.devcontainer/README.md). |
| `.devcontainer/gateway/` | mitmproxy + iptables + redis — the firewall and credential boundary (`broker_addon.py`, `control_server.py`) |
| `.devcontainer/fusefilter/` | host-side gitignore-driven filtered view (Go + go-fuse) |
| `.devcontainer/tools/` | bb-hunter sidecar image (opt-in `--tools`): recon/scanning tooling Claude drives over SSH, egress contained through the gateway |
| [`broker/`](broker/) | host-side credential broker: `webui.py` (password-gated web app) + `fill.py` (CLI filler) that run real OAuth and mint scoped handles. See its [README](broker/README.md). |
| [`tooling/`](tooling/) | standalone bug-bounty tooling (bb-hunter image, scope guard, MCP/LiteLLM helpers) — usable outside a sandbox too |
| [`docs/SANDBOX-PLAN.md`](docs/SANDBOX-PLAN.md) | full design, threat model, phased plan |

## Quickstart

Prerequisites (plan §9): **rootless Docker** (recommended) or Podman with
`docker compose`, `fuse3`, and — on rootful Docker only — `user_allow_other`.

```bash
# Bring a sandbox up against a repo (builds images, mounts the FUSE view,
# installs the MITM CA, starts the compose project):
./sandbox up --source /path/to/repo

# Open an interactive Claude Code session inside it:
./sandbox open --name cc-repo

# List sandboxes (with broker-UI / app / caido ports + password) / tear one down:
./sandbox ls
./sandbox down --name cc-repo
```

Other subcommands: `sandbox allow` adds egress entries to a *running* sandbox
(hot-reloaded, no restart); `sandbox web` (re)starts or stops the host broker UI;
`sandbox tools` drops into the bug-bounty sidecar (or runs a one-off command in it).
Run `./sandbox` with no args for the full synopsis.

Useful `up` flags:

| Flag | Effect |
|---|---|
| `--name NAME` | explicit sandbox name (default derived from the source dir) |
| `--worktree BRANCH` | isolate same-repo tasks via a git worktree (separate dir + branch) |
| `--port HOST:CTR` | publish an app port explicitly. **Omit it** and a free host port (from 3000) is auto-assigned → container `:3000`, so parallel sandboxes never collide. |
| `--web-port N` | pin the host-only broker UI port (default: first free from 9999). |
| `--tools` | add the bb-hunter tools sidecar + Caido proxy — Claude drives recon tooling over SSH, egress still contained through the gateway. |
| `--allow-url DOMAIN` | add a domain to this sandbox's egress allowlist (repeatable) |
| `--allow-ip IP` | add an IP to the allowlist (repeatable) |
| `--allow-internet` | **UNRESTRICTED egress** — no allowlist, no MITM. Use sparingly. |
| `--allow-image-build` | enable rootless image builds (Podman/Buildah); loosens the workload's seccomp confinement. Off by default. |

Each sandbox is its own compose project (`COMPOSE_PROJECT_NAME`) with isolated
networks, volumes, allowlist, and MITM log — so several run in parallel without
crossing wires (plan §11). Auth and agents come from a shared host-side Claude home
(`SANDBOX_CLAUDE_HOME`, default `~/.sandbox/claude-home`); log in once and every
sandbox inherits it.

### Host ports (auto-assigned, per-sandbox-stable)

So parallel sandboxes never fight over the same host port, three ranges are scanned
for the first free port and the choice is remembered per sandbox (stable across
restarts). All bind `127.0.0.1` only:

| Range | What | Override |
|---|---|---|
| `3000+` | the workload's app port → container `:3000` | `--port HOST:CTR` |
| `9999+` | host-side broker web UI (credential + log management) | `--web-port N` |
| `8090+` | Caido workbench (only with `--tools`) | — |

`./sandbox ls` prints the live assignments (and the broker-UI password) for every sandbox.

## Egress allowlist

Egress is denied by default and restricted to an explicit domain allowlist enforced
in the gateway. The base list is
[`.devcontainer/gateway/allowed-domains.txt`](.devcontainer/gateway/allowed-domains.txt)
(Anthropic, GitHub, npm, PyPI, …); per-sandbox additions come from `--allow-url` /
`--allow-ip`. `broker_addon.py` hot-reloads the per-sandbox list on change, so
`./sandbox allow --url DOMAIN` takes effect with no restart (credentials survive).
When a fetch is blocked, the MITM deny-log shows exactly what to add.

## Credentials

The container never holds real secrets. On the host you run the real auth flow and
mint a scoped, revocable handle stored in the gateway's redis; the workload sends the
handle and the gateway swaps in the real credential for in-scope requests only. Two
front-ends write the same store through the gateway's control socket:

- **`broker/webui.py`** — the normal path: a host-only, password-gated web app started
  by `./sandbox up` (URL + password from `./sandbox ls`, or restart it with
  `./sandbox web`). Mint/revoke handles, set inject-by-host headers, and watch the
  redacted egress log.
- **`broker/fill.py`** — the scripting/CLI filler.

See [`broker/README.md`](broker/README.md) and plan §4/§15.

## Tools sidecar (bug-bounty recon)

`./sandbox up --tools` adds the **bb-hunter** sidecar and a **Caido** proxy. The
sidecar shares the gateway network namespace, so its recon/scanning traffic is
contained through the same firewall + broker as the workload — no extra internet
reach. Claude drives it over loopback SSH (`hunter <cmd>`); `./sandbox tools` drops
you into it for manual use. Inspected egress flows through Caido's workbench
(`http://127.0.0.1:<caido-port>`) for intercept/replay. For raw recon the gateway
would block (nmap, DNS, raw sockets), `./sandbox tools --unrestricted` spins up an
ephemeral **full-internet** box — manual use only, outside the containment. See plan §16.

## The load-bearing caveat

Every boundary here prevents credential **theft**, not authorized **use**. While a
handle or identity is live, Claude can do whatever its *scope* permits — including
exfiltrating *to* an allowlisted host. Allowlist tightness, RBAC, path/method limits,
and TTLs are the real levers. Dev containers are not a hard sandbox; this bounds blast
radius, it does not make autonomous use safe. Full threat model in plan §7–8.
