# SearXNG — web search for sandboxed agents

A self-hosted meta-search instance that gives **every** harness web search through one
endpoint: omp calls it with its built-in `web_search` tool, and pi / prime-agent / Claude
Code (which loses its server-side `WebSearch` the moment it runs against a local model)
reach it through the `websearch` command baked into the workload image.

Run it **here, on your server box — not inside a sandbox.** A sandbox sidecar shares the
gateway's network namespace, so SearXNG's own scraping traffic would need google,
duckduckgo, startpage, … on the egress allowlist and would be MITM'd, which is both a wide
hole in the boundary and the fastest route to CAPTCHAs. Running it outside means a sandbox
allowlists exactly one hostname, and every sandbox shares one instance. See plan §19.

## Deploy

```bash
cp env.example .env           # not .env.example — the sandbox FUSE filter hard-denies .env*
openssl rand -hex 32          # -> SEARXNG_SECRET
$EDITOR .env                  # also set SEARXNG_BASE_URL to the public URL
docker compose up -d

# It works when this returns JSON with a "results" array:
curl "http://127.0.0.1:8888/search?q=test&format=json" | head -c 400
```

Then front it with your existing reverse proxy for TLS (e.g. `searxng.example.com` →
`127.0.0.1:8888`), the same way your Ollama box is exposed.

> **Put authentication in front of it if it is reachable from the internet.** This config
> disables the bot-detection limiter (agents are not browsers), so an exposed instance is
> an open proxy for search traffic and will get you rate-limited by the engines. Basic auth
> is enough; omp can send it (`searxng.basicUsername` / `searxng.basicPassword`), and the
> `websearch` client accepts credentials in the URL.

## Point a sandbox at it

```bash
./sandbox up --source ~/dev/repo --harness omp --llm-backend ollama \
             --search-url https://searxng.example.com
```

That one flag: adds the host to the sandbox's egress allowlist, sets `SEARXNG_ENDPOINT`
so omp's native `web_search` uses it, points the in-container `websearch` command at it,
and installs a `websearch` skill into each harness home so the model knows the command
exists. `--search none` turns it back off.

## The four things that break a fresh install

All four are already handled in `settings.yml` — listed here because you will hit them if
you deploy SearXNG anywhere else:

| symptom | cause | fix |
|---|---|---|
| `format=json` returns an error page | JSON output is **off by default** | `search.formats: [html, json]` |
| requests 403 / get challenged | limiter + bot detection reject non-browser clients | `server.limiter: false` (private instances only) |
| `GET /search?…&format=json` → 405 | SearXNG defaults to `method: POST` | `server.method: "GET"` |
| engines return nothing after a while | that engine blocked the instance | drop or swap it in `settings.yml`; back off on retries |

## Operational notes

- Results are **snippets, not page content**. Reading a linked page still needs an
  explicit `./sandbox allow --url <domain>` — that is deliberate (plan §19: a general
  fetch lane is an exfiltration channel).
- ~20 results per query, not tunable through the API; the `websearch` client trims to
  `-n` locally.
- Engines get blocked from time to time — that is the maintenance cost of self-hosting
  meta-search. If you would rather not babysit it, Brave Search API or Tavily fit the
  credential broker cleanly (the key lives in the gateway, never in the container).
- Valkey is only used for caching here (the limiter is off); it is included because
  upstream expects it and it quiets the startup warnings.
