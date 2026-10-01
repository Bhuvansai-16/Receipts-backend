import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from receipts import config, demo, server, swebench

ROWS = {"psf__requests-1": {"instance_id": "psf__requests-1", "repo": "psf/requests", "difficulty": "<15 min fix",
                            "problem_statement": "GET sends Content-Length", "patch": "GOLD", "PASS_TO_PASS": "[]"}}
CASE = {"instance_id": "psf__requests-1", "repo": "psf/requests", "title": "GET sends Content-Length",
        "summary": "s"}
CASES = [{**CASE, "id": "req-fix", "patch": "gold", "kind": "real fix"},
         {**CASE, "id": "req-empty", "patch": "none", "kind": "empty patch"},
         {**CASE, "id": "req-wrong", "patch": "wrong.diff", "kind": "wrong patch"}]


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(swebench, "_dataset", lambda: ROWS)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(demo, "cases", lambda: CASES)
    (tmp_path / "wrong.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    monkeypatch.setattr(demo, "PATCHES", tmp_path)
    monkeypatch.setattr(demo, "_lock", asyncio.Lock())
    demo._live.clear()
    server.LIVE.clear()
    gate = threading.Event()
    gate.set()
    seen = {}

    async def fake_check(inst, patch, emit=None, **kw):
        seen["patch"] = patch
        while not gate.is_set():  # a test holds the check open to see a second visitor join it
            await asyncio.sleep(0.01)
        emit("verdict", {"verdict": "PROVEN", "reason": "r", "seconds": 1.0, "tokens": 10})
        return {"instance_id": inst.instance_id, "verdict": "PROVEN", "reason": "r", "seconds": 1.0,
                "tokens": {}, "started_at": "2026-10-01T00:00:00+00:00", "events": []}

    monkeypatch.setattr(server.engine, "check", fake_check)
    with TestClient(server.app) as c:
        c.gate, c.seen, c.store = gate, seen, server.app.state.runs
        yield c
        gate.set()


def finished(api, run_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = api.get(f"/api/runs/{run_id}").json()["status"]
        if status in ("done", "error"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"{run_id} still running")


def test_the_demo_lists_its_cases_without_sign_in_or_patch_text(api):
    r = api.get("/api/demo")
    assert r.status_code == 200
    body = r.json()
    assert [c["id"] for c in body["cases"]] == ["req-fix", "req-empty", "req-wrong"]
    assert all("patch" not in c for c in body["cases"])
    assert body["live"] is None and body["gallery"] == [] and body["left_today"] == config.DEMO_RUNS_PER_DAY


def test_anyone_can_start_a_demo_check(api):
    r = api.post("/api/demo/runs", json={"case": "req-fix"})
    assert r.status_code == 202 and r.json()["joined"] is False
    run_id = r.json()["run_id"]
    assert finished(api, run_id) == "done" and api.seen["patch"] == "GOLD"
    assert asyncio.run(api.store.get(run_id))["user_id"] == demo.DEMO_USER


def test_a_wrong_patch_case_sends_its_diff(api):
    run_id = api.post("/api/demo/runs", json={"case": "req-wrong"}).json()["run_id"]
    finished(api, run_id)
    assert api.seen["patch"] == "diff --git a/x b/x\n"


def test_a_second_visitor_joins_the_live_demo_check(api):
    api.gate.clear()
    first = api.post("/api/demo/runs", json={"case": "req-fix"}).json()
    second = api.post("/api/demo/runs", json={"case": "req-empty"}).json()
    assert second == {"run_id": first["run_id"], "joined": True}
    assert api.get("/api/demo").json()["live"] == first["run_id"]
    api.gate.set()
    finished(api, first["run_id"])
    assert api.get("/api/demo").json()["live"] is None


def test_an_unknown_case_is_404(api):
    assert api.post("/api/demo/runs", json={"case": "nope"}).status_code == 404


def test_the_daily_demo_cap_is_429_in_plain_words(api, monkeypatch):
    monkeypatch.setattr(config, "DEMO_RUNS_PER_DAY", 1)
    finished(api, api.post("/api/demo/runs", json={"case": "req-fix"}).json()["run_id"])
    r = api.post("/api/demo/runs", json={"case": "req-empty"})
    assert r.status_code == 429 and "used up" in r.json()["detail"]
    assert api.get("/api/demo").json()["left_today"] == 0


def test_the_global_cap_applies_to_the_demo(api, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_RUNS_PER_DAY", 0)
    assert api.post("/api/demo/runs", json={"case": "req-fix"}).status_code == 429


def test_the_gallery_shows_finished_demo_receipts_only(api):
    async def seed():
        s = api.store
        for run_id, user, status in [("d-done", demo.DEMO_USER, "done"), ("d-error", demo.DEMO_USER, "error"),
                                     ("d-running", demo.DEMO_USER, None), ("u-done", "u1", "done")]:
            await s.create(run_id, user, "psf__requests-1", "gold")
            if status:
                await s.finish(run_id, status, {"verdict": "PROVEN", "reason": "r", "seconds": 1.0})
            else:
                await s.mark_running(run_id)

    asyncio.run(seed())
    assert [r["id"] for r in api.get("/api/demo").json()["gallery"]] == ["d-done"]
