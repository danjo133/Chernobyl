# Agent Sandbox

A reusable, hardened devcontainer for running a coding agent — Claude Code, [pi](https://pi.dev),
[omp](https://omp.sh) or [prime-agent](https://github.com/PrimeIntellect-ai/prime-agent), against
Anthropic or a local model — with permission prompts off, doing untrusted-ish autonomous work. It
wraps the workload in three independent boundaries so a bypassed or fully-autonomous session can do
only bounded damage:

1. **An external egress firewall** the workload's traffic *must* traverse — enforced
   in a separate container the workload shares a network namespace with but holds no
   capabilities over, so it cannot read, flush, or rewrite the rules.
2. **A host-side, gitignore-driven FUSE filter** so secrets and ignored files never
   bind into the container in the first place — plus a hard-deny layer for
   sensitive-even-if-committed files (`.env*`, `*.pem`, `*.key`, `id_*`, `.ssh/`, …).
3. **A credential broker** so the container can *use* credentials (Anthropic, GitHub,
   Kubernetes, cloud) without ever *holding* the real secret — the human
   authenticates on the host, the container carries only an opaque, scoped handle.

The agent harness and the model backend are both swappable at launch
(`--harness`, `--llm-backend`) — see [Harnesses & LLM backends](#harnesses--llm-backends).

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
| [`sandbox`](sandbox) | the CLI — `up \| ls \| allow \| web \| tools \| open \| models \| flywheel \| down`. Manages worktrees, project naming, port auto-assignment, harness/model selection, egress flags. Start here. |
| [`.devcontainer/`](.devcontainer/) | the sandbox itself: compose topology, workload image, gateway, FUSE filter, tools sidecar. See its [README](.devcontainer/README.md). |
| `.devcontainer/harness/` | harness + backend registry (Claude Code / pi / omp / prime-agent × Anthropic / Ollama / OpenAI-compatible), shared by the CLI and the workload |
| `.devcontainer/gateway/` | mitmproxy + iptables + redis — the firewall and credential boundary (`broker_addon.py`, `control_server.py`, opt-in `flywheel_addon.py`) |
| `.devcontainer/fusefilter/` | host-side gitignore-driven filtered view (Go + go-fuse) |
| `.devcontainer/tools/` | bb-hunter sidecar image (opt-in `--tools`): recon/scanning tooling Claude drives over SSH, egress contained through the gateway |
| [`broker/`](broker/) | host-side credential broker: `webui.py` (password-gated web app) + `fill.py` (CLI filler) that run real OAuth and mint scoped handles, plus `flywheel.py` (read/export captured LLM traffic). See its [README](broker/README.md). |
| [`tooling/`](tooling/) | standalone bug-bounty tooling (bb-hunter image, scope guard, MCP/LiteLLM helpers) — usable outside a sandbox too |
| [`tooling/systemd/`](tooling/systemd/README.md) | ready-made user timer for `sandbox reap` (TTL enforcement), plus NixOS and cron equivalents |
| [`tooling/searxng/`](tooling/searxng/) | deploy-it-yourself SearXNG (compose + settings) that gives every harness web search through one allowlisted host — runs **outside** the sandbox, see its [README](tooling/searxng/README.md) |
| [`install.sh`](install.sh) / [`Makefile`](Makefile) | installer: checks host prerequisites, builds the FUSE filter, puts `sandbox` on your `PATH` |
| [`docs/SANDBOX-PLAN.md`](docs/SANDBOX-PLAN.md) | full design, threat model, phased plan |

## Install

Prerequisites (plan §9): **rootless Docker** (recommended) or Podman with
`docker compose`, `fuse3`, `python3`, and — on rootful Docker only — `user_allow_other`.
Building the FUSE filter needs Go (or Nix); a finished install ships the binary, so
the target machine does not need Go unless it is building.

```bash
git clone https://github.com/danjo133/Chernobyl.git && cd Chernobyl

./install.sh --check      # report on prerequisites, change nothing
./install.sh              # install for you into ~/.local  (no root)
./install.sh --system     # or: install into /usr/local for every user (uses sudo)
```

`install.sh` wraps the `Makefile`, which you can also drive directly —
`make install PREFIX=/opt/agent-sandbox`, `make uninstall`, `make test`. Installing
puts the payload in `$PREFIX/lib/agent-sandbox` and symlinks `$PREFIX/bin/sandbox` at
it; if `$PREFIX/bin` is not on your `PATH`, the installer says so.

To update, `git pull` and re-run `./install.sh`.

### What lives where

The install tree is **read-only at runtime** — the CLI writes nothing into it. That is
what makes one system-wide install safe to share: users never collide, and never see
each other's sandboxes.

| Path | What | Override with |
|---|---|---|
| `$PREFIX/lib/agent-sandbox/` | the install itself (CLI, compose topology, gateway, broker) | `PREFIX` |
| `~/.local/state/agent-sandbox/` | your sandboxes: env file, egress allowlist, broker control dir, ports, TTL | `SANDBOX_STATE_DIR` (or `XDG_STATE_HOME`) |
| `~/.sandbox/homes/` | your harness logins/settings/history, bind-mounted into every sandbox | `SANDBOX_HOMES` |
| `$XDG_RUNTIME_DIR/devfilter/<name>/` | the FUSE-filtered view of the repo, per sandbox | — |

One directory per sandbox under `state/sandboxes/<name>/`, so parallel sandboxes never
race. Only that directory's `control/` subdir is bind-mounted into the gateway, so
host-side material (the broker UI password, pids) stays out of any container.

Upgrading from a version that kept state inside the repo? The CLI moves it into the new
layout on first run and tells you what it moved — existing sandboxes stay drivable.

`make uninstall` removes the install but deliberately keeps your state and logins.

## Quickstart

Installed, the CLI is just `sandbox`; from a checkout it is `./sandbox`.

```bash
# Bring a sandbox up against a repo (builds images, mounts the FUSE view,
# installs the MITM CA, starts the compose project):
./sandbox up --source /path/to/repo

# Open an interactive agent session inside it (asks which harness on first use):
./sandbox open --name cc-repo

# …or pick the harness and the model up front:
./sandbox up --source /path/to/repo --harness pi --llm-backend ollama \
             --llm-url https://ollama.example.com

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
| `--harness H` | agent CLI: `claudecode` (default), `pi`, `omp`, `prime-agent` |
| `--llm-backend B` | `anthropic` (default), `ollama`, `openai-compat` |
| `--llm-url URL` | backend endpoint (required for `openai-compat`) |
| `--model ID` | model id; for a local backend the largest tool-capable one is auto-picked if omitted |
| `--small-model ID` | model for the fast/subagent slot. Defaults to the **same** model on a local backend — a distinct one means two models resident, which thrashes on a single GPU (and under `OLLAMA_MAX_LOADED_MODELS=1`). Split them when the box has room. |
| `--model-context N` | pin the usable context window instead of probing for it |
| `--no-context-probe` | skip the check for a backend that silently truncates prompts (see below) |
| `--flywheel` | record every LLM call for later distillation or eval — **unredacted**, see below |
| `--port HOST:CTR` | publish an app port explicitly. **Omit it** and a free host port (from 3000) is auto-assigned → container `:3000`, so parallel sandboxes never collide. |
| `--web-port N` | pin the host-only broker UI port (default: first free from 9999). |
| `--tools` | add the bb-hunter tools sidecar + Caido proxy — Claude drives recon tooling over SSH, egress still contained through the gateway. |
| `--allow-url DOMAIN` | add a domain to this sandbox's egress allowlist (repeatable) |
| `--allow-ip IP` | add an IP to the allowlist (repeatable) |
| `--allow-internet` | **UNRESTRICTED egress** — no allowlist, no MITM. Use sparingly. |
| `--allow-image-build` | enable rootless image builds (Podman/Buildah); loosens the workload's seccomp confinement. Off by default. |

Each sandbox is its own compose project (`COMPOSE_PROJECT_NAME`) with isolated
networks, volumes, allowlist, and MITM log — so several run in parallel without
crossing wires (plan §11). Auth and agents come from a shared host-side home **per
harness** (`SANDBOX_HOMES`, default `~/.sandbox/homes/<harness>-home`; Claude Code keeps
`~/.sandbox/claude-home`); log in once per harness and every sandbox inherits it.

## Harnesses & LLM backends

Two independent choices, composable in any combination (plan §17):

| `--harness` | what it is |
|---|---|
| `claudecode` | Claude Code — subagents, hooks, MCP, the Minions agents/skills wiring |
| `pi` | [pi](https://pi.dev) — 4 tools and a tiny system prompt; the best fit for local models |
| `omp` | [omp / oh-my-pi](https://omp.sh) — pi fork with LSP, DAP debugging, 30+ tools |
| `prime-agent` | [prime-agent](https://github.com/PrimeIntellect-ai/prime-agent) — RLM (tools as calls in a persistent IPython REPL) + a self-refining harness |

| `--llm-backend` | notes |
|---|---|
| `anthropic` | default. Claude Code uses its OAuth session; the pi-family harnesses get a placeholder key while the gateway injects the real one — no model credential in the container either way. |
| `ollama` | your own box. Serves both the OpenAI API (pi family) and, since 0.12, `/v1/messages` — so **Claude Code talks to a local model directly**, no translating proxy. Defaults to `http://localhost:11434`; for a box on your network export `SANDBOX_OLLAMA_URL` once or pass `--llm-url`. |
| `openai-compat` | anything else (vLLM, llama.cpp, LM Studio, OpenRouter, a router): `--llm-url` required. `claudecode` needs `--llm-anthropic-compat` to confirm the endpoint also serves `/v1/messages`. |

All four harnesses are baked into the image, so switching one is a runtime decision —
no rebuild, no restart, credentials in the broker survive:

```bash
./sandbox models --llm-backend ollama --llm-url https://ollama.example.com   # what can it run?
./sandbox open --name cc-repo --harness omp --model qwen3.5:35b             # switch, in place
```

The CLI picks a default model for a local backend (largest tool-capable), sizes the
context window, and appends whatever domains the harness and backend need to the sandbox's
egress allowlist.

> **The context check earns its keep.** A model's advertised window (`/api/show` says
> 262144 for qwen3.5:35b) is not what the server serves — Ollama serves its own `num_ctx`,
> defaulting to a few thousand tokens, and silently truncates longer prompts **from the
> front**, where every harness puts its system prompt and tool schemas. The model then gets
> style rules and no tools, and narrates tool calls as prose or invented XML (`<exec>`,
> `<evidence-and-output>`) that nothing executes — with no error anywhere, so it reads as
> "this model can't use tools". `sandbox up` measures the served window and warns with the
> fix (`OLLAMA_CONTEXT_LENGTH=32768`, or `PARAMETER num_ctx`), capping the sandbox's context
> to what the server actually accepts. See plan §17.

## Web search

Claude Code's `WebSearch` is a server-side Anthropic tool, so it disappears the moment you
point the harness at a local model; pi and prime-agent ship no web tool at all; omp has a
native one. One endpoint fixes all four (plan §19):

```bash
# deploy once, on your server box — NOT in the sandbox (see tooling/searxng/README.md)
cd tooling/searxng && cp env.example .env && $EDITOR .env && docker compose up -d

# then point sandboxes at it
./sandbox up --source ~/dev/repo --search-url https://searxng.example.com
./sandbox open --name cc-repo --search none          # or turn it back off
```

That adds **one** host to the egress allowlist, sets `SEARXNG_ENDPOINT` for omp's native
`web_search`, points the in-container `websearch` command at it, and installs a `websearch`
skill into each harness home so the model knows the command exists (all four harnesses read
the same `SKILL.md` format).

It is deliberately **snippets only**: reading a result page still needs
`./sandbox allow --url <domain>`. A general fetch lane out of a contained sandbox is an
exfiltration channel, so it is not on by default — and the generated skill tells the model
to name the domain it needs rather than route around the allowlist.

Search runs outside the sandbox for the same reason the flywheel runs in the gateway: a
sidecar would share the gateway's netns, so SearXNG's own scraping would need the search
engines themselves on your allowlist, MITM'd. Prefer not to self-host? Brave Search API or
Tavily fit the credential broker cleanly — the key lives in the gateway, never in the
container.

## Flywheel (opt-in traffic capture)

`./sandbox up --flywheel` makes the gateway record every LLM request/response — from any
harness, against any backend, in one shape — into the sandbox's host-side control dir.
That corpus is the *observe* step of the flywheel: fine-tune or evaluate a local model on
the work you actually do, rather than on a public benchmark (plan §18).

```bash
./sandbox flywheel --name cc-repo stats
./sandbox flywheel --name cc-repo export --format openai -o traces.jsonl
```

> **These records are not redacted.** Unlike the egress log, they contain full prompts and
> completions — your source, tool output, anything the agent read. They stay on the host
> (the workload cannot see them) and are gitignored, but treat the capture dir like the
> source tree it mirrors. Request headers are never captured, so broker-injected
> credentials never land in the corpus.

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
