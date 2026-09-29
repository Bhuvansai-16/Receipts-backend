import json

import pytest
from fastapi.testclient import TestClient

from receipts import server, swebench
from receipts.swebench import Instance

ROWS = {
    "psf__requests-1": {"instance_id": "psf__requests-1", "repo": "psf/requests", "difficulty": "<15 min fix",
                        "problem_statement": "\nGET sends Content-Length\nmore text", "patch": "GOLD",
                        "PASS_TO_PASS": "[]"},
    "django__django-1": {"instance_id": "django__django-1", "repo": "django/django", "difficulty": "<15 min fix",
                         "problem_statement": "x", "patch": "", "PASS_TO_PASS": "[]"},
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(swebench, "_dataset", lambda: ROWS)
    monkeypatch.setattr(server.config, "RUNS_DIR", tmp_path)
    server.RUNS.clear()
    seen = {}

    async def fake_check(inst: Instance, patch, emit=None):
        seen["patch"] = patch
        emit("claim", {"kind": "fix", "claim": "c"})
        emit("fork", {"side": "base", "n": 1, "passed": False, "message": "assert 1 == 2"})
        emit("verdict", {"verdict": "PROVEN", "reason": "r", "seconds": 1.0, "tokens": 10})
        emit("done", {})
        return {"instance_id": inst.instance_id, "verdict": "PROVEN", "reason": "r",
                "started_at": "2026-09-28T00:00:00+00:00", "events": []}

    monkeypatch.setattr(server.engine, "check", fake_check)
    with TestClient(server.app) as c:
        c.seen = seen
        yield c


def sse_types(client, run_id):
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        return [line.split(":", 1)[1].strip() for line in r.iter_lines() if line.startswith("event:")]


def test_instances_lists_pytest_repos_only(client):
    body = client.get("/api/instances").json()
    assert [i["id"] for i in body] == ["psf__requests-1"]
    assert body[0]["title"] == "GET sends Content-Length"


def test_instance_detail_and_404(client):
    assert client.get("/api/instances/psf__requests-1").json()["problem_statement"].startswith("\nGET")
    assert client.get("/api/instances/nope__nope-1").status_code == 404


def test_run_streams_events_then_serves_saved_evidence(client, tmp_path):
    r = client.post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert sse_types(client, run_id) == ["status", "claim", "fork", "verdict", "done"]
    got = client.get(f"/api/runs/{run_id}").json()
    assert got["status"] == "done" and got["evidence"]["verdict"] == "PROVEN"
    assert got["evidence"]["pr"] == "gold" and client.seen["patch"] == "GOLD"
    assert (tmp_path / f"{run_id}.json").exists()


def test_diff_and_noop_prs_reach_the_engine(client):
    rid = client.post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "none"}).json()["run_id"]
    sse_types(client, rid)
    assert client.seen["patch"] is None
    rid = client.post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "diff",
                                         "diff": "diff --git a/x b/x\n"}).json()["run_id"]
    sse_types(client, rid)
    assert client.seen["patch"] == "diff --git a/x b/x\n"


@pytest.mark.parametrize("body, code", [
    ({"instance_id": "nope__nope-1", "pr": "gold"}, 404),
    ({"instance_id": "django__django-1", "pr": "gold"}, 404),
    ({"instance_id": "psf__requests-1", "pr": "maybe"}, 422),
    ({"instance_id": "psf__requests-1", "pr": "diff", "diff": "  "}, 422),
    ({"instance_id": "psf__requests-1", "pr": "diff", "diff": "x" * 200_001}, 413),
])
def test_start_run_validates_input(client, body, code):
    assert client.post("/api/runs", json=body).status_code == code


def test_runs_list_includes_runs_saved_on_disk_newest_first(client, tmp_path):
    (tmp_path / "psf__requests-1-gold-20260101-000000.json").write_text(json.dumps(
        {"instance_id": "psf__requests-1", "verdict": "REFUTED", "started_at": "2026-01-01T00:00:00+00:00"}))
    (tmp_path / "psf__requests-1-none-20260102-000000.json").write_text(json.dumps(
        {"instance_id": "psf__requests-1", "verdict": "PROVEN", "started_at": "2026-01-02T00:00:00+00:00"}))
    runs = client.get("/api/runs").json()
    assert [(r["pr"], r["verdict"], r["status"]) for r in runs] == [("none", "PROVEN", "done"),
                                                                    ("gold", "REFUTED", "done")]


def test_finished_run_without_stored_events_replays_done(client, tmp_path):
    (tmp_path / "psf__requests-1-gold-20260101-000000.json").write_text(json.dumps(
        {"instance_id": "psf__requests-1", "verdict": "REFUTED", "started_at": "2026-01-01T00:00:00+00:00"}))
    assert sse_types(client, "psf__requests-1-gold-20260101-000000") == ["done"]


def test_unknown_or_unsafe_run_ids_are_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fsecrets").status_code == 404
