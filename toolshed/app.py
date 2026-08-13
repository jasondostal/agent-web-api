"""toolshed — self-hosted web tools for coding agents.

Capabilities:
  web_search      → SearXNG metasearch (proxied via SEARXNG_URL)
  web_fetch       → URL → clean markdown; HTML via trafilatura, PDF via pymupdf4llm
  artifacts       → store/serve agent-created documents at capability URLs
                    (unguessable UUID paths, immutable, Claude-artifacts style)

Transports:
  REST:  GET  /fetch?url=...       GET /healthz
         POST /artifacts           GET /a/{id}        GET /artifacts (list)
  MCP:   /mcp (Streamable HTTP)    tools: web_search, web_fetch,
                                   store_artifact, get_artifact, list_artifacts

Sits behind a LAN-only reverse proxy. SSRF guard blocks private/loopback fetch
targets unless ALLOW_PRIVATE=1 (agents fetching arbitrary URLs shouldn't be
able to probe the LAN through this box).
"""

import html as html_mod
import ipaddress
import os
import socket
import sqlite3
import uuid
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
import pymupdf
import pymupdf4llm
import trafilatura
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastmcp import FastMCP
from pydantic import BaseModel

ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "0") == "1"
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "400000"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_ARTIFACT_BYTES = int(os.environ.get("MAX_ARTIFACT_BYTES", str(10 * 1024 * 1024)))
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "http://localhost:8080").rstrip("/")
DATA_DIR = os.environ.get("DATA_DIR", "./data")

ARTIFACTS_DIR = os.path.join(DATA_DIR, "artifacts")
DB_PATH = os.path.join(DATA_DIR, "toolshed.db")

USER_AGENT = "Mozilla/5.0 (compatible; toolshed/0.3; +self-hosted agent tools)"

TimeRange = Literal["day", "week", "month", "year"]

# Types we serve inline; anything else goes out as a download.
INLINE_TYPES = {
    "text/html",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "image/svg+xml",
}
TEXT_TYPES = INLINE_TYPES  # types get_artifact will return as text over MCP


def _init_storage() -> None:
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS artifacts (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size         INTEGER NOT NULL,
                created      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )"""
        )


_init_storage()


# --- fetch / extract -------------------------------------------------------


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


def _download(url: str) -> tuple[bytes, str, str]:
    """Fetch url → (body, content_type, final_url). Re-checks SSRF on the
    final URL so a redirect can't bounce the fetch into the LAN."""
    _assert_public_target(url)
    try:
        r = httpx.get(
            url,
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"fetch failed: {e}")
    final_url = str(r.url)
    if final_url != url:
        _assert_public_target(final_url)
    if r.status_code >= 400:
        raise HTTPException(502, f"upstream returned HTTP {r.status_code}: {url}")
    if len(r.content) > MAX_DOWNLOAD_BYTES:
        raise HTTPException(413, f"response too large (> {MAX_DOWNLOAD_BYTES} bytes): {url}")
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    return r.content, ctype, final_url


def _extract_pdf(body: bytes, url: str) -> dict:
    try:
        doc = pymupdf.open(stream=body, filetype="pdf")
        md = pymupdf4llm.to_markdown(doc)
        meta = doc.metadata or {}
    except Exception as e:
        raise HTTPException(422, f"PDF extraction failed: {e}")
    truncated = len(md) > MAX_CONTENT_CHARS
    return {
        "url": url,
        "title": meta.get("title") or None,
        "author": meta.get("author") or None,
        "date": None,
        "sitename": None,
        "kind": "pdf",
        "truncated": truncated,
        "content": md[:MAX_CONTENT_CHARS] if truncated else md,
    }


def _extract_html(body: bytes, url: str, format: str) -> dict:
    html = body.decode("utf-8", errors="replace")
    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown" if format == "markdown" else "txt",
        include_links=True,
        include_tables=True,
        include_images=False,
        favor_recall=True,
    )
    if md is None:
        raise HTTPException(422, f"could not extract main content: {url}")
    meta = trafilatura.extract_metadata(html)
    truncated = len(md) > MAX_CONTENT_CHARS
    return {
        "url": url,
        "title": meta.title if meta else None,
        "author": meta.author if meta else None,
        "date": meta.date if meta else None,
        "sitename": meta.sitename if meta else None,
        "kind": "html",
        "truncated": truncated,
        "content": md[:MAX_CONTENT_CHARS] if truncated else md,
    }


def extract_page(url: str, format: str = "markdown") -> dict:
    """Fetch url and extract main content. PDFs and HTML both land as
    markdown. Raises HTTPException on failure."""
    body, ctype, final_url = _download(url)
    if ctype == "application/pdf" or body[:5] == b"%PDF-":
        return _extract_pdf(body, final_url)
    return _extract_html(body, final_url, format)


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


# --- artifact store --------------------------------------------------------


def _artifact_path(artifact_id: str) -> str:
    return os.path.join(ARTIFACTS_DIR, artifact_id)


def save_artifact(content: bytes, title: str, content_type: str) -> dict:
    if len(content) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, f"artifact too large (> {MAX_ARTIFACT_BYTES} bytes)")
    if not content:
        raise HTTPException(400, "empty artifact content")
    artifact_id = str(uuid.uuid4())
    with open(_artifact_path(artifact_id), "wb") as f:
        f.write(content)
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO artifacts (id, title, content_type, size) VALUES (?, ?, ?, ?)",
            (artifact_id, title, content_type, len(content)),
        )
    return {"id": artifact_id, "url": f"{PUBLIC_BASE}/a/{artifact_id}", "size": len(content)}


def load_artifact(artifact_id: str) -> tuple[bytes, dict]:
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT id, title, content_type, size, created FROM artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "no such artifact")
    try:
        with open(_artifact_path(artifact_id), "rb") as f:
            content = f.read()
    except FileNotFoundError:
        raise HTTPException(410, "artifact metadata exists but content is gone")
    meta = dict(zip(("id", "title", "content_type", "size", "created"), row))
    return content, meta


def recent_artifacts(limit: int = 50) -> list[dict]:
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT id, title, content_type, size, created FROM artifacts "
            "ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(zip(("id", "title", "content_type", "size", "created"), r)) for r in rows]


def delete_artifact(artifact_id: str) -> None:
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "no such artifact")
    try:
        os.remove(_artifact_path(artifact_id))
    except FileNotFoundError:
        pass


def _normalize_artifact_id(id_or_url: str) -> str:
    s = id_or_url.strip()
    if "/" in s:
        s = s.rstrip("/").rsplit("/", 1)[-1]
    return s


# --- MCP transport ---------------------------------------------------------

mcp = FastMCP(
    "toolshed",
    instructions="Self-hosted agent tools: web_search (SearXNG metasearch), "
    "web_fetch (URL to markdown, handles PDFs), and an artifact store for "
    "saving/sharing documents at stable URLs. LAN-hosted, no quotas — use freely.",
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
    """Fetch a URL and extract its main content as clean markdown. Handles
    HTML pages and PDF documents. Cannot execute JavaScript."""
    try:
        d = extract_page(url)
    except HTTPException as e:
        return f"web_fetch failed: {e.detail}"
    header = "\n".join(
        s
        for s in (
            f"# {d['title']}" if d["title"] else None,
            f"Author: {d['author']}" if d["author"] else None,
            f"Date: {d['date']}" if d["date"] else None,
            f"Site: {d['sitename']}" if d["sitename"] else None,
            "(PDF)" if d["kind"] == "pdf" else None,
            "(content truncated)" if d["truncated"] else None,
        )
        if s
    )
    return f"{header}\n\n{d['content']}"


@mcp.tool
def store_artifact(
    content: str,
    title: str,
    content_type: Literal[
        "text/html", "text/markdown", "text/plain", "application/json", "image/svg+xml", "text/csv"
    ] = "text/markdown",
) -> str:
    """Store a document (report, HTML page, notes, data) and get back a stable
    URL where it can be viewed in a browser or retrieved by other agents.
    Artifacts are immutable — store a new one to publish a revision."""
    try:
        result = save_artifact(content.encode("utf-8"), title, content_type)
    except HTTPException as e:
        return f"store_artifact failed: {e.detail}"
    return f"Stored \"{title}\" ({result['size']} bytes)\nURL: {result['url']}"


@mcp.tool
def get_artifact(id_or_url: str) -> str:
    """Retrieve a stored artifact's content by its id or URL."""
    try:
        content, meta = load_artifact(_normalize_artifact_id(id_or_url))
    except HTTPException as e:
        return f"get_artifact failed: {e.detail}"
    if meta["content_type"] not in TEXT_TYPES:
        return f"Artifact \"{meta['title']}\" is binary ({meta['content_type']}, {meta['size']} bytes): {PUBLIC_BASE}/a/{meta['id']}"
    return f"# {meta['title']} ({meta['content_type']}, {meta['created']})\n\n{content.decode('utf-8', errors='replace')}"


@mcp.tool
def list_artifacts(limit: int = 20) -> str:
    """List recently stored artifacts with their titles and URLs."""
    rows = recent_artifacts(max(1, min(limit, 100)))
    if not rows:
        return "No artifacts stored yet."
    return "\n".join(
        f"- {r['title']} ({r['content_type']}, {r['size']}b, {r['created']}) {PUBLIC_BASE}/a/{r['id']}"
        for r in rows
    )


mcp_app = mcp.http_app(path="/mcp")

# --- REST transport --------------------------------------------------------
# FastAPI routes are matched before the root mount, so explicit routes stay
# REST while everything else (i.e. /mcp) falls through to the MCP app.

app = FastAPI(
    title="toolshed",
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


class ArtifactIn(BaseModel):
    content: str
    title: str
    content_type: str = "text/markdown"


@app.post("/artifacts")
def post_artifact(a: ArtifactIn):
    return JSONResponse(save_artifact(a.content.encode("utf-8"), a.title, a.content_type))


@app.get("/artifacts")
def get_artifacts(limit: int = Query(50, le=200)):
    return JSONResponse(recent_artifacts(limit))


@app.get("/a/{artifact_id}")
def serve_artifact(artifact_id: str):
    content, meta = load_artifact(artifact_id)
    ctype = meta["content_type"]
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    if ctype not in INLINE_TYPES:
        headers["Content-Disposition"] = f'attachment; filename="{meta["id"]}"'
        ctype = "application/octet-stream"
    if ctype == "text/markdown":
        ctype = "text/plain; charset=utf-8"
    return Response(content=content, media_type=ctype, headers=headers)


# --- admin: the shed inventory ---------------------------------------------
# Zero-JS on purpose (strict CSP proxies silently kill inline scripts).
# Deletion is deliberately human-only: there is no MCP delete tool — agents
# can publish artifacts, only a person at this page can destroy them.

_SHED_CSS = """
:root{--night:#081210;--panel:#0d1a16;--line:#1c312a;--mallard:#2bd98e;
--lantern:#ffb454;--bone:#d8e7de;--reed:#5f7a6e}
*{box-sizing:border-box;margin:0}
body{background:var(--night);color:var(--bone);font-family:ui-monospace,Menlo,monospace;
padding:2.5em 1.5em;max-width:1000px;margin:0 auto}
h1{color:var(--mallard);font-size:1.3em;margin-bottom:.25em}
.sub{color:var(--reed);margin-bottom:2em}
table{width:100%;border-collapse:collapse}
th{color:var(--lantern);text-align:left;padding:.5em .75em;border-bottom:1px solid var(--line);
font-size:.85em;text-transform:uppercase;letter-spacing:.08em}
td{padding:.55em .75em;border-bottom:1px solid var(--line);font-size:.9em;vertical-align:top}
tr:hover td{background:var(--panel)}
a{color:var(--mallard);text-decoration:none}
a:hover{text-decoration:underline}
.guid{color:var(--reed);font-size:.8em}
.type{color:var(--lantern);font-size:.8em}
.del button{background:none;border:1px solid var(--line);color:var(--reed);
font-family:inherit;font-size:.8em;padding:.25em .6em;cursor:pointer;border-radius:3px}
.del button:hover{border-color:#e05252;color:#e05252}
.empty{color:var(--reed);padding:3em 0;text-align:center}
"""


def _fmt_size(n: int) -> str:
    return f"{n}b" if n < 1024 else (f"{n / 1024:.1f}kb" if n < 1024**2 else f"{n / 1024**2:.1f}mb")


@app.get("/shed", response_class=HTMLResponse)
def shed_admin():
    rows = recent_artifacts(200)
    total = _fmt_size(sum(r["size"] for r in rows))
    if rows:
        body = "".join(
            f"<tr>"
            f"<td><a href='/a/{r['id']}'>{html_mod.escape(r['title'] or '(untitled)')}</a>"
            f"<div class='guid'>{r['id']}</div></td>"
            f"<td class='type'>{html_mod.escape(r['content_type'])}</td>"
            f"<td>{_fmt_size(r['size'])}</td>"
            f"<td class='guid'>{r['created']}</td>"
            f"<td class='del'><form method='post' action='/shed/delete'>"
            f"<input type='hidden' name='artifact_id' value='{r['id']}'>"
            f"<button>delete</button></form></td>"
            f"</tr>"
            for r in rows
        )
        table = (
            "<table><tr><th>artifact</th><th>type</th><th>size</th><th>created</th><th></th></tr>"
            + body
            + "</table>"
        )
    else:
        table = "<div class='empty'>the shed is empty — agents haven't stored anything yet</div>"
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>toolshed</title>"
        f"<style>{_SHED_CSS}</style></head><body>"
        f"<h1>$ ls ~/toolshed</h1>"
        f"<div class='sub'>{len(rows)} artifact{'s' if len(rows) != 1 else ''} · {total} · "
        f"immutable · deletion is human-only</div>"
        f"{table}</body></html>"
    )


@app.post("/shed/delete")
def shed_delete(artifact_id: str = Form(...)):
    delete_artifact(_normalize_artifact_id(artifact_id))
    return RedirectResponse("/shed", status_code=303)


@app.delete("/artifacts/{artifact_id}")
def rest_delete(artifact_id: str):
    delete_artifact(artifact_id)
    return JSONResponse({"deleted": artifact_id})


app.mount("/", mcp_app)
