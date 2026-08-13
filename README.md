# agent-web-api

**Self-hosted web search + fetch for coding agents. No cloud search APIs, no quotas, no per-call pricing.**

Coding agents want two web capabilities: *search* (query → ranked URLs) and *fetch* (URL → clean
markdown). The hosted options — Brave Search API, Exa, Tavily, Firecrawl — are metered cloud
services. This repo is the ~$0 homelab alternative: [SearXNG](https://github.com/searxng/searxng)
for the search half and **toolshed** — a small FastAPI service wrapping
[trafilatura](https://github.com/adbar/trafilatura) (HTML) and
[pymupdf4llm](https://github.com/pymupdf/RAG) (PDF) for the fetch half, plus a
Claude-style artifact store (immutable documents at unguessable capability URLs) — one reverse-proxy vhost
routing both under a single hostname, and a [pi](https://github.com/badlogic/pi-mono) extension
wiring them in as `web_search` / `web_fetch` tools. Total footprint: ~230MiB RAM.

## Architecture

One internal hostname (examples below use `web.homelab.example`), path-routed at the reverse proxy:

| Path | Backend | Purpose |
|------|---------|---------|
| `/` | searxng | Search UI (browser) |
| `/search?q=...&format=json` | searxng | JSON search API (agents) |
| `/fetch?url=...` | toolshed | URL → clean markdown JSON (HTML or PDF) |
| `/mcp` | toolshed | MCP (Streamable HTTP): `web_search`, `web_fetch`, `store_artifact`, `get_artifact`, `list_artifacts` |
| `/a/{id}` | toolshed | Serve a stored artifact (renders in browser) |
| `/artifacts` | toolshed | Store (POST) / list (GET) artifacts |
| `/healthz` | toolshed | Liveness |
| `/jobs` | toolshed → jobs | Submit/inspect ephemeral agent jobs (see below) |

- **SearXNG** — metasearch over Google/Bing/DDG/Brave. Its rate limiter is OFF on purpose: the
  limiter's bot detection blocks exactly the non-browser JSON calls agents make. Access control
  belongs at the proxy (LAN allow-list) instead. Config in `searxng/settings.yml`.
- **toolshed** (`toolshed/`) — FastAPI + FastMCP. `web_fetch` returns title/author/date + markdown
  for HTML pages and PDFs (content-type sniffed).
  SSRF guard refuses private/loopback targets so a prompt-injected agent can't probe your LAN
  through it (`ALLOW_PRIVATE=1` to disable). `MAX_CONTENT_CHARS` truncates (default 400k).
- **Proxy vhost** — `swag/agent-web.subdomain.conf.example` (linuxserver SWAG flavored nginx):
  LAN-only guard, `/fetch|/healthz` → extractor, everything else → searxng, so SearXNG's native
  `/search` endpoint doubles as the API path with zero subpath configuration.
- Neither container publishes host ports; the proxy reaches them on the shared docker network.

## Quickstart

```yaml
# compose services (adapt to your stack)
searxng:
  image: searxng/searxng:latest
  environment:
    - SEARXNG_BASE_URL=https://web.homelab.example/
  volumes:
    - ./searxng:/etc/searxng
  mem_limit: 512m

toolshed:
  build: ./toolshed
  environment:
    - PUBLIC_BASE=https://web.homelab.example
  volumes:
    - ./data/toolshed:/data
  # external DNS if your LAN resolver runs blocklists — ad-list entries
  # break legit article fetches
  dns: [1.1.1.1, 8.8.8.8]
  mem_limit: 512m
```

1. Copy `searxng/settings.yml`, set `base_url` and a fresh `secret_key` (`openssl rand -hex 32`).
2. Adapt the proxy conf; keep it LAN/VPN-only — an open SearXNG instance gets found and hammered.
3. Point internal DNS at your proxy.
4. Smoke test: `curl 'https://web.homelab.example/search?q=test&format=json'` and
   `curl 'https://web.homelab.example/fetch?url=https://example.com'`.

## MCP clients (Claude Code, LM Studio, anything MCP-native)

The same two tools are served over MCP Streamable HTTP at `/mcp` — [FastMCP](https://github.com/jlowin/fastmcp)
mounted inside the toolshed container, no extra service. Point any MCP host at it:

```json
{
  "mcpServers": {
    "homelab-web": { "url": "https://web.homelab.example/mcp" }
  }
}
```

LM Studio: Program tab → Install → Edit mcp.json (0.3.17+). Claude Code:
`claude mcp add --transport http homelab-web https://web.homelab.example/mcp`.
Keep the endpoint LAN-only at the proxy; the MCP layer itself is unauthenticated.

## pi integration

`pi-extension/web-selfhosted.ts` registers `web_search` + `web_fetch`. Install by symlinking into
`~/.pi/agent/extensions/`, then tell it where the stack lives (either works):

- `~/.pi/web-selfhosted.json` → `{"base": "https://web.homelab.example"}`
- or env `SELFHOSTED_WEB_BASE=https://web.homelab.example`

If you also run [pi-web-access](https://www.npmjs.com/package/pi-web-access), disable its cloud
`web_search` via `~/.pi/web-search.json` `{"webSearch":{"enabled":false}}` so the names don't
collide — its `fetch_content` is worth keeping for GitHub cloning and JS-rendered pages, which
trafilatura's plain HTTP fetch can't handle.

## Jobs: ephemeral agent runner

POST a prompt; a fully-empowered agent runs it in a throwaway docker container, publishes
results to the artifact store, and evaporates. The design principle: **capability walls on the
outside, full power on the inside.** The container *is* the sandbox — inside it the agent gets
unrestricted bash/files/pytest (the `full` image adds Playwright + Chromium) with no permission
prompts. Blast radius = the container: no host mounts, memory/CPU caps, wall-clock timeout,
removed after log capture. The only doors out are the model API and toolshed.

Three parts:

- **`runner/`** — `runner.py`, a plain OpenAI-compatible function-calling loop (httpx, no
  framework) baked into `job-runner:lite` (python + git + pytest + node) and `job-runner:full`
  (+ Playwright/Chromium). Tools: `bash`, `read_file`/`write_file` (workspace-jailed),
  `web_search`/`web_fetch`/`store_artifact` (via toolshed), `done`. Hard caps on turns and
  total tokens; every exit path stores a report artifact (`[DONE]`/`[CAPPED]`/`[ERROR]`) — a
  job never evaporates without a trace.
- **`jobs/`** — FastAPI orchestrator that owns the docker sock (the *agent* never sees it).
  FIFO queue, fixed concurrency (default 3), wall clock kill (default 30min), SQLite job
  history with captured logs. Runs on whatever box should host the containers — it doesn't
  have to be the toolshed host. Model routing (`deepseek`/`mimo`/`lmstudio`) maps a job's
  `model` to a base URL + API key from the orchestrator's env; keys are injected per-container,
  never baked into images.
- **Toolshed front door** — proxies `POST/GET /jobs` to the orchestrator, serves a zero-JS
  `/shed/jobs` status page, and exposes `run_job` + `job_status` MCP tools. Yes, that means
  agents can spawn agents; the caps are what make that sane.

```bash
curl -X POST https://web.homelab.example/jobs -H 'Content-Type: application/json' \
  -d '{"prompt": "write fizzbuzz + tests, run pytest, store a report", "image": "lite"}'
# → {"job_id": "..."}  — watch /shed/jobs, artifacts land in /shed/artifacts
```

Accepted v1 tradeoffs: job containers get normal egress (a prompt-injected agent could probe
your LAN — restrict with a custom docker network if that bothers you), and the orchestrator is
unauthenticated on the LAN (keep it off your reverse proxy).

## Tradeoffs

- SearXNG's upstream engines occasionally captcha/rate-limit one engine and result quality dips
  until it rotates. Multiple engines are enabled so a single flake doesn't zero out results.
- No JavaScript execution on fetch. Docs sites, blogs, READMEs, news — fine. SPA-heavy pages
  come back hollow; bolt on a headless-browser tier (Crawl4AI, self-hosted Firecrawl) if the
  misses annoy you.

---

Built overnight by Claude (Fable 5) running unattended with infrastructure access; verified by
headless agent runs on DeepSeek v4 Pro and MiMo 2.5 Ultraspeed against the live stack.
