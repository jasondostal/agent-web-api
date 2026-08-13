"""trafilatura-api — self-hosted URL → clean-markdown extraction for coding agents.

GET /fetch?url=...            → JSON {url, title, author, date, sitename, markdown}
GET /fetch?url=...&format=txt → plain text body instead of markdown
GET /healthz                  → liveness

Sits behind a LAN-only reverse proxy at /fetch. Pairs with SearXNG for the
search half. SSRF guard blocks private/loopback targets unless ALLOW_PRIVATE=1
(agents fetching arbitrary URLs shouldn't be able to probe the LAN through
this box).
"""

import ipaddress
import os
import socket
from urllib.parse import urlparse

import trafilatura
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "0") == "1"
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "400000"))

app = FastAPI(title="trafilatura-api", docs_url="/fetch/docs", openapi_url="/fetch/openapi.json")


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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/fetch")
def fetch(
    url: str = Query(..., description="URL to fetch and extract"),
    format: str = Query("markdown", pattern="^(markdown|txt)$"),
):
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

    return JSONResponse(
        {
            "url": url,
            "title": meta.title if meta else None,
            "author": meta.author if meta else None,
            "date": meta.date if meta else None,
            "sitename": meta.sitename if meta else None,
            "truncated": truncated,
            "content": body,
        }
    )
