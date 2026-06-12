# Claude Code sandbox — scaffold

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
| `devcontainer.Dockerfile` | workload image: Claude Code + toolchain + CA trust env |
| `trust-ca.sh` | installs the gateway MITM CA into the workload |
| `managed-settings.json` | org policy (telemetry off, bypass perms in the contained env) |
| `build-dirs.txt` | build/dep dirs backed by per-sandbox volumes |
| `gateway/` | mitmproxy + iptables + redis broker (the firewall + credential boundary) |
| `fusefilter/` | host-side gitignore-driven filtered view (Go + go-fuse) |
| `../broker/` | host-side credential filler (runs real OAuth, mints scoped handles) |
| `../sandbox` | CLI: `up \| ls \| down \| open` (worktrees, project naming, ports) |

## Bring-up order (once Bash works) — maps to plan §14

1. **Gateway alone** — `cd gateway && docker build .`, run with `--cap-add NET_ADMIN`,
   verify allowlisted hosts pass and others get 403/blocked.
2. **Full compose** — `./sandbox up --source /path/to/repo`, confirm the workload reaches
   only allowlisted hosts and holds no net caps (`capsh --print` inside).
3. **FUSE filter** — confirm secrets/gitignored files are absent in `/workspace`, edits to
   tracked files appear on the host, builds write to the volume-backed dirs.
4. **Broker** — mint a handle with `broker/fill.py`, confirm injection works and the real
   credential is absent from the container (env, files, `/proc`).

## Two rules to remember

- **A sandbox is a unit:** gateway + workload (+ any dev-dep containers) share one netns;
  everything inside talks over `localhost`, egress only via mitmproxy.
- **The FUSE filter governs source only;** build state lives in per-sandbox volumes.
