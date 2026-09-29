import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from receipts import auth, config, db, server, swebench
from receipts.swebench import Instance

ROWS = {
    "psf__requests-1": {"instance_id": "psf__requests-1", "repo": "psf/requests", "difficulty": "<15 min fix",
                        "problem_statement": "\nGET sends Content-Length\nmore text", "patch": "GOLD",
                        "PASS_TO_PASS": "[]"},
    "django__django-1": {"instance_id": "django__django-1", "repo": "django/django", "difficulty": "<15 min fix",
                         "problem_statement": "x", "patch": "", "PASS_TO_PASS": "[]"},
}
USER = {"id": "u1", "email": "a@b.c", "name": "A", "image": None, "emailVerified": False}
T0 = datetime(2026, 9, 29, tzinfo=timezone.utc)


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(swebench, "_dataset", lambda: ROWS)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    server._instances.cache_clear()
    server.LIVE.clear()
    seen = {}

    async def fake_check(inst: Instance, patch, emit=None):
        seen["patch"] = patch
        emit("claim", {"kind": "fix", "claim": "c"})
        emit("fork", {"side": "base", "n": 1, "passed": False, "message": "assert 1 == 2"})
        emit("verdict", {"verdict": "PROVEN", "reason": "r", "seconds": 1.0, "tokens": 10})
        emit("done", {})
        return {"instance_id": inst.instance_id, "verdict": "PROVEN", "reason": "r", "seconds": 1.0,
                "tokens": {"m": {"total_tokens": 7}}, "started_at": "2026-09-29T00:00:00+00:00", "events": []}

    monkeypatch.setattr(server.engine, "check", fake_check)
    with TestClient(server.app) as c:
        c.seen = seen
        c.store = server.app.state.runs
        yield c
    server.app.dependency_overrides.clear()


def signed_in(api):
    server.app.dependency_overrides[auth.current_user] = lambda: USER
    return api


def start(api, pr="gold", **extra):
    r = api.post("/api/runs", json={"instance_id": "psf__requests-1", "pr": pr, **extra})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def sse_types(api, run_id):
    with api.stream("GET", f"/api/runs/{run_id}/events") as r:
        return [line.split(":", 1)[1].strip() for line in r.iter_lines() if line.startswith("event:")]


def test_health(api):
    assert api.get("/api/health").json() == {"ok": True}


def test_instances_are_pytest_only_and_cacheable(api):
    r = api.get("/api/instances")
    assert [i["id"] for i in r.json()] == ["psf__requests-1"] and r.json()[0]["title"] == "GET sends Content-Length"
    assert "max-age=3600" in r.headers["cache-control"]
    assert api.get("/api/instances", headers={"if-none-match": r.headers["etag"]}).status_code == 304


def test_instance_detail_and_404(api):
    assert api.get("/api/instances/psf__requests-1").json()["problem_statement"].startswith("\nGET")
    assert api.get("/api/instances/nope__nope-1").status_code == 404


def test_starting_a_check_needs_sign_in(api):
    assert api.post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"}).status_code == 401
    assert api.get("/api/runs").status_code == 401 and api.get("/api/me").status_code == 401


def test_me_returns_the_public_profile_only(api):
    assert signed_in(api).get("/api/me").json() == {"id": "u1", "email": "a@b.c", "name": "A", "image": None}


def test_run_streams_then_is_stored_for_its_owner(api):
    run_id = start(signed_in(api))
    assert sse_types(api, run_id) == ["status", "claim", "fork", "verdict", "done"]
    got = api.get(f"/api/runs/{run_id}").json()
    assert got["status"] == "done" and got["evidence"]["verdict"] == "PROVEN" and api.seen["patch"] == "GOLD"
    assert got["evidence"]["pr"] == "gold" and got["evidence"]["run_id"] == run_id
    assert [r["id"] for r in api.get("/api/runs").json()["runs"]] == [run_id]
    assert sse_types(api, run_id) == ["status", "claim", "fork", "verdict", "done"]  # replay equals live


def test_diff_and_noop_prs_reach_the_engine(api):
    sse_types(api, start(signed_in(api), "none"))
    assert api.seen["patch"] is None
    sse_types(api, start(api, "diff", diff="diff --git a/x b/x\n"))
    assert api.seen["patch"] == "diff --git a/x b/x\n"


def test_receipt_is_public_and_finished_ones_are_immutable(api):
    run_id = start(signed_in(api), "none")
    sse_types(api, run_id)
    server.app.dependency_overrides.clear()  # signed out now
    r = api.get(f"/api/runs/{run_id}")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert sse_types(api, run_id)[-1] == "done"


def test_unfinished_run_is_never_cached(api):
    asyncio.run(api.store.create("q1", "u1", "psf__requests-1", "gold"))
    r = api.get("/api/runs/q1")
    assert r.json()["status"] == "queued" and r.headers["cache-control"] == "no-store"


def test_engine_crash_ends_the_run_as_error(api, monkeypatch):
    async def crash(inst, patch, emit=None):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(server.engine, "check", crash)
    run_id = start(signed_in(api))
    assert sse_types(api, run_id) == ["status", "error", "done"]
    assert api.get(f"/api/runs/{run_id}").json()["status"] == "error"


def test_my_runs_are_only_mine_with_pages_and_etag(api):
    for i in range(3):
        asyncio.run(api.store.create(f"r{i}", "u1", "psf__requests-1", "gold", T0 + timedelta(minutes=i)))
    asyncio.run(api.store.create("theirs", "u2", "psf__requests-1", "gold", T0))
    first = signed_in(api).get("/api/runs?limit=2")
    body = first.json()
    assert [r["id"] for r in body["runs"]] == ["r2", "r1"] and body["next_cursor"]
    assert "evidence" not in body["runs"][0]
    rest = api.get(f"/api/runs?limit=2&cursor={body['next_cursor']}").json()
    assert [r["id"] for r in rest["runs"]] == ["r0"] and rest["next_cursor"] is None
    assert api.get("/api/runs?limit=2", headers={"if-none-match": first.headers["etag"]}).status_code == 304


def test_bad_cursor_is_400(api):
    assert signed_in(api).get("/api/runs?cursor=garbage").status_code == 400


def test_active_limit(api, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_RUNS", 2)
    for i in range(2):
        asyncio.run(api.store.create(f"a{i}", "u1", "psf__requests-1", "gold"))
    r = signed_in(api).post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"})
    assert r.status_code == 429 and "running" in r.json()["detail"]


def test_daily_limit(api, monkeypatch):
    monkeypatch.setattr(config, "RUNS_PER_DAY", 3)
    for i in range(3):
        asyncio.run(api.store.create(f"d{i}", "u1", "psf__requests-1", "gold"))
        asyncio.run(api.store.finish(f"d{i}", "done", {"verdict": "PROVEN"}))
    r = signed_in(api).post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"})
    assert r.status_code == 429 and "Daily limit" in r.json()["detail"]


@pytest.mark.parametrize("body, code", [
    ({"instance_id": "nope__nope-1", "pr": "gold"}, 404),
    ({"instance_id": "django__django-1", "pr": "gold"}, 404),
    ({"instance_id": "psf__requests-1", "pr": "maybe"}, 422),
    ({"instance_id": "psf__requests-1", "pr": "diff", "diff": "  "}, 422),
    ({"instance_id": "psf__requests-1", "pr": "diff", "diff": "x" * 200_001}, 413),
])
def test_start_run_validates_input(api, body, code):
    assert signed_in(api).post("/api/runs", json=body).status_code == code


def test_unknown_or_unsafe_run_ids_are_404(api):
    assert api.get("/api/runs/does-not-exist").status_code == 404
    assert api.get("/api/runs/..%2F..%2Fsecrets").status_code == 404
    assert api.get("/api/runs/does-not-exist/events").status_code == 404


def test_finished_run_without_stored_events_replays_done(api):
    asyncio.run(api.store.import_run("psf__requests-1-gold-20260101-000000", {
        "instance_id": "psf__requests-1", "verdict": "REFUTED", "started_at": "2026-01-01T00:00:00+00:00"}))
    assert sse_types(api, "psf__requests-1-gold-20260101-000000") == ["done"]


def test_unfinished_runs_fail_on_startup(monkeypatch, tmp_path):
    store = db.MemoryRuns()
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    asyncio.run(store.create("stuck", "u1", "psf__requests-1", "gold"))
    monkeypatch.setattr(swebench, "_dataset", lambda: ROWS)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(server.db, "MemoryRuns", lambda: store)
    with TestClient(server.app) as c:
        got = c.get("/api/runs/stuck").json()
    assert got["status"] == "error"
    assert got["evidence"]["events"] == [{"type": "error", "data": {"message": db.RESTARTED}}]


def test_cors_allows_only_the_frontend(api):
    ok = api.options("/api/runs", headers={"origin": config.FRONTEND_URL, "access-control-request-method": "POST",
                                           "access-control-request-headers": "content-type"})
    assert ok.headers.get("access-control-allow-origin") == config.FRONTEND_URL
    assert ok.headers.get("access-control-allow-credentials") == "true"
    bad = api.options("/api/runs", headers={"origin": "https://evil.com", "access-control-request-method": "POST"})
    assert bad.headers.get("access-control-allow-origin") is None


def test_cors_allows_the_header_neons_client_sends(api):
    r = api.options("/api/auth/get-session", headers={
        "origin": config.FRONTEND_URL, "access-control-request-method": "GET",
        "access-control-request-headers": "content-type,x-neon-client-info"})
    assert r.status_code == 200 and "x-neon-client-info" in r.headers["access-control-allow-headers"].lower()


def test_without_a_database_saved_runs_are_served(monkeypatch, tmp_path):
    (tmp_path / "psf__requests-1-gold-20260101-000000.json").write_text(json.dumps({
        "instance_id": "psf__requests-1", "verdict": "PROVEN", "started_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8")
    monkeypatch.setattr(swebench, "_dataset", lambda: ROWS)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    with TestClient(server.app) as c:
        got = c.get("/api/runs/psf__requests-1-gold-20260101-000000").json()
    assert got["status"] == "done" and got["evidence"]["verdict"] == "PROVEN"
