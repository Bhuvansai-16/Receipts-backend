"""Web API + SPA host: start checks, stream their progress (SSE), serve saved evidence.

python -m receipts serve   ->   http://127.0.0.1:8000
"""
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import config, engine, swebench

MAX_DIFF_BYTES = 200_000
MAX_CONCURRENT_CHECKS = 2  # Daytona quota + Token Factory credit
WEB_DIST = config.ROOT / "web" / "dist"


class RunRequest(BaseModel):
    instance_id: str
    pr: Literal["gold", "none", "diff"]
    diff: str | None = None


@dataclass
class Run:
    id: str
    instance_id: str
    pr: str
    started_at: str
    status: str = "queued"  # queued | running | done | error
    events: list[dict] = field(default_factory=list)
    evidence: dict | None = None
    listeners: set = field(default_factory=set)

    def publish(self, type_: str, data: dict) -> None:
        event = {"type": type_, "data": data}
        self.events.append(event)
        for queue in self.listeners:
            queue.put_nowait(event)


RUNS: dict[str, Run] = {}
_tasks: set[asyncio.Task] = set()
_slots: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    if config.SANDBOX_PROVIDER == "daytona":
        from .daytona_backend import client

        await client().close()


app = FastAPI(title="Receipts", lifespan=lifespan)


@lru_cache(maxsize=1)
def _instances() -> list[dict]:
    rows = [r for r in swebench._dataset().values() if r["repo"] in swebench.PYTEST_REPOS]
    title = lambda text: next((line.strip() for line in text.splitlines() if line.strip()), "")[:140]
    return sorted(({"id": r["instance_id"], "repo": r["repo"], "difficulty": r["difficulty"],
                    "title": title(r["problem_statement"])} for r in rows), key=lambda i: (i["repo"], i["id"]))


def _load(instance_id: str) -> swebench.Instance:
    try:
        return swebench.load_instance(instance_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/instances")
def list_instances() -> list[dict]:
    return _instances()


@app.get("/api/instances/{instance_id}")
def get_instance(instance_id: str) -> dict:
    inst = _load(instance_id)
    return {"id": inst.instance_id, "repo": inst.repo, "problem_statement": inst.problem_statement}


@app.post("/api/runs", status_code=202)
async def start_run(req: RunRequest) -> dict:
    inst = _load(req.instance_id)
    if req.pr == "diff":
        if not (req.diff or "").strip():
            raise HTTPException(422, "Paste a unified diff, or pick the real fix or a do-nothing PR.")
        if len(req.diff.encode()) > MAX_DIFF_BYTES:
            raise HTTPException(413, f"Diff is larger than {MAX_DIFF_BYTES // 1000} KB.")
    patch = {"gold": inst.gold_patch, "none": None, "diff": req.diff}[req.pr]
    run_id = engine.new_run_id(inst.instance_id, req.pr, taken=RUNS)
    run = RUNS[run_id] = Run(run_id, inst.instance_id, req.pr, engine.now_iso())
    task = asyncio.create_task(_execute(run, inst, patch))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"run_id": run_id}


async def _execute(run: Run, inst: swebench.Instance, patch: str | None) -> None:
    global _slots
    _slots = _slots or asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    async with _slots:
        run.status = "running"
        run.publish("status", {"status": "running"})
        try:
            # "done" is sent below, after the evidence is saved, so clients never fetch a half-written run
            ev = await engine.check(inst, patch, emit=lambda t, d: t != "done" and run.publish(t, d))
            ev.update(run_id=run.id, pr=run.pr)
            engine.save_evidence(ev, run.id)
            run.evidence, run.status = ev, "done"
        except Exception as e:  # engine.check never raises by design; this is a last resort
            run.status = "error"
            run.publish("error", {"message": f"{type(e).__name__}: {e}"})
    run.publish("done", {})


def _saved(run_id: str) -> dict | None:
    if not engine.safe_run_id(run_id):
        return None
    path = config.RUNS_DIR / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _pr_label(run_id: str, ev: dict) -> str:
    # <instance>-<pr>-<YYYYmmdd-HHMMSS>
    return ev.get("pr") or run_id[len(ev.get("instance_id", "")) + 1:-16] or "?"


@app.get("/api/runs")
def list_runs() -> list[dict]:
    rows = {}
    for path in config.RUNS_DIR.glob("*.json") if config.RUNS_DIR.exists() else []:
        try:
            ev = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rows[path.stem] = {"run_id": path.stem, "instance_id": ev.get("instance_id"), "pr": _pr_label(path.stem, ev),
                           "status": "done", "verdict": ev.get("verdict"), "started_at": ev.get("started_at", "")}
    for run in RUNS.values():
        rows[run.id] = {"run_id": run.id, "instance_id": run.instance_id, "pr": run.pr, "status": run.status,
                        "verdict": (run.evidence or {}).get("verdict"), "started_at": run.started_at}
    return sorted(rows.values(), key=lambda r: r["started_at"], reverse=True)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    if run := RUNS.get(run_id):
        partial = {"instance_id": run.instance_id, "pr": run.pr, "started_at": run.started_at, "events": run.events}
        return {"status": run.status, "evidence": run.evidence or partial}
    if (ev := _saved(run_id)) is None:
        raise HTTPException(404, "No such run.")
    ev.setdefault("pr", _pr_label(run_id, ev))
    return {"status": "done", "evidence": ev}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    run = RUNS.get(run_id)
    if run is None and (ev := _saved(run_id)) is None:
        raise HTTPException(404, "No such run.")

    async def stream():
        if run is None:  # finished before this server started: replay what was stored
            for event in [e for e in ev.get("events", []) if e["type"] != "done"] + [{"type": "done", "data": {}}]:
                yield {"event": event["type"], "data": json.dumps(event["data"])}
            return
        queue: asyncio.Queue = asyncio.Queue()
        backlog = list(run.events)
        run.listeners.add(queue)
        try:
            for event in backlog:
                yield {"event": event["type"], "data": json.dumps(event["data"])}
            if backlog and backlog[-1]["type"] == "done":
                return
            while True:
                event = await queue.get()
                yield {"event": event["type"], "data": json.dumps(event["data"])}
                if event["type"] == "done":
                    return
        finally:
            run.listeners.discard(queue)

    return EventSourceResponse(stream())


if WEB_DIST.is_dir():  # built SPA (npm run build); in development Vite serves it and proxies /api
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        file = WEB_DIST / path
        return FileResponse(file if path and file.is_file() else WEB_DIST / "index.html")
