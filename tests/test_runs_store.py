import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
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


def test_jsonb_safe_strips_nul_characters_postgres_rejects():
    literal = r"C:\u0000dir"  # the six characters \u0000 in text are fine; only a real NUL is not
    ev = {"log": [{"output": "a\x00b"}], "k\x00": "x", "n": 1, "path": literal}
    assert db.jsonb_safe(ev) == {"log": [{"output": "ab"}], "k": "x", "n": 1, "path": literal}


class _FakePool:
    """Stands in for the psycopg pool: hands out connections whose execute() fails or succeeds in order."""

    def __init__(self, *fails):
        self.fails, self.checks = list(fails), 0

    @contextlib.asynccontextmanager
    async def connection(self):
        fail = self.fails.pop(0)

        class Conn:
            async def execute(self, sql, params=()):
                if fail:
                    raise psycopg.OperationalError("server closed the connection unexpectedly")
                return SimpleNamespace(rowcount=1)

        yield Conn()

    async def check(self):
        self.checks += 1


def test_pg_store_retries_once_when_neon_dropped_an_idle_connection(run):
    pool = _FakePool(True, False)
    run(db.PgRuns(pool).mark_running("r1"))
    assert pool.checks == 1 and pool.fails == []


def test_pg_store_gives_up_after_one_retry(run):
    pool = _FakePool(True, True)
    with pytest.raises(psycopg.OperationalError):
        run(db.PgRuns(pool).mark_running("r1"))


SOURCE = {"repo": "octo/hello", "pr_number": 1}


def test_pr_runs_keep_their_source_and_latest_per_pr(store, run):
    async def go():
        await store.create("a", "u1", "octo/hello#1", "github", T0, {**SOURCE, "head_sha": "s1"})
        await store.create("b", "u1", "octo/hello#1", "github", T0 + timedelta(minutes=1), {**SOURCE, "head_sha": "s2"})
        await store.finish("b", "done", EVIDENCE)
        await store.set_check_run("b", 555)
        return (await store.latest_for_prs("octo/hello", [1, 2]), await store.has_run_for_head("octo/hello", 1, "s1"),
                await store.has_run_for_head("octo/hello", 1, "s3"), await store.get("b"))
    latest, seen, unseen, row = run(go())
    assert latest[1]["id"] == "b" and latest[1]["verdict"] == "PROVEN" and 2 not in latest and seen and not unseen
    assert (row["repo"], row["pr_number"], row["head_sha"], row["check_run_id"]) == ("octo/hello", 1, "s2", 555)


def test_installations_sync_per_user(store, run):
    octo = {"id": 7, "account_login": "octo", "account_type": "User"}

    async def go():
        await store.sync_installations("u1", [octo])
        await store.sync_installations("u2", [octo])
        await store.sync_installations("u1", [])
        return await store.user_installations("u1"), await store.user_installations("u2"), await store.installation_owner(7)
    mine, theirs, owner = run(go())
    assert mine == [] and [i["id"] for i in theirs] == [7] and owner == "u2"


def test_auto_check_switch_and_installation_removal(store, run):
    async def go():
        await store.sync_installations("u1", [{"id": 7, "account_login": "octo", "account_type": "User"}])
        await store.set_auto_check(42, 7, "octo/hello", True)
        on = await store.auto_checks([42, 43])
        await store.set_auto_check(42, 7, "octo/hello", False)
        off = await store.auto_checks([42])
        await store.set_auto_check(42, 7, "octo/hello", True)
        await store.delete_installation(7)
        return on, off, await store.auto_checks([42]), await store.user_installations("u1")
    assert run(go()) == ({42}, set(), set(), [])


def test_global_recent_counts_every_user_in_the_window(store, run):
    async def go():
        await store.create("a", "u1", "x__y-1", "gold", T0)
        await store.create("b", "u2", "x__y-1", "gold", T0)
        await store.create("old", "u3", "x__y-1", "gold", T0 - timedelta(days=2))
        return await store.global_recent(T0 - timedelta(days=1))
    assert run(go()) == 2


def test_import_runs_skips_eval_output(tmp_path):
    # scripts/eval_prs.py results are comparisons for us, not public example receipts.
    ev = {"instance_id": "x", "verdict": "PROVEN", "reason": "r", "seconds": 1.0, "events": []}
    (tmp_path / "a-gold-20260101-000000.json").write_text(json.dumps(ev), encoding="utf-8")
    (tmp_path / "eval-final-pr16.json").write_text(json.dumps(ev), encoding="utf-8")
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "eval-fixed-pr15.json").write_text(json.dumps(ev), encoding="utf-8")
    store = db.MemoryRuns()
    assert asyncio.run(db.import_runs(store, tmp_path)) == 1
    assert list(store.rows) == ["a-gold-20260101-000000"]
