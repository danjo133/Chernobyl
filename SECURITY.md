# Security model

This project sandboxes an autonomous coding agent (Claude Code) so it can run with
`--dangerously-skip-permissions` **without** that meaning "unrestricted access to your
machine, network, and credentials." It is defense-in-depth, not a formally verified
boundary. Read this before pointing it at real secrets or hostile inputs.

The full design and rationale live in [`docs/SANDBOX-PLAN.md`](docs/SANDBOX-PLAN.md);
this file is the security-relevant summary and the honest list of what does and does not
hold.

## Threat model

The **workload container is untrusted**. Assume the agent inside it can be prompt-injected
by content it reads (a web page, a dependency, a target's response) and will then actively
try to (a) exfiltrate data or credentials, (b) reach the internet off-allowlist for C2, and
(c) read host files it shouldn't. The design goal is that none of those succeed without an
explicit, human-initiated opt-out on the host.

Out of scope: a malicious *host* operator, kernel 0-days / container-escape via the runtime,
and multi-tenant hosts (see "single-user host" below).

## Guarantees the architecture provides

1. **Egress is deny-by-default and allowlisted.** The workload shares the gateway's network
   namespace and holds **no** `NET_ADMIN`/`NET_RAW` (`cap_drop: ALL`), so it cannot alter the
   firewall. All TCP egress is force-redirected through mitmproxy, which enforces a domain
   allowlist (`gateway/allowed-domains.txt`); everything else is dropped. IPv6 egress is
   dropped entirely (fail-closed at startup).
2. **Real credentials never enter the workload.** The broker injects secrets onto requests
   **on egress**, at the gateway. The store is Redis on a unix socket in the gateway's mount
   namespace (not shared with the workload) and is memory-only (`--save "" --appendonly no`),
   so nothing sensitive is written to disk and `sandbox down` wipes it.
3. **The mitmproxy CA private key is gateway-only.** Only the public CA cert is shared into
   the workload (`ca-pub` volume, read-only); the private key stays in the gateway-only
   `gateway-ca` volume.
4. **The credential-management surface is host-only.** The web UI and the control socket live
   on the host / in the gateway's mount namespace — never reachable from the workload. The web
   UI binds `127.0.0.1`, is password-gated (constant-time compare), sets `HttpOnly;
   SameSite=Strict` cookies, checks the `Host` header (anti DNS-rebind), and never renders a
   real secret (values are masked to a last-4 hint).
5. **Host secrets are filtered out of the source view.** A host-side FUSE filter hides
   git-ignored files plus a hard-deny list of secret files/dirs (`.env*`, `*.key`, `*.pem`,
   `id_*`, `.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, …) — at **any** directory depth,
   even inside force-shown dirs. See `.sandboxshow` in `.devcontainer/README.md` to make a
   git-ignored cache read/write without exposing secrets.

Because these hold, the workload runs with `bypassPermissions` — the sandbox *is* the blast
radius. That trade is only sound while the guarantees above hold; the hardening below is what
makes them hold.

## Hardening applied (confused-deputy & egress-bypass fixes)

- **No cleartext credential path.** Port 80 is not redirected to the proxy; credentials are
  injected **only** onto verified-TLS (`https`) flows. This closes an exfil where a workload
  points port 80 at an attacker IP, sends `Host: <allowed>`, and has the broker staple a real
  token onto a plaintext request to the attacker.
- **Splice is destination-verified.** For `!`-passthrough (cert-pinned) hosts, the gateway
  splices raw TLS **only if** the real destination IP resolves from the client's SNI host —
  otherwise it fails closed. This closes an SNI-spoof tunnel to arbitrary IPs. No passthrough
  entry ships enabled by default.
- **DNS is pinned to the embedded resolver.** Port 53 is allowed only to Docker's `127.0.0.11`,
  not arbitrary resolvers, to cut off high-bandwidth DNS tunneling.

## Known residual risks (please read)

- **Low-bandwidth DNS side channel.** Name resolution still goes through the embedded resolver,
  which forwards externally host-side. An attacker can encode small amounts of data in the
  *names* it asks to resolve. Restricting the resolver to the allowlist (see the TODO in
  `gateway/entrypoint.sh`) would close this; it is not yet done.
- **Allowlisted-domain abuse.** Any allowlisted host with attacker-usable storage (gists,
  paste endpoints, user-content subdomains) is a potential exfil channel. Keep the allowlist
  tight; subdomain matching (`host.endswith(".<allowed>")`) permits *all* subdomains of an
  allowed domain.
- **Tools/Caido mode relaxes upstream cert probing** (`upstream_cert=false`) while chaining
  through Caido; the destination is re-derived from the Host header by Caido, but this mode is
  a larger trust surface. Enable it only when you want the interception workbench.
- **The egress request log looks normal** for injected requests (it logs header *names*, never
  values) — it is not an exfil-detection tool.
- **This is young software.** The threat model has had one adversarial review pass, not
  sustained red-teaming. Treat "credentials never leak / egress is contained" as a strong
  design intent, not a proof.

## Opt-in escape hatches (they drop containment on purpose)

Each requires an explicit flag on the **host** — the in-container agent cannot invoke them:

- `sandbox up --allow-internet` — unrestricted egress, no allowlist, no MITM.
- `sandbox up --allow-image-build` — `seccomp=unconfined` so rootless image builds work
  (larger kernel attack surface; `cap_drop: ALL` + `no-new-privileges` still hold).
- `sandbox tools --unrestricted` — adds `NET_RAW`/`NET_ADMIN` + full internet for the tools
  sidecar.

Use them only when you understand the trade-off.

## Single-user-host assumption

The per-sandbox control directory and its socket are protected by host filesystem permissions
under the assumption of a **single-user host**. On a shared/multi-user host, another local user
who can reach the control dir could tamper with credential records (they still cannot *read*
secrets back — values are masked). Do not run this on a multi-tenant host without tightening
those permissions.

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories on this repository
(**Security → Report a vulnerability**) rather than a public issue. A proof-of-concept for any
egress bypass or credential-leak path is especially appreciated.
