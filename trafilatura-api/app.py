"""trafilatura-api — self-hosted web search + URL → clean-markdown for agents.

Transports:
  REST:  GET /fetch?url=...   → JSON {url, title, author, date, sitename, markdown}
         GET /healthz         → liveness
  MCP:   /mcp (Streamable HTTP) → tools: web_search, web_fetch
         (for MCP-native clients: Claude Code, LM Studio, etc.)

Search is proxied to a SearXNG instance (SEARXNG_URL); extraction runs
in-process via trafilatura. Sits behind a LAN-only reverse proxy. SSRF guard
blocks private/loopback fetch targets unless ALLOW_PRIVATE=1 (agents fetching
arbitrary URLs shouldn't be able to probe the LAN through this box).
"""

import ipaddress
import os
import socket
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
import trafilatura
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "0") == "1"
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "400000"))
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")

TimeRange = Literal["day", "week", "month", "year"]


def _assert_public_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise HTTPException(400, "no hostname in url")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise HTTPException(502, f"cannot resolve host: {parsed.hostname}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(403, f"refusing private/internal target: {parsed.hostname}")


def extract_page(url: str, format: str = "markdown") -> dict:
    """Fetch url and extract main content. Raises HTTPException on failure."""
    _assert_public_target(url)

    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        raise HTTPException(502, f"fetch failed (blocked, timeout, or non-HTML): {url}")

    body = trafilatura.extract(
        downloaded,
        url=url,
        output_format="markdown" if format == "markdown" else "txt",
        include_links=True,
        include_tables=True,
        include_images=False,
        favor_recall=True,
    )
    if body is None:
        raise HTTPException(422, f"could not extract main content: {url}")

    meta = trafilatura.extract_metadata(downloaded)
    truncated = len(body) > MAX_CONTENT_CHARS
    if truncated:
        body = body[:MAX_CONTENT_CHARS]

    return {
        "url": url,
        "title": meta.title if meta else None,
        "author": meta.author if meta else None,
        "date": meta.date if meta else None,
        "sitename": meta.sitename if meta else None,
        "truncated": truncated,
        "content": body,
    }


def search_web(query: str, num_results: int = 8, time_range: Optional[str] = None) -> list[dict]:
    """Query SearXNG's JSON API. Raises HTTPException on upstream failure."""
    params = {"q": query, "format": "json"}
    if time_range:
        params["time_range"] = time_range
    try:
        r = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=20)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"searxng error: {e}")
    results = r.json().get("results", [])[: max(1, min(num_results, 20))]
    return [
        {
            "title": x.get("title"),
            "url": x.get("url"),
            "snippet": " ".join((x.get("content") or "").split()),
        }
        for x in results
    ]


# --- MCP transport ---------------------------------------------------------

mcp = FastMCP(
    "homelab-web",
    instructions="Self-hosted web access: web_search (SearXNG metasearch) and "
    "web_fetch (URL to clean markdown). LAN-hosted, no API quotas — use freely.",
)


@mcp.tool
def web_search(query: str, num_results: int = 8, time_range: Optional[TimeRange] = None) -> str:
    """Search the web. Returns numbered results with title, URL, and snippet.
    Follow up with web_fetch on promising URLs to read full content."""
    try:
        results = search_web(query, num_results, time_range)
    except HTTPException as e:
        return f"web_search failed: {e.detail}"
    if not results:
        return f"No results for: {query}"
    return "\n\n".join(
        f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results)
    )


@mcp.tool
def web_fetch(url: str) -> str:
    """Fetch a URL and extract its main content as clean markdown.
    Best for articles, docs, blogs, READMEs. Cannot execute JavaScript."""
    try:
        d = extract_page(url)
    except HTTPException as e:
        return f"web_fetch failed: {e.detail}"
    header = "\n".join(
        s
        for s in (
            f"# {d['title']}" if d["title"] else None,
            f"Date: {d['date']}" if d["date"] else None,
            f"Site: {d['sitename']}" if d["sitename"] else None,
            "(content truncated)" if d["truncated"] else None,
        )
        if s
    )
    return f"{header}\n\n{d['content']}"


mcp_app = mcp.http_app(path="/mcp")

# --- REST transport --------------------------------------------------------
# FastAPI routes are matched before the root mount, so /fetch and /healthz
# stay REST while everything else (i.e. /mcp) falls through to the MCP app.

app = FastAPI(
    title="trafilatura-api",
    docs_url="/fetch/docs",
    openapi_url="/fetch/openapi.json",
    lifespan=mcp_app.lifespan,
)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/fetch")
def fetch(
    url: str = Query(..., description="URL to fetch and extract"),
    format: str = Query("markdown", pattern="^(markdown|txt)$"),
):
    return JSONResponse(extract_page(url, format))


app.mount("/", mcp_app)
