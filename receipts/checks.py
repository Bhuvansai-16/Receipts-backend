"""Running checks in the background: live progress, per-user limits, storing the result.

Both the SWE-bench demo (server.py) and GitHub pull requests (github.py) start checks through `launch`.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from . import config, engine

MAX_CONCURRENT_CHECKS = 2  # whole server: sandbox quota + Token Factory credit
STOPPED = "Stopped before it finished."
log = logging.getLogger("uvicorn.error")


@dataclass
class LiveRun:
    """Progress of a run while it is active; once finished, the stored evidence takes over."""

    id: str
    user_id: str | None = None
    source: dict | None = None  # pull request details, shown while the run is live
    task: asyncio.Task | None = None
    events: list[dict] = field(default_factory=list)
    listeners: set = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)

    def publish(self, type_: str, data: dict) -> None:
        event = {"type": type_, "t": round(time.monotonic() - self.started, 1), "data": data}
        self.events.append(event)
        for queue in self.listeners:
            queue.put_nowait(event)


LIVE: dict[str, LiveRun] = {}
_tasks: set[asyncio.Task] = set()
_slots: asyncio.Semaphore | None = None


async def usage(runs, user_id) -> tuple[int, int]:
    return await runs.usage(user_id, datetime.now(timezone.utc) - timedelta(days=1))


async def enforce_limits(runs, user_id) -> None:
    # ponytail: check-then-insert; two simultaneous starts can both pass. Fine for a cost guard.
    since = datetime.now(timezone.utc) - timedelta(days=1)
    active, recent = await runs.usage(user_id, since)
    if active >= config.MAX_ACTIVE_RUNS:
        raise HTTPException(429, f"You already have {active} checks running. Start another when one finishes.")
    if recent >= config.RUNS_PER_DAY:
        raise HTTPException(429, f"Daily limit reached ({config.RUNS_PER_DAY} checks in 24 hours). Try again later.")
    if await runs.global_recent(since) >= config.GLOBAL_RUNS_PER_DAY:
        raise HTTPException(429, "Receipts has reached today's check limit for everyone. Try again tomorrow.")


async def launch(runs, user_id, run_id, instance_id, pr_kind, prepare, *, source=None, on_start=None,
                 on_finish=None) -> str:
    """Store the run as queued and start it. `prepare()` returns (target, patch) inside the task, so slow
    downloads and environment setup show up as progress instead of blocking the request."""
    await runs.create(run_id, user_id, instance_id, pr_kind, source=source and {
        k: source[k] for k in ("repo", "pr_number", "head_sha")})
    live = LIVE[run_id] = LiveRun(run_id, user_id, source)
    task = live.task = asyncio.create_task(_execute(live, instance_id, prepare, runs, source, on_start, on_finish))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run_id


def cancel(run_id: str, user_id: str) -> bool:
    """Stop a live run started by `user_id`. Sandbox work in flight is abandoned; the run ends as an error."""
    live = LIVE.get(run_id)
    if live is None or live.user_id != user_id or live.task is None or live.task.done():
        return False
    live.task.cancel()
    return True


async def _execute(live, instance_id, prepare, runs, source, on_start, on_finish) -> None:
    global _slots
    _slots = _slots or asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    status, evidence = "error", {"instance_id": instance_id, "reason": "the check failed to run"}
    try:
        async with _slots:
            await runs.mark_running(live.id)
            live.publish("status", {"status": "running"})
            if on_start:
                await on_start(live.id)
            target, patch = await prepare()
            # "done" is sent below, after the evidence is stored, so clients never read a half-written run
            evidence = await engine.check(target, patch, emit=lambda t, d: t != "done" and live.publish(t, d))
            status = "done"
    except asyncio.CancelledError:  # the owner pressed Stop (see cancel)
        live.publish("error", {"message": STOPPED})
        evidence["reason"] = STOPPED
    except Exception as e:  # engine.check never raises by design; downloads, the sandbox or the database can
        live.publish("error", {"message": f"{type(e).__name__}: {e}"})
        evidence["reason"] = f"{type(e).__name__}: {e}"
    finally:
        evidence = {**evidence, "run_id": live.id, "events": live.events, **({"source": source} if source else {})}
        try:  # store what the viewers saw, so a replay equals the live stream
            await runs.finish(live.id, status, evidence)
        finally:
            if on_finish:
                try:
                    await on_finish(status, evidence)
                except Exception as e:  # reporting back to GitHub must not hide the stored result
                    log.warning("after-run hook failed for %s: %s", live.id, e)
            live.publish("done", {})
            LIVE.pop(live.id, None)
