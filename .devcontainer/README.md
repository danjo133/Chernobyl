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
| `containers/` | rootless Podman/Buildah config (vfs storage, single-UID) for in-workload image builds |
| `compose.imagebuild.yaml` | opt-in overlay (`--allow-image-build`): appends seccomp/apparmor=unconfined for rootless image builds |
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

## Two rules to remember

- **A sandbox is a unit:** gateway + workload (+ any dev-dep containers) share one netns;
  everything inside talks over `localhost`, egress only via mitmproxy.
- **The FUSE filter governs source only;** build state lives in per-sandbox volumes.
