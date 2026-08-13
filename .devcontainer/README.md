# Agent sandbox — scaffold

A reusable, hardened devcontainer: external egress firewall + credential broker,
host-side gitignore FUSE filter, multi-sandbox + local-dev support. Full design and
rationale in [`docs/SANDBOX-PLAN.md`](../docs/SANDBOX-PLAN.md).

> **Status: UNVERIFIED scaffold.** None of this has been built or run yet (the dev
> session's Bash/bwrap is broken on this NixOS host — see plan §9). Two pieces need a
> real compile/run pass before trusting them: the gateway's `iptables`/transparent-proxy
> wiring (`gateway/entrypoint.sh`) and the go-fuse node embedding (`fusefilter/main.go`).

## Layout

| Path | What |
|---|---|
| `compose.yaml` | gateway + devcontainer topology (workload shares gateway netns) |
| `devcontainer.json` | VS Code entry: compose service, FUSE mount hook, CA-trust postCreate |
| `devcontainer.Dockerfile` | workload image: all four agent harnesses + toolchain + CA trust env |
| `harness/` | harness + backend registry (see plan §17) — sourced by both the `sandbox` CLI and the workload; `install.sh` runs at build, `run.sh` becomes `sandbox-harness` inside |
| `harness/websearch` | in-container search client (plan §19): one command every harness can call, pointed at `--search-url`; installed to `/usr/local/bin/websearch` |
| `trust-ca.sh` | installs the gateway MITM CA into the workload |
| `managed-settings.json` | org policy (telemetry off, bypass perms in the contained env) |
| `build-dirs.txt` | build/dep dirs backed by per-sandbox volumes |
| `containers/` | rootless Podman/Buildah config (vfs storage, single-UID) for in-workload image builds |
| `compose.imagebuild.yaml` | opt-in overlay (`--allow-image-build`): appends seccomp/apparmor=unconfined for rootless image builds |
| `gateway/` | mitmproxy + iptables + redis broker (the firewall + credential boundary), plus the opt-in `flywheel_addon.py` LLM-traffic capture (plan §18) |
| `fusefilter/` | host-side gitignore-driven filtered view (Go + go-fuse) |
| `../broker/` | host-side credential filler (runs real OAuth, mints scoped handles) |
| `../sandbox` | CLI: `up \| ls \| down \| open \| models \| flywheel` (worktrees, project naming, ports, harness/model selection) |

## Bring-up order (once Bash works) — maps to plan §14

1. **Gateway alone** — `cd gateway && docker build .`, run with `--cap-add NET_ADMIN`,
   verify allowlisted hosts pass and others get 403/blocked.
2. **Full compose** — `./sandbox up --source /path/to/repo`, confirm the workload reaches
   only allowlisted hosts and holds no net caps (`capsh --print` inside).
3. **FUSE filter** — confirm secrets/gitignored files are absent in `/workspace`, edits to
   tracked files appear on the host, builds write to the volume-backed dirs.
4. **Broker** — mint a handle with `broker/fill.py`, confirm injection works and the real
   credential is absent from the container (env, files, `/proc`).

## Agent harnesses

The image ships **four** harnesses, one Docker layer each, so switching is a runtime
choice (`./sandbox open --harness X`) with no rebuild:

| harness | binary | installed from | notes |
|---|---|---|---|
| `claudecode` | `claude` | npm `@anthropic-ai/claude-code` | org policy via `managed-settings.json`; Minions agents/skills wiring |
| `pi` | `pi` | npm `@earendil-works/pi-coding-agent` (`--ignore-scripts`) | smallest system prompt — the best fit for local models |
| `omp` | `omp` | npm `@oh-my-pi/pi-coding-agent` | **also installs `bun`**: omp's published binary is a bun bundle and will not run without it |
| `prime-agent` | `prime-agent` | R2 release tarball, sha256-verified, then `npm i -g` | not on the npm registry; the installer's own `curl \| sh` is deliberately not used |

**Web search** is wired the same way for all four: `--search-url` puts one host on the
allowlist, sets `SEARXNG_ENDPOINT` for omp's native `web_search` tool, and installs a
`websearch` skill into each harness home (they all read Claude Code's `SKILL.md` format).
Claude Code's own `skills/` is usually a symlink to the read-only Minions mount, so the
skill is skipped there — the `websearch` command is still on PATH, and you can add the
skill to Minions yourself. See [plan §19](../docs/SANDBOX-PLAN.md).

Each keeps its state in its own shared host home (`~/.claude`, `~/.pi`, `~/.omp`,
`~/.prime` inside the container). Model/provider config is generated per launch from
`--llm-backend`/`--model`; see [plan §17](../docs/SANDBOX-PLAN.md#17-harness-abstraction--swapping-the-agent-and-the-model).

## Workload toolchain

The image ships Node, **Go** (official tarball, pinned via `GO_VERSION`; `GOTOOLCHAIN=local`),
**Python** (3 + venv + dev headers; venvs in the `.venv` volume), `make`/`build-essential`,
and **rootless Podman/Buildah** for daemonless OCI image builds. Module/build caches and the
image store persist in per-sandbox volumes (`build-go`, `build-containers`).

**Image builds (`podman build` / `buildah bud`)** run unprivileged — no Docker socket, no DinD,
no extra caps. The trade-offs (and the seccomp note) are deliberate:
- **opt-in, off by default.** Bring the sandbox up with `./sandbox up --allow-image-build` (alias
  `--disable-seccomp`) to layer `compose.imagebuild.yaml`, which grants the workload `seccomp=unconfined`
  so the kernel allows the `unshare(CLONE_NEWUSER)` rootless builds need. Without the flag the hardened
  default seccomp profile applies and `podman build` fails at namespace setup. Either way the workload
  keeps `cap_drop:ALL` + `no-new-privileges` and cannot reach the egress rules.
- storage is **vfs** (overlay needs `/dev/fuse`, which we don't grant): correct but slower and disk-heavy.
- **single-UID** (no `/etc/subuid` range — `newuidmap` is inert under `no-new-privileges`), so build
  steps that chown/switch to a *different* UID may misbehave; `ignore_chown_errors` tolerates the storage case.
- build-time `RUN` egress still flows through the gateway, so pulls obey the allowlist and base-image
  package managers must **trust the MITM CA** (`COPY` it in or splice the registry) — same friction as plan §12.
- **Host prereq:** unprivileged user namespaces must be enabled on the host
  (`kernel.unprivileged_userns_clone=1` / NixOS `security.unprivilegedUsernsClone = true;`), else rootless
  builds fail to create the namespace.

## Caches visible to the sandbox but not to git (`.sandboxshow`)

The FUSE filter hides whatever `git check-ignore` would ignore, so build junk and
git-ignored secrets never enter `/workspace`. But some git-ignored paths — caches you
want the agent to read/write across runs — should stay visible. List them in a committed
`.sandboxshow` at the repo root:

```
# .sandboxshow — git-ignored paths that stay read/write inside the sandbox
.cache/
.pytest_cache/
tools/bin
```

Rules: blank lines and `#` comments are skipped; a trailing slash is optional. An entry
**without** a slash matches that name as any path component (shell globs like `*.tmp`
work); an entry **with** a slash matches that path and everything beneath it. The built-in
set (`.claude`, `node_modules`, `.venv`, `target`, `dist`) is always force-shown. The
**hard-deny secret layer** (`.env*`, `*.key`, `*.pem`, `id_*`, `.ssh`, `.aws`, `.gnupg`,
`.kube`, `.docker`, …) ALWAYS wins — listing a secret here can never expose it. Writes to
force-shown paths pass through to the host source dir (still git-ignored, so never committed).

## Two rules to remember

- **A sandbox is a unit:** gateway + workload (+ any dev-dep containers) share one netns;
  everything inside talks over `localhost`, egress only via mitmproxy.
- **The FUSE filter governs source only;** build state lives in per-sandbox volumes.
