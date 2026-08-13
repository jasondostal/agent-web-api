"""jobs — orchestrator for ephemeral agent job containers.

Runs on the dev box next to dockerd (sock mounted); toolshed proxies /jobs
here so the public face stays one hostname. This service is trusted code —
the agent containers it launches never see the docker sock.

POST /jobs            {prompt, model?, image?, repo?, caps?} → {job_id}
GET  /jobs            recent jobs
GET  /jobs/{id}       one job
GET  /jobs/{id}/logs  captured container stdout/stderr
PATCH /jobs/{id}      runner progress callbacks (turns/tokens/artifacts)

Concurrency: fixed worker pool over a FIFO queue (default 3). Wall clock:
container is killed after WALL_CLOCK_SECS. Containers are removed after log
capture — nothing accumulates.
"""

import json
import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

import docker
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")
TOOLSHED_URL = os.environ.get("TOOLSHED_URL", "https://web.homelab.example")
JOBS_URL_FOR_RUNNER = os.environ.get("JOBS_URL_FOR_RUNNER", "http://jobs-host.homelab.example:8815")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
WALL_CLOCK_SECS = int(os.environ.get("WALL_CLOCK_SECS", "1800"))
MAX_LOG_CHARS = 200_000

IMAGES = {"lite": "job-runner:lite", "full": "job-runner:full"}
MEM_LIMITS = {"lite": "1g", "full": "2g"}

# Model routing: job's model name → runner env. Keys come from this service's
# own env (.jobs.env on the host); they are never written to disk or images.
MODELS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-pro-ultraspeed",
        "key_env": "XIAOMI_MIMO_API_KEY",
    },
    "lmstudio": {
        "base_url": os.environ.get("LMSTUDIO_BASE_URL", "http://lmstudio.homelab.example:1234/v1"),
        "model": os.environ.get("LMSTUDIO_MODEL", ""),  # LM Studio serves whatever is loaded
        "key_env": None,
    },
}

FIELDS = (
    "id prompt model image repo state created started ended "
    "exit_code turns tokens artifact_urls error"
).split()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                prompt        TEXT NOT NULL,
                model         TEXT NOT NULL,
                image         TEXT NOT NULL,
                repo          TEXT,
                state         TEXT NOT NULL,
                created       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                started       TEXT,
                ended         TEXT,
                exit_code     INTEGER,
                turns         INTEGER DEFAULT 0,
                tokens        INTEGER DEFAULT 0,
                artifact_urls TEXT DEFAULT '[]',
                caps          TEXT DEFAULT '{}',
                logs          TEXT,
                error         TEXT
            )"""
        )


def _row_dict(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in FIELDS}
    d["artifact_urls"] = json.loads(d["artifact_urls"] or "[]")
    return d


def _update(job_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*cols.values(), job_id))


# --- execution -------------------------------------------------------------

docker_client = docker.from_env()
job_queue: "queue.Queue[str]" = queue.Queue()


def run_job(job_id: str) -> None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["state"] != "queued":
        return

    caps = json.loads(row["caps"] or "{}")
    route = MODELS[row["model"]]
    env = {
        "MODEL_BASE_URL": route["base_url"],
        "MODEL_NAME": route["model"],
        "MODEL_API_KEY": os.environ.get(route["key_env"], "") if route["key_env"] else "",
        "TOOLSHED_URL": TOOLSHED_URL,
        "JOBS_URL": JOBS_URL_FOR_RUNNER,
        "JOB_ID": job_id,
        "JOB_PROMPT": row["prompt"],
        "REPO_URL": row["repo"] or "",
        "MAX_TURNS": str(caps.get("max_turns", 30)),
        "MAX_TOKENS_TOTAL": str(caps.get("max_tokens", 500_000)),
    }

    _update(job_id, state="running", started=_now())
    container = None
    try:
        container = docker_client.containers.run(
            IMAGES[row["image"]],
            detach=True,
            name=f"job-{job_id}",
            environment=env,
            mem_limit=MEM_LIMITS[row["image"]],
            nano_cpus=2_000_000_000,
            labels={"managed-by": "jobs", "job-id": job_id},
        )
        try:
            result = container.wait(timeout=WALL_CLOCK_SECS)
            exit_code = result.get("StatusCode", -1)
        except Exception:  # wall clock exceeded (requests timeout) or dockerd hiccup
            container.kill()
            exit_code = -9
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        state = {0: "done", 2: "capped"}.get(exit_code, "error")
        _update(
            job_id,
            state=state,
            exit_code=exit_code,
            logs=logs[-MAX_LOG_CHARS:],
            error="wall clock exceeded" if exit_code == -9 else None,
            ended=_now(),
        )
    except Exception as e:
        _update(job_id, state="error", error=str(e)[:2000], ended=_now())
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except docker.errors.APIError:
                pass


def worker() -> None:
    while True:
        run_job(job_queue.get())
        job_queue.task_done()


# --- API -------------------------------------------------------------------

app = FastAPI(title="jobs")


class JobIn(BaseModel):
    prompt: str
    model: str = "deepseek"
    image: str = "lite"
    repo: str | None = None
    caps: dict = {}


class JobPatch(BaseModel):
    turns: int | None = None
    tokens: int | None = None
    artifact_urls: list[str] | None = None
    state: str | None = None  # accepted but container exit code is authoritative


@app.post("/jobs")
def submit(j: JobIn):
    if j.model not in MODELS:
        raise HTTPException(400, f"unknown model {j.model!r}; have {sorted(MODELS)}")
    if j.image not in IMAGES:
        raise HTTPException(400, f"unknown image {j.image!r}; have {sorted(IMAGES)}")
    job_id = uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, prompt, model, image, repo, state, caps) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (job_id, j.prompt, j.model, j.image, j.repo, json.dumps(j.caps or {})),
        )
    job_queue.put(job_id)
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/jobs")
def list_jobs(limit: int = 50):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (min(limit, 200),)
        ).fetchall()
    return [_row_dict(r) for r in rows]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such job")
    return _row_dict(row)


@app.get("/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_logs(job_id: str):
    with db() as conn:
        row = conn.execute("SELECT state, logs FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such job")
    if row["state"] in ("queued", "running"):
        try:
            return docker_client.containers.get(f"job-{job_id}").logs().decode(
                "utf-8", errors="replace"
            )
        except docker.errors.NotFound:
            return "(no logs yet)"
    return row["logs"] or "(no logs captured)"


@app.patch("/jobs/{job_id}")
def patch_job(job_id: str, p: JobPatch):
    with db() as conn:
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such job")
    cols = {}
    if p.turns is not None:
        cols["turns"] = p.turns
    if p.tokens is not None:
        cols["tokens"] = p.tokens
    if p.artifact_urls is not None:
        cols["artifact_urls"] = json.dumps(p.artifact_urls)
    if cols:
        _update(job_id, **cols)
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True, "queued": job_queue.qsize()}


# --- startup ---------------------------------------------------------------

_init_db()
with db() as _conn:
    # Orphans from a restart: running jobs are dead (their waiter thread is
    # gone); queued jobs get re-enqueued.
    _conn.execute("UPDATE jobs SET state='error', error='orphaned by restart' "
                  "WHERE state='running'")
    for _r in _conn.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created"):
        job_queue.put(_r["id"])

for _ in range(MAX_CONCURRENT):
    threading.Thread(target=worker, daemon=True).start()
