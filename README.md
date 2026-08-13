# agent-web-api

**Self-hosted web search + fetch for coding agents. No cloud search APIs, no quotas, no per-call pricing.**

Coding agents want two web capabilities: *search* (query → ranked URLs) and *fetch* (URL → clean
markdown). The hosted options — Brave Search API, Exa, Tavily, Firecrawl — are metered cloud
services. This repo is the ~$0 homelab alternative: [SearXNG](https://github.com/searxng/searxng)
for the search half, a ~100-line FastAPI wrapper around
[trafilatura](https://github.com/adbar/trafilatura) for the fetch half, one reverse-proxy vhost
routing both under a single hostname, and a [pi](https://github.com/badlogic/pi-mono) extension
wiring them in as `web_search` / `web_fetch` tools. Total footprint: ~230MiB RAM.

## Architecture

One internal hostname (examples below use `web.homelab.example`), path-routed at the reverse proxy:

| Path | Backend | Purpose |
|------|---------|---------|
| `/` | searxng | Search UI (browser) |
| `/search?q=...&format=json` | searxng | JSON search API (agents) |
| `/fetch?url=...` | trafilatura-api | URL → clean markdown JSON |
| `/mcp` | trafilatura-api | MCP (Streamable HTTP): `web_search` + `web_fetch` tools |
| `/healthz` | trafilatura-api | Liveness |

- **SearXNG** — metasearch over Google/Bing/DDG/Brave. Its rate limiter is OFF on purpose: the
  limiter's bot detection blocks exactly the non-browser JSON calls agents make. Access control
  belongs at the proxy (LAN allow-list) instead. Config in `searxng/settings.yml`.
- **trafilatura-api** — FastAPI wrapper (`trafilatura-api/`). Returns title/author/date + markdown.
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

trafilatura-api:
  build: ./trafilatura-api
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
mounted inside the trafilatura-api container, no extra service. Point any MCP host at it:

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

## Tradeoffs

- SearXNG's upstream engines occasionally captcha/rate-limit one engine and result quality dips
  until it rotates. Multiple engines are enabled so a single flake doesn't zero out results.
- No JavaScript execution on fetch. Docs sites, blogs, READMEs, news — fine. SPA-heavy pages
  come back hollow; bolt on a headless-browser tier (Crawl4AI, self-hosted Firecrawl) if the
  misses annoy you.

---

Built overnight by Claude (Fable 5) running unattended with infrastructure access; verified by
headless agent runs on DeepSeek v4 Pro and MiMo 2.5 Ultraspeed against the live stack.
