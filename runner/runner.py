"""runner — the agent loop that executes one job inside a throwaway container.

Plain OpenAI-compatible chat-completions function-calling loop over httpx.
No framework. The container is the sandbox: this process gets unrestricted
bash and file access inside /workspace with no permission prompts. The only
doors out are the model API and toolshed.

Config via env:
  MODEL_BASE_URL    e.g. https://api.deepseek.com/v1
  MODEL_NAME        e.g. deepseek-v4-pro
  MODEL_API_KEY
  TOOLSHED_URL      e.g. https://web.homelab.example (search/fetch/artifacts)
  JOB_PROMPT        the task
  JOB_ID            optional; included in artifact titles + progress reports
  JOBS_URL          optional; PATCH {JOBS_URL}/jobs/{JOB_ID} progress each turn
  REPO_URL          optional; shallow-cloned into the workspace before the loop
  WORKSPACE         default /workspace
  MAX_TURNS         default 30
  MAX_TOKENS_TOTAL  default 500000

Exit codes: 0 done, 1 unrecoverable error, 2 cap hit. In every case the
runner stores a final artifact — a job never evaporates without a report.
"""

import json
import os
import subprocess
import sys
import time

import httpx

MODEL_BASE_URL = os.environ["MODEL_BASE_URL"].rstrip("/")
MODEL_NAME = os.environ["MODEL_NAME"]
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "")
TOOLSHED_URL = os.environ["TOOLSHED_URL"].rstrip("/")
JOB_PROMPT = os.environ["JOB_PROMPT"]
JOB_ID = os.environ.get("JOB_ID", "")
JOBS_URL = os.environ.get("JOBS_URL", "").rstrip("/")
REPO_URL = os.environ.get("REPO_URL", "")
WORKSPACE = os.path.realpath(os.environ.get("WORKSPACE", "/workspace"))
MAX_TURNS = int(os.environ.get("MAX_TURNS", "30"))
MAX_TOKENS_TOTAL = int(os.environ.get("MAX_TOKENS_TOTAL", "500000"))

BASH_TIMEOUT = 120
MAX_TOOL_OUTPUT = 30000  # chars of tool result fed back to the model
MAX_FILE_READ = 100000

SYSTEM_PROMPT = f"""You are an autonomous agent running unattended in a \
disposable Linux container, executing one job. Your workspace is {WORKSPACE}; \
you have unrestricted bash and file access there — write files, run tests, \
install packages, whatever the job needs. Nothing you do requires permission.

Work the task step by step. Verify your work (run the tests, check the \
output) before finishing. Use web_search/web_fetch when you need current \
information. Use store_artifact for any deliverable worth keeping — stored \
artifacts survive; everything else in the container is destroyed when you \
finish.

You MUST end by calling done(report_markdown) with a concise report of what \
you did, what worked, what didn't, and links to any artifacts you stored. \
The report is the job's permanent record."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": f"Run a shell command in {WORKSPACE}. Returns stdout+stderr. "
            f"{BASH_TIMEOUT}s timeout per command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace. Path is relative to the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the workspace (parent dirs created). "
            "Path is relative to the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Returns titles, URLs, snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and get its main content as markdown (HTML or PDF).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "store_artifact",
            "description": "Store a document (report, code, data) in the artifact store. "
            "Returns a stable URL. Artifacts outlive this container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "title": {"type": "string"},
                    "content_type": {
                        "type": "string",
                        "enum": [
                            "text/markdown",
                            "text/plain",
                            "text/html",
                            "application/json",
                            "text/csv",
                            "image/svg+xml",
                        ],
                    },
                },
                "required": ["content", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish the job. The report is stored as the job's final artifact.",
            "parameters": {
                "type": "object",
                "properties": {"report_markdown": {"type": "string"}},
                "required": ["report_markdown"],
            },
        },
    },
]


def _truncate(s: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [truncated, {len(s)} chars total]"


def _jail(path: str) -> str:
    full = os.path.realpath(os.path.join(WORKSPACE, path))
    if full != WORKSPACE and not full.startswith(WORKSPACE + os.sep):
        raise ValueError(f"path escapes workspace: {path}")
    return full


def tool_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout after {BASH_TIMEOUT}s]"
    out = r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    return _truncate(out.strip() or f"[exit {r.returncode}, no output]") + (
        f"\n[exit {r.returncode}]" if r.returncode != 0 else ""
    )


def tool_read_file(path: str) -> str:
    with open(_jail(path), "r", errors="replace") as f:
        return _truncate(f.read(), MAX_FILE_READ)


def tool_write_file(path: str, content: str) -> str:
    full = _jail(path)
    os.makedirs(os.path.dirname(full) or WORKSPACE, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"


def tool_web_search(query: str) -> str:
    r = http.get(f"{TOOLSHED_URL}/search", params={"q": query, "format": "json"}, timeout=30)
    r.raise_for_status()
    results = r.json().get("results", [])[:8]
    if not results:
        return f"No results for: {query}"
    return "\n\n".join(
        f"{i + 1}. {x.get('title')}\n   {x.get('url')}\n   "
        + " ".join((x.get("content") or "").split())
        for i, x in enumerate(results)
    )


def tool_web_fetch(url: str) -> str:
    r = http.get(f"{TOOLSHED_URL}/fetch", params={"url": url}, timeout=60)
    if r.status_code != 200:
        return f"web_fetch failed: HTTP {r.status_code}: {r.text[:500]}"
    d = r.json()
    header = " / ".join(s for s in (d.get("title"), d.get("sitename"), d.get("date")) if s)
    return _truncate(f"{header}\n\n{d.get('content', '')}")


def tool_store_artifact(content: str, title: str, content_type: str = "text/markdown") -> str:
    r = http.post(
        f"{TOOLSHED_URL}/artifacts",
        json={"content": content, "title": title, "content_type": content_type},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    artifact_urls.append(d["url"])
    return f"stored: {d['url']}"


TOOL_IMPLS = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "web_search": tool_web_search,
    "web_fetch": tool_web_fetch,
    "store_artifact": tool_store_artifact,
}

http = httpx.Client(follow_redirects=True)
artifact_urls: list[str] = []
tokens_used = 0
turns_used = 0


def report_progress(state: str | None = None) -> None:
    """Best-effort progress PATCH back to the jobs service."""
    if not (JOBS_URL and JOB_ID):
        return
    body = {"turns": turns_used, "tokens": tokens_used, "artifact_urls": artifact_urls}
    if state:
        body["state"] = state
    try:
        http.patch(f"{JOBS_URL}/jobs/{JOB_ID}", json=body, timeout=10)
    except httpx.HTTPError:
        pass


def store_final(title: str, markdown: str) -> None:
    """Store the job's final report. This must not fail silently — but it
    also must not crash the exit path, so fall back to stdout."""
    try:
        tool_store_artifact(markdown, title, "text/markdown")
    except Exception as e:
        print(f"[runner] FAILED to store final artifact: {e}\n{markdown}", file=sys.stderr)


def finish(exit_code: int, title: str, markdown: str) -> None:
    store_final(title, markdown)
    report_progress({0: "done", 2: "capped"}.get(exit_code, "error"))
    sys.exit(exit_code)


def chat(messages: list) -> dict:
    """One completions call with retry on transient failures."""
    global tokens_used
    last_err = None
    for attempt in range(4):
        if attempt:
            time.sleep(2**attempt)
        try:
            r = http.post(
                f"{MODEL_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {MODEL_API_KEY}"},
                json={
                    "model": MODEL_NAME,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                },
                timeout=300,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            r.raise_for_status()
            data = r.json()
            tokens_used += (data.get("usage") or {}).get("total_tokens", 0)
            return data["choices"][0]["message"]
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
            last_err = str(e)
    raise RuntimeError(f"model API unrecoverable after retries: {last_err}")


def job_title(suffix: str) -> str:
    prompt_stub = " ".join(JOB_PROMPT.split())[:60]
    return f"[{suffix}] job {JOB_ID or 'local'}: {prompt_stub}"


def main() -> None:
    global turns_used
    os.makedirs(WORKSPACE, exist_ok=True)

    if REPO_URL:
        clone = tool_bash(f"git clone --depth 1 {REPO_URL!r} repo")
        print(f"[runner] clone: {clone}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": JOB_PROMPT},
    ]

    while True:
        if turns_used >= MAX_TURNS:
            finish(2, job_title("CAPPED"), _cap_report(f"turn cap ({MAX_TURNS}) reached"))
        if tokens_used >= MAX_TOKENS_TOTAL:
            finish(2, job_title("CAPPED"), _cap_report(f"token cap ({MAX_TOKENS_TOTAL}) reached"))

        try:
            msg = chat(messages)
        except RuntimeError as e:
            finish(1, job_title("ERROR"), _cap_report(str(e)))

        turns_used += 1
        report_progress()
        messages.append(msg)

        calls = msg.get("tool_calls") or []
        if not calls:
            # Model replied with prose instead of a tool call; push it to finish properly.
            messages.append(
                {
                    "role": "user",
                    "content": "Continue with tool calls. When the job is complete, "
                    "call done(report_markdown).",
                }
            )
            continue

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError as e:
                args, result = {}, f"[bad tool arguments: {e}]"
                name = "_invalid"

            if name == "done":
                report = args.get("report_markdown", "(empty report)")
                finish(0, job_title("DONE"), report)
            elif name in TOOL_IMPLS:
                print(f"[runner] turn {turns_used}: {name}({str(args)[:200]})")
                try:
                    result = TOOL_IMPLS[name](**args)
                except Exception as e:
                    result = f"[{name} failed: {e}]"
            elif name != "_invalid":
                result = f"[unknown tool: {name}]"

            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )


def _cap_report(reason: str) -> str:
    return (
        f"# Job did not complete: {reason}\n\n"
        f"**Prompt:** {JOB_PROMPT}\n\n"
        f"**Turns used:** {turns_used} · **Tokens used:** {tokens_used}\n\n"
        f"**Artifacts stored before stopping:**\n"
        + ("\n".join(f"- {u}" for u in artifact_urls) or "- none")
    )


if __name__ == "__main__":
    main()
