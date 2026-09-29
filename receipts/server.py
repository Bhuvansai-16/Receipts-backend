"""Receipts API: sign-in (proxied to Neon Auth), checks and receipts. Runs live in Neon Postgres.

python -m receipts serve   ->   http://127.0.0.1:8000   (the UI is the separate receipts-frontend repo)
"""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import auth, config, db, engine, github, swebench

MAX_DIFF_BYTES = 200_000
MAX_CONCURRENT_CHECKS = 2  # whole server: Daytona quota + Token Factory credit


class RunRequest(BaseModel):
    instance_id: str
    pr: Literal["gold", "none", "diff"]
    diff: str | None = None


@dataclass
class LiveRun:
    """Progress of a run while it is active; once finished, the stored evidence takes over."""

    id: str
    events: list[dict] = field(default_factory=list)
    listeners: set = field(default_factory=set)

    def publish(self, type_: str, data: dict) -> None:
        event = {"type": type_, "data": data}
        self.events.append(event)
        for queue in self.listeners:
            queue.put_nowait(event)


LIVE: dict[str, LiveRun] = {}
_tasks: set[asyncio.Task] = set()
_slots: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loading SWE-bench takes ~15 s (it also checks the HF hub); do it now so the issue picker is instant.
    warmup = asyncio.get_running_loop().run_in_executor(None, _instances)
    pool = await db.open_pool(config.DATABASE_URL) if config.DATABASE_URL else None
    app.state.runs = db.PgRuns(pool) if pool else db.MemoryRuns()
    if pool is None:  # ponytail: no DATABASE_URL = runs in memory, seeded from runs/*.json; new ones don't persist
        await db.import_runs(app.state.runs, config.RUNS_DIR)
    await app.state.runs.fail_unfinished()  # their tasks died with the previous process
    yield
    warmup.cancel()
    await auth.close()
    if pool:
        await pool.close()
    if config.SANDBOX_PROVIDER == "daytona":
        from .daytona_backend import client

        await client().close()


app = FastAPI(title="Receipts API", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(github.router)
app.add_middleware(GZipMiddleware, minimum_size=1000)  # leaves text/event-stream alone
# Neon's auth client adds x-neon-client-info to every request, so preflights must allow it.
app.add_middleware(CORSMiddleware, allow_origins=[config.FRONTEND_URL], allow_credentials=True,
                   allow_methods=["GET", "POST"], allow_headers=["content-type", "authorization", "x-neon-client-info"])


def runs_store(request: Request):
    return request.app.state.runs


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _dumps(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":"), default=_json_default).encode()


def cached_json(request: Request, payload, cache_control: str) -> Response:
    """JSON with an ETag; a matching If-None-Match gets an empty 304."""
    body = _dumps(payload)
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": cache_control}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


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


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/instances")
def list_instances(request: Request) -> Response:
    return cached_json(request, _instances(), "public, max-age=3600")


@app.get("/api/instances/{instance_id}")
def get_instance(instance_id: str, request: Request) -> Response:
    inst = _load(instance_id)
    return cached_json(request, {"id": inst.instance_id, "repo": inst.repo,
                                 "problem_statement": inst.problem_statement}, "public, max-age=3600")


@app.get("/api/me")
async def me(user: dict = Depends(auth.current_user)) -> dict:
    return {key: user.get(key) for key in ("id", "email", "name", "image")}


@app.post("/api/runs", status_code=202)
async def start_run(req: RunRequest, user: dict = Depends(auth.current_user), runs=Depends(runs_store)) -> dict:
    inst = _load(req.instance_id)
    if req.pr == "diff":
        if not (req.diff or "").strip():
            raise HTTPException(422, "Paste a unified diff, or pick the real fix or a do-nothing PR.")
        if len(req.diff.encode()) > MAX_DIFF_BYTES:
            raise HTTPException(413, f"Diff is larger than {MAX_DIFF_BYTES // 1000} KB.")
    # ponytail: check-then-insert; two simultaneous clicks can both pass. Fine for a cost guard.
    active, recent = await runs.usage(user["id"], datetime.now(timezone.utc) - timedelta(days=1))
    if active >= config.MAX_ACTIVE_RUNS:
        raise HTTPException(429, f"You already have {active} checks running. Start another when one finishes.")
    if recent >= config.RUNS_PER_DAY:
        raise HTTPException(429, f"Daily limit reached ({config.RUNS_PER_DAY} checks in 24 hours). Try again later.")
    patch = {"gold": inst.gold_patch, "none": None, "diff": req.diff}[req.pr]
    run_id = engine.new_run_id(inst.instance_id, req.pr, taken=LIVE)
    await runs.create(run_id, user["id"], inst.instance_id, req.pr)
    live = LIVE[run_id] = LiveRun(run_id)
    task = asyncio.create_task(_execute(live, inst, patch, runs))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"run_id": run_id}


async def _execute(live: LiveRun, inst: swebench.Instance, patch: str | None, runs) -> None:
    global _slots
    _slots = _slots or asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    status, evidence = "error", {"instance_id": inst.instance_id, "reason": "the check failed to run"}
    try:
        async with _slots:
            await runs.mark_running(live.id)
            live.publish("status", {"status": "running"})
            # "done" is sent below, after the evidence is stored, so clients never read a half-written run
            evidence = await engine.check(inst, patch, emit=lambda t, d: t != "done" and live.publish(t, d))
            status = "done"
    except Exception as e:  # engine.check never raises by design; the sandbox or database can
        live.publish("error", {"message": f"{type(e).__name__}: {e}"})
    finally:
        try:  # store what the viewers saw, so a replay equals the live stream
            await runs.finish(live.id, status, {**evidence, "run_id": live.id, "events": live.events})
        finally:
            live.publish("done", {})
            LIVE.pop(live.id, None)


@app.get("/api/runs")
async def my_runs(request: Request, cursor: str | None = None, limit: int = Query(20, ge=1, le=50),
                  user: dict = Depends(auth.current_user), runs=Depends(runs_store)) -> Response:
    try:
        after = db.decode_cursor(cursor) if cursor else None
    except ValueError:
        raise HTTPException(400, "Invalid cursor.")
    rows = await runs.list_for_user(user["id"], limit + 1, after)
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = db.encode_cursor(rows[-1]["started_at"], rows[-1]["id"]) if more else None
    return cached_json(request, {"runs": rows, "next_cursor": next_cursor}, "private, no-cache")


async def _stored(run_id: str, runs) -> dict:
    row = await runs.get(run_id) if engine.safe_run_id(run_id) else None
    if row is None:
        raise HTTPException(404, "No such run.")
    return row


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, runs=Depends(runs_store)) -> Response:
    row = await _stored(run_id, runs)
    evidence = row["evidence"]
    if evidence is None:  # queued or running (or killed by a restart): what has happened so far
        live = LIVE.get(run_id)
        events = list(live.events) if live else []
        if row["status"] == "error" and row["reason"]:
            events.append({"type": "error", "data": {"message": row["reason"]}})
        evidence = {"instance_id": row["instance_id"], "started_at": row["started_at"], "events": events}
    finished = row["status"] in ("done", "error")
    return Response(_dumps({"status": row["status"], "evidence": {**evidence, "pr": evidence.get("pr") or row["pr"]}}),
                    media_type="application/json",
                    headers={"Cache-Control": "public, max-age=31536000, immutable" if finished else "no-store"})


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, runs=Depends(runs_store)):
    live = LIVE.get(run_id)
    if live is None:  # finished: replay what was stored
        row = await _stored(run_id, runs)
        stored = [e for e in (row["evidence"] or {}).get("events", []) if e["type"] != "done"]

        async def replay():
            for event in stored + [{"type": "done", "data": {}}]:
                yield {"event": event["type"], "data": json.dumps(event["data"])}

        return EventSourceResponse(replay())

    async def stream():
        queue: asyncio.Queue = asyncio.Queue()
        backlog = list(live.events)
        live.listeners.add(queue)
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
            live.listeners.discard(queue)

    return EventSourceResponse(stream())
