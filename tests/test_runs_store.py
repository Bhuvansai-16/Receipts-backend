import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from receipts import db

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
EVIDENCE = {"instance_id": "x__y-1", "verdict": "PROVEN", "reason": "r", "seconds": 12.5,
            "tokens": {"a": {"total_tokens": 100}, "b": {"total_tokens": 23}}, "events": [{"type": "done", "data": {}}]}
# The Neon variant runs only against a throwaway database (e.g. a Neon branch): it truncates `runs`.
STORES = ["memory"] + (["neon"] if os.environ.get("TEST_DATABASE_URL") else [])


@pytest.fixture(scope="module")
def run():
    """One event loop for the module: a psycopg pool is bound to the loop that opened it."""
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


async def _neon_store(url: str) -> db.PgRuns:
    await db.migrate(url)
    pool = await db.open_pool(url)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE runs")
    return db.PgRuns(pool)


@pytest.fixture(params=STORES)
def store(request, run):
    if request.param == "memory":
        yield db.MemoryRuns()
        return
    store = run(_neon_store(os.environ["TEST_DATABASE_URL"]))
    yield store
    run(store.pool.close())


def test_create_then_finish_stores_result_columns_and_evidence(store, run):
    async def go():
        await store.create("r1", "u1", "x__y-1", "gold", T0)
        await store.mark_running("r1")
        assert (await store.get("r1"))["status"] == "running"
        await store.finish("r1", "done", EVIDENCE)
        return await store.get("r1")
    row = run(go())
    assert (row["status"], row["verdict"], row["seconds"], row["tokens"]) == ("done", "PROVEN", 12.5, 123)
    assert row["evidence"]["events"] == [{"type": "done", "data": {}}] and row["finished_at"] is not None


def test_list_for_user_is_own_newest_first_with_keyset_pages(store, run):
    async def go():
        for i in range(3):
            await store.create(f"r{i}", "u1", "x__y-1", "gold", T0 + timedelta(minutes=i))
        await store.create("other", "u2", "x__y-1", "gold", T0)
        first = await store.list_for_user("u1", 2)
        rest = await store.list_for_user("u1", 2, (first[-1]["started_at"], first[-1]["id"]))
        return first, rest
    first, rest = run(go())
    assert [r["id"] for r in first] == ["r2", "r1"] and [r["id"] for r in rest] == ["r0"]
    assert set(first[0]) == set(db.SUMMARY_KEYS)  # summary only: never the big evidence column


def test_usage_counts_active_and_recent(store, run):
    async def go():
        await store.create("a", "u1", "x__y-1", "gold", T0)
        await store.create("b", "u1", "x__y-1", "gold", T0 - timedelta(days=2))
        await store.finish("b", "done", EVIDENCE)
        return await store.usage("u1", T0 - timedelta(days=1))
    assert run(go()) == (1, 1)


def test_fail_unfinished_marks_active_runs_as_error(store, run):
    async def go():
        await store.create("q", "u1", "x__y-1", "gold", T0)
        await store.create("r", "u1", "x__y-1", "gold", T0)
        await store.mark_running("r")
        n = await store.fail_unfinished()
        return n, (await store.get("q"))["status"], (await store.get("r"))["status"]
    assert run(go()) == (2, "error", "error")


def test_import_run_is_idempotent(store, run):
    ev = {**EVIDENCE, "started_at": "2026-09-28T14:44:00+00:00"}

    async def go():
        await store.import_run("x__y-1-gold-20260928-201414", ev)
        await store.import_run("x__y-1-gold-20260928-201414", ev)
        return await store.get("x__y-1-gold-20260928-201414")
    row = run(go())
    assert (row["user_id"], row["pr"], row["status"], row["verdict"]) == (None, "gold", "done", "PROVEN")
    assert row["started_at"] == datetime(2026, 9, 28, 14, 44, tzinfo=timezone.utc)


def test_cursor_round_trip_and_rejects_garbage():
    assert db.decode_cursor(db.encode_cursor(T0, "r1")) == (T0, "r1")
    with pytest.raises(ValueError):
        db.decode_cursor("not a cursor")


def test_pr_comes_from_evidence_or_the_run_id():
    assert db.pr_of("x__y-1-none-20260928-201414", {"instance_id": "x__y-1"}) == "none"
    assert db.pr_of("x__y-1-my-fix-20260928-201414", {"instance_id": "x__y-1"}) == "diff"
    assert db.pr_of("x__y-1-gold-20260928-201414-2", {"instance_id": "x__y-1", "pr": "gold"}) == "gold"
    assert db.pr_of("x__y-1-none-20260928-201414", {"instance_id": "x__y-1", "pr": "bogus"}) == "none"


def test_import_runs_skips_files_without_a_verdict(tmp_path, run):
    (tmp_path / "x__y-1-gold-20260928-201414.json").write_text(json.dumps(EVIDENCE), encoding="utf-8")
    (tmp_path / "broken-20260928-201414.json").write_text("{}", encoding="utf-8")
    assert run(db.import_runs(db.MemoryRuns(), tmp_path)) == 1


def test_import_runs_skips_unreadable_files(tmp_path, run):
    (tmp_path / "x__y-1-gold-20260928-201414.json").write_text(json.dumps(EVIDENCE), encoding="utf-8")
    (tmp_path / "x__y-1-gold-20260928-201500.json").write_text('{"instance_id": "x__y-1", "verd', encoding="utf-8")
    assert run(db.import_runs(db.MemoryRuns(), tmp_path)) == 1
