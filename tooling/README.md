# Tooling — the reproducible hunting box

This directory builds the **hunting container**: a lean Kali-based box with the
exact recon/exploitation toolset the framework uses. The container is the
reproducible, disposable hunting environment — build it once, hunt from inside
it, throw it away. `bootstrap.sh` installs the *same* toolset on a bare Kali VM.

See [`../docs/PLAN.md`](../docs/PLAN.md) (Phase 0) and
[`../docs/legal-scope.md`](../docs/legal-scope.md) for context. **Only test
assets inside an authorized program scope.**

## Layout

```
tooling/
  docker/
    Dockerfile      # curated Kali image (no kali-linux-* metapackages)
    compose.yml     # hunter + mitmproxy services (+ commented Caido stub)
    .dockerignore
  bootstrap.sh      # idempotent installer for bare Kali VMs (mirrors Dockerfile)
  mcp/README.md     # how the MCP servers (Caido skill, HexStrike) wire in
```

## Quickstart (container)

```sh
cd tooling/docker

# 1) Build the image (needs network — go install / git clone happen here).
docker compose build

# 2) Drop into an interactive hunting shell (repo mounted at /workspace).
docker compose run --rm hunter

# 3) Start the proxy (separately, in the background) when you need it.
docker compose up -d mitmproxy
```

On first run inside the container, fetch nuclei templates (kept out of the
image on purpose, so they stay fresh):

```sh
nuclei -update-templates
```

## Quickstart (bare Kali VM)

```sh
./tooling/bootstrap.sh            # install everything (skips what's present)
./tooling/bootstrap.sh --update   # refresh go tools + nuclei-templates
```

Then make sure your PATH includes the tool dirs (bootstrap reminds you):

```sh
export PATH="$HOME/go/bin:$HOME/.local/bin:$PATH"
```

## Installed toolset

- **Subdomain/DNS:** subfinder, amass, assetfinder, chaos, dnsx, puredns (+massdns)
- **Probe/scan:** httpx, naabu, nmap
- **Crawl/URLs:** katana, gau, waybackurls
- **Content:** ffuf (with SecLists/Assetnote wordlists, mounted)
- **Vuln:** nuclei (+templates, fetched at runtime), dalfox, sqlmap
- **JS/secrets:** jsluice, trufflehog
- **Android:** jadx, apktool, apkleaks, objection (frida/MobSF run separately — see PLAN)
- **Proxy:** mitmproxy/mitmweb (service), Caido (on host — see below)
- **Search/orchestration:** ripgrep (`rg`), gron, **task** (go-task — now bundled)

### Source analysis & deobfuscation

- **SAST / source-to-sink:** semgrep, ast-grep (`ast-grep` / `sg`), tree-sitter CLI
- **JS deobfuscation / unminification:** webcrack, js-beautify, `@wakaru/unminify`,
  `@wakaru/unpacker`, restringer
- **Pivot search:** ripgrep (`rg`) backs the AI-triage "give me commands to run"
  loop; gron flattens JSON for grep-friendly diffing

**CodeQL (optional, manual — NOT installed):** too heavy to bake in. Add it by
hand when you need dataflow queries: download the CLI from
https://github.com/github/codeql-cli-binaries/releases and unzip it onto your
PATH (you also need the query packs from https://github.com/github/codeql).

### PoC & replay (hurl)

[`hurl`](https://hurl.dev) is bundled as our plain-text HTTP request format for
PoCs and validator replay. Write a `.hurl` file and run it:

```sh
hurl --test poc.hurl          # assertion-style PoC / regression check
hurl --verbose request.hurl   # one-off replay with full request/response
```

The image installs `hurl` from apt when available, otherwise from the pinned
upstream static release (`HURL_VERSION` in the Dockerfile / bootstrap.sh — bump
both to upgrade).

## Wordlists (`/opt/wordlists`)

Wordlists are a **mounted named volume**, not baked into the image (they are
multi-GB). Populate the volume once. From inside the `hunter` container:

```sh
# SecLists (shallow clone keeps it smaller)
git clone --depth 1 https://github.com/danielmiessler/SecLists.git \
    /opt/wordlists/seclists

# Assetnote wordlists (per PLAN.md — used for content discovery).
# Browse https://wordlists.assetnote.io/ and pull what you need, e.g.:
mkdir -p /opt/wordlists/assetnote
wget -q https://wordlists-cdn.assetnote.io/data/manual/best-dns-wordlist.txt \
    -O /opt/wordlists/assetnote/best-dns-wordlist.txt
wget -q https://wordlists-cdn.assetnote.io/data/automated/httparchive_directories_1m_2024_05_28.txt \
    -O /opt/wordlists/assetnote/httparchive_directories.txt
```

Because it is a named volume, the data persists across container rebuilds. To
inspect/clear it: `docker volume inspect bb-hunter_wordlists`.

## Proxy & Android traffic

### Ports

| Port | Service  | Purpose                                  |
|------|----------|------------------------------------------|
| 8080 | mitmproxy| HTTP(S) intercept proxy (point clients here) |
| 8081 | mitmweb  | mitmproxy web UI (`http://localhost:8081`)   |

Captured flows are written to the shared `mitm-flows` volume
(`/flows/captured.mitm`), readable from the `hunter` container for pipeline
transforms (`mitmdump`-based addons).

### Pointing an Android device/emulator at mitmproxy

1. Start the proxy: `docker compose up -d mitmproxy`.
2. On the **device/emulator**, set the Wi-Fi proxy to your **host machine's IP**
   and port **8080** (the container publishes 8080 to the host). For the Android
   emulator you can also launch it with `-http-proxy http://<host-ip>:8080`.
3. Install the mitmproxy CA so HTTPS can be intercepted:
   - On the device browse to `http://mitm.it` (served by mitmproxy) and install
     the Android cert, **or** push the CA at
     `mitm-certs:/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.cer`.
   - Android 7+ ignores user CAs for app traffic. For apps you are authorized to
     test, install the CA as a **system** cert on a rooted device/emulator, or
     use **objection** / a Frida SSL-pinning bypass (both installed) to defeat
     pinning. Only do this on apps in scope.
4. Discovered endpoints/secrets from captured flows pivot back into the web
   recon pipeline (see PLAN.md Phase 2 Android track).

The `mitm-certs` volume persists the CA across restarts, so a cert installed on
the device stays valid.

## Caido (human-in-the-loop)

Caido is **not** containerized here. Run the Caido desktop app on your host for
manual, AI-assisted deep dives (HTTPQL, Replay, Automate, Findings). It connects
to this framework through its official **Claude Skill**, not via compose:

- Skill: https://github.com/caido/skills

A commented-out `caido` service stub exists in `compose.yml` if you ever want a
headless instance, but desktop Caido + the skill is the supported workflow.

## Environment variables

Set these in a `.env` next to `docker/compose.yml`, or export them before
`docker compose`/`bootstrap.sh`:

| Variable         | Used by                                   | Required |
|------------------|-------------------------------------------|----------|
| `CHAOS_API_KEY`  | chaos-client (ProjectDiscovery scopes)    | for chaos |
| `GITHUB_TOKEN`   | github-subdomains / diodb / amass sources | recommended |
| `SHODAN_API_KEY` | infra recon enrichment                    | optional |

Example `tooling/docker/.env`:

```dotenv
CHAOS_API_KEY=xxxxxxxx
GITHUB_TOKEN=ghp_xxxxxxxx
SHODAN_API_KEY=xxxxxxxx
```

## Notes

- The image deliberately omits the giant `kali-linux-*` metapackages — only the
  curated toolset is installed, to keep it lean.
- `go install`, `git clone`, npm global, and the hurl release-fallback steps need
  **network at build time**.
- nuclei templates and wordlists are intentionally runtime/volume concerns, not
  baked into the image (freshness + size).
