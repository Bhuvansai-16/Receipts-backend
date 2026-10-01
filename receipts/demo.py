"""The no-sign-in demo: hand-picked checks anyone can start or watch, under hard caps.

Demo runs belong to the reserved user id "demo" (Neon user ids are UUIDs), so the run store's per-user queries
give the daily count and the gallery with no schema change. One demo check runs at a time: a visitor who
presses Run while one is live joins it. Every demo check is a real check: same pipeline, same verdict rules.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import checks, config, engine, swebench
from .checks import LIVE

router = APIRouter(prefix="/api/demo")
DEMO_USER = "demo"
HERE = Path(__file__).parent
CASES_FILE = HERE / "demo_cases.json"
PATCHES = HERE / "demo_patches"
GALLERY_SIZE = 12
PUBLIC_KEYS = ("id", "title", "repo", "instance_id", "kind", "summary")
USED_UP = ("Today's demo checks are used up. The finished receipts below show real runs, or sign in to check "
           "your own pull requests.")
# ponytail: one process (Cloud Run max 1 instance), so an in-memory lock and live id are enough
_lock = asyncio.Lock()
_live: dict[str, str] = {}  # {"id": run_id} of the demo check started last


class Start(BaseModel):
    case: str


@lru_cache(maxsize=1)
def cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def _runs(request: Request):
    return request.app.state.runs


def live_run() -> str | None:
    run_id = _live.get("id")
    return run_id if run_id in LIVE else None


def _patch(case: dict) -> tuple[str, str | None]:
    """(pr kind, patch text): the real fix and the empty patch come from SWE-bench, a wrong patch from a file."""
    if case["patch"] in ("gold", "none"):
        return case["patch"], None
    return "diff", (PATCHES / case["patch"]).read_text(encoding="utf-8")


def _since() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


@router.get("")
async def overview(runs=Depends(_runs)) -> dict:
    _, today = await runs.usage(DEMO_USER, _since())
    recent = await runs.list_for_user(DEMO_USER, 50)
    return {"cases": [{k: c[k] for k in PUBLIC_KEYS} for c in cases()], "live": live_run(),
            "gallery": [r for r in recent if r["status"] == "done"][:GALLERY_SIZE],
            "left_today": max(0, config.DEMO_RUNS_PER_DAY - today)}


@router.post("/runs", status_code=202)
async def start(body: Start, runs=Depends(_runs)) -> dict:
    case = next((c for c in cases() if c["id"] == body.case), None)
    if case is None:
        raise HTTPException(404, "No such demo check.")
    async with _lock:  # two visitors pressing Run at once start one check
        if run_id := live_run():
            return {"run_id": run_id, "joined": True}
        since = _since()
        if ((await runs.usage(DEMO_USER, since))[1] >= config.DEMO_RUNS_PER_DAY
                or await runs.global_recent(since) >= config.GLOBAL_RUNS_PER_DAY):
            raise HTTPException(429, USED_UP)
        kind, patch = _patch(case)
        inst = swebench.load_instance(case["instance_id"])

        async def prepare():
            return inst, inst.gold_patch if kind == "gold" else patch

        run_id = engine.new_run_id(inst.instance_id, kind, taken=LIVE)
        await checks.launch(runs, DEMO_USER, run_id, inst.instance_id, kind, prepare)
        _live["id"] = run_id
    return {"run_id": run_id, "joined": False}
