# Separate frontend, Neon login and database: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split Receipts into `receipts-backend` (FastAPI) and `receipts-frontend` (React) repos, add sign-up/sign-in through a FastAPI proxy to Neon Managed Better Auth, store runs in Neon Postgres, and add a minimal landing page.

**Architecture:** The browser only talks to FastAPI. `/api/auth/*` is a proxy that mirrors Neon's own server SDK (same forwarded headers, cookie rewriting and OAuth session-verifier exchange). Runs live in a `runs` table in Neon (psycopg 3 async pool, plain SQL); live progress stays in memory while a run is active. The frontend uses Neon's `@neondatabase/auth` client pointed at the FastAPI proxy.

**Tech Stack:** Python 3.12 (global, no venv), FastAPI 0.141, psycopg 3.3 + psycopg-pool 3.3, httpx 0.28, sse-starlette; React 19 + Vite 8 + TypeScript 7, `@neondatabase/auth` 0.5.0-beta, react-router 7.

**Spec:** `docs/superpowers/specs/2026-09-29-accounts-and-split-design.md`

## Global Constraints

- No venv; backend runs on global Python 3.12 with already-installed packages (psycopg[binary] 3.3.5, psycopg-pool 3.3.1, httpx 0.28.1, fastapi 0.141.1, sse-starlette 3.4). Config only from `.env`.
- Neon: app traffic uses the pooled `DATABASE_URL` (`-pooler` host); migrations use `DATABASE_URL_UNPOOLED`; both carry `sslmode=require&channel_binding=require`.
- Auth: the browser never calls Neon directly. Proxied cookies: name prefix `__Secure-neon-auth`, forced `Secure`, `HttpOnly`, `SameSite=Lax`, no `Partitioned`, `Domain=COOKIE_DOMAIN` only when set. Verifier param `neon_auth_session_verifier`.
- Windows dev: psycopg async needs a selector event loop. `serve` passes `loop="asyncio:SelectorEventLoop"` to uvicorn on win32; DB CLI commands use `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)` on win32.
- Receipts are public by link; starting checks and listing your runs need a session. Limits: `MAX_ACTIVE_RUNS=2`, `RUNS_PER_DAY=20` (rolling 24 h).
- Frontend: `createAuthClient(`${VITE_API_URL}/api/auth`)` from `@neondatabase/auth`; all API calls use `credentials: "include"`.
- Design system unchanged (`PRODUCT.md`): white field, black type, no blue, honey accents, verdicts always glyph + word, WCAG 2.2 AA. UI copy has no em dashes.
- Files written by scripts use explicit UTF-8 (`encoding="utf-8"`).

## Review Focus

1. Expired or revoked session cookie: upstream `get-session` returns `null`; the API must answer 401 (not 500) and the frontend must fall back to signed-out. Test: `test_current_user_401_when_upstream_session_is_null` (Task 3).
2. OAuth `next` pointing at another site (or a look-alike prefix such as `http://localhost:5173.evil.com`): must redirect to `FRONTEND_URL/app`. Test: `test_safe_next_blocks_other_sites` (Task 3).
3. Receipt link opened while signed out: must render (public), including its event replay. Test: `test_receipt_is_public` (Task 4).
4. Backend restarts mid-run: the run must end as `error`, never stay "running" forever. Test: `test_unfinished_runs_fail_on_startup` (Task 4).
5. Tampered pagination cursor: must be a 400, not a 500. Test: `test_bad_cursor_is_400` (Task 4).

## Rulings and self-review fixes

Spec deviations (reason in brackets):
- Frontend auth client is `createAuthClient(`${VITE_API_URL}/api/auth`)` from `@neondatabase/auth`, without `fetchOptions` (0.5.0-beta has no such option; Better Auth's client already sends `credentials: "include"`; `@neondatabase/neon-js` would pull pg and prettier into the bundle).
- The proxy does not forward `content-encoding` (httpx hands over the decoded body). Proxy errors are `{"code", "message"}`, Better Auth's error shape, so the client shows the message.
- Proxy Origin fallback is the request's own origin, as in Neon's SDK; `current_user` sends `FRONTEND_URL`.
- Index `(user_id, started_at DESC, id DESC)` matches the list query's `ORDER BY` exactly.
- A run's stored `evidence.events` are the server-published events (they include `status`), so a replay equals the live stream.

Fixes to the code below, applied during implementation:
- Task 1: stop the API preview first; `cd ..` in the same command as the rename (a shell whose cwd is inside the folder blocks the rename on Windows); create `receipts-frontend/.gitignore` in Task 1 (not Task 5); delete Vite's `/api` dev proxy (the UI calls `VITE_API_URL` directly); drop the `web/` lines from the backend `.gitignore`.
- Task 2 tests: one module-scoped event loop (`run` fixture = `loop.run_until_complete`) instead of `asyncio.run` per call, because a psycopg pool is bound to the loop that opened it.
- Task 2 `db.py`: writes go through `_exec` (returns rowcount) instead of `RETURNING id` + `fetchall`; `coalesce(%s::timestamptz, now())`; the pool uses `check=AsyncConnectionPool.check_connection` (Neon scales to zero and drops idle connections, so the first request after a pause would fail); an imported `pr` must be gold, none or diff, else it comes from the run id.
- Task 3 tests: the fake upstream is a `SimpleNamespace`; request bodies are compared as parsed JSON.
- Task 3 `auth.py`: `close()` resets the client; `/complete` sends only the verifier to `get-session` and treats "no Set-Cookie" or an unreachable upstream as failure (redirect to `/signin?error=oauth`); `current_user` tolerates non-JSON bodies; an empty cookie header is not sent.
- Task 4: `_execute` stores `live.events` as `evidence.events`; `asyncio` is imported once at the top of the tests.

---

### Task 1: Split into two repositories

**Files:**
- Rename folder: `Nebiusxnemo/receipts/` → `Nebiusxnemo/receipts-backend/`
- Create repo: `Nebiusxnemo/receipts-frontend/` (history of `web/` kept)
- Delete from backend: `web/`
- Modify: `Nebiusxnemo/.claude/launch.json`

**Interfaces:**
- Produces: `receipts-frontend/` with the current UI at its root (`src/`, `package.json`, …) on branch `main`; `receipts-backend/` without `web/`.

- [ ] **Step 1: Stop anything holding files** (the preview server), then make a branch with only `web/`'s history

```bash
cd /c/Users/Bhuvansai/OneDrive/Documents/Nebiusxnemo/receipts
git subtree split --prefix=web -b web-history
```
Expected: prints a commit hash.

- [ ] **Step 2: Create the frontend repo from that branch and carry over installed modules**

```bash
cd /c/Users/Bhuvansai/OneDrive/Documents/Nebiusxnemo
git clone --branch web-history --single-branch receipts receipts-frontend
cd receipts-frontend && git branch -m web-history main && git remote remove origin
mv ../receipts/web/node_modules ./node_modules
git log --oneline | head -3 && ls
```
Expected: the three web commits, and `index.html package.json src …` at the root.

- [ ] **Step 3: Remove `web/` from the backend repo and rename the folder**

```bash
cd /c/Users/Bhuvansai/OneDrive/Documents/Nebiusxnemo/receipts
git rm -r -q web && rm -rf web && git branch -D web-history
git commit -q -m "chore: move the web UI to its own repo (receipts-frontend)"
cd .. && mv receipts receipts-backend
```

- [ ] **Step 4: Point the preview launcher at the renamed folder**

`Nebiusxnemo/.claude/launch.json`:
```json
{
  "version": "0.0.1",
  "configurations": [
    {
      "name": "receipts-api",
      "runtimeExecutable": "python",
      "runtimeArgs": ["-m", "uvicorn", "receipts.server:app", "--app-dir", "receipts-backend", "--port", "8000", "--loop", "asyncio:SelectorEventLoop"],
      "port": 8000
    },
    {
      "name": "receipts-web",
      "runtimeExecutable": "npm",
      "runtimeArgs": ["--prefix", "receipts-frontend", "run", "dev"],
      "port": 5173
    }
  ]
}
```

- [ ] **Step 5: Verify both repos**

Run: `cd receipts-backend && python -m pytest tests/ -q` (expect all pass except none failing) and `cd ../receipts-frontend && npx vitest run` (expect 13 passed).

---

### Task 2: Runs store in Neon (+ migrate and import commands)

**Files:**
- Modify: `receipts-backend/receipts/config.py`
- Create: `receipts-backend/receipts/db.py`, `receipts-backend/migrations/001_init.sql`
- Modify: `receipts-backend/receipts/__main__.py`
- Test: `receipts-backend/tests/test_runs_store.py`

**Interfaces:**
- Produces:
  - `config.DATABASE_URL: str`, `config.DATABASE_URL_UNPOOLED: str`, `config.NEON_AUTH_URL: str`, `config.FRONTEND_URL: str`, `config.API_URL: str`, `config.COOKIE_DOMAIN: str | None`, `config.MAX_ACTIVE_RUNS: int`, `config.RUNS_PER_DAY: int`
  - `db.SUMMARY_KEYS: tuple[str, ...]`, `db.encode_cursor(started_at: datetime, run_id: str) -> str`, `db.decode_cursor(cursor: str) -> tuple[datetime, str]` (raises `ValueError`)
  - Store interface implemented by `db.MemoryRuns()` and `db.PgRuns(pool)`: `create(run_id, user_id, instance_id, pr, started_at=None)`, `mark_running(run_id)`, `finish(run_id, status, evidence)`, `get(run_id) -> dict | None` (summary keys + `evidence`), `list_for_user(user_id, limit, after=None) -> list[dict]` (summary keys, newest first), `usage(user_id, since) -> tuple[int, int]` (active, started since), `fail_unfinished() -> int`, `import_run(run_id, evidence) -> None` (idempotent)
  - `db.open_pool(url) -> AsyncConnectionPool`, `db.migrate(url) -> list[str]`, `db.import_runs(store, runs_dir) -> int`, `db.pr_from_run_id(run_id, instance_id) -> str`
  - CLI: `python -m receipts migrate`, `python -m receipts import-runs`

- [ ] **Step 1: Add settings to `receipts/config.py`** (after `RUNS_DIR`)

```python
# Neon: pooled URL for the app, direct (unpooled) URL for migrations (neon.com/docs/connect/connection-pooling)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATABASE_URL_UNPOOLED = os.environ.get("DATABASE_URL_UNPOOLED", "") or DATABASE_URL
NEON_AUTH_URL = os.environ.get("NEON_AUTH_URL", "").rstrip("/")  # Neon Console > Auth > Configuration
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173").rstrip("/")
API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "") or None  # e.g. ".example.com" for app. + api. subdomains
MAX_ACTIVE_RUNS = int(os.environ.get("MAX_ACTIVE_RUNS", "2"))
RUNS_PER_DAY = int(os.environ.get("RUNS_PER_DAY", "20"))
```

- [ ] **Step 2: Write the migration** `migrations/001_init.sql`

```sql
-- Runs and their evidence. Users live in neon_auth (managed by Neon); no foreign key into that schema.
CREATE TABLE runs (
  id          text PRIMARY KEY,
  user_id     text,
  instance_id text NOT NULL,
  pr          text NOT NULL CHECK (pr IN ('gold', 'none', 'diff')),
  status      text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'error')),
  verdict     text,
  reason      text,
  seconds     real,
  tokens      integer,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  evidence    jsonb
);

CREATE INDEX runs_user_started ON runs (user_id, started_at DESC, id);
```

- [ ] **Step 3: Write the failing tests** `tests/test_runs_store.py`

```python
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


def run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


async def _pg_store():
    await db.migrate(os.environ["TEST_DATABASE_URL"])
    pool = await db.open_pool(os.environ["TEST_DATABASE_URL"])
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE runs")
    return db.PgRuns(pool)


STORES = ["memory"] + (["neon"] if os.environ.get("TEST_DATABASE_URL") else [])


@pytest.fixture(params=STORES)
def store(request):
    if request.param == "memory":
        return db.MemoryRuns()
    return run(_pg_store())


def test_create_then_finish_stores_result_columns_and_evidence(store):
    async def go():
        await store.create("r1", "u1", "x__y-1", "gold", T0)
        await store.mark_running("r1")
        assert (await store.get("r1"))["status"] == "running"
        await store.finish("r1", "done", EVIDENCE)
        return await store.get("r1")
    row = run(go())
    assert (row["status"], row["verdict"], row["seconds"], row["tokens"]) == ("done", "PROVEN", 12.5, 123)
    assert row["evidence"]["events"] == [{"type": "done", "data": {}}] and row["finished_at"] is not None


def test_list_for_user_is_own_newest_first_with_keyset_pages(store):
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


def test_usage_counts_active_and_recent(store):
    async def go():
        await store.create("a", "u1", "x__y-1", "gold", T0)
        await store.create("b", "u1", "x__y-1", "gold", T0 - timedelta(days=2))
        await store.finish("b", "done", EVIDENCE)
        return await store.usage("u1", T0 - timedelta(days=1))
    assert run(go()) == (1, 1)


def test_fail_unfinished_marks_active_runs_as_error(store):
    async def go():
        await store.create("q", "u1", "x__y-1", "gold", T0)
        await store.create("r", "u1", "x__y-1", "gold", T0)
        await store.mark_running("r")
        n = await store.fail_unfinished()
        return n, (await store.get("q"))["status"], (await store.get("r"))["status"]
    assert run(go()) == (2, "error", "error")


def test_import_run_is_idempotent(store):
    ev = {**EVIDENCE, "started_at": "2026-09-28T14:44:00+00:00"}
    async def go():
        await store.import_run("x__y-1-gold-20260928-201414", ev)
        await store.import_run("x__y-1-gold-20260928-201414", ev)
        return await store.get("x__y-1-gold-20260928-201414")
    row = run(go())
    assert (row["user_id"], row["pr"], row["status"], row["verdict"]) == (None, "gold", "done", "PROVEN")


def test_cursor_round_trip_and_rejects_garbage():
    assert db.decode_cursor(db.encode_cursor(T0, "r1")) == (T0, "r1")
    with pytest.raises(ValueError):
        db.decode_cursor("not a cursor")


def test_pr_from_run_id():
    assert db.pr_from_run_id("x__y-1-none-20260928-201414", "x__y-1") == "none"
    assert db.pr_from_run_id("x__y-1-my-fix-20260928-201414", "x__y-1") == "diff"


def test_import_runs_skips_files_without_a_verdict(tmp_path):
    (tmp_path / "x__y-1-gold-20260928-201414.json").write_text(json.dumps(EVIDENCE), encoding="utf-8")
    (tmp_path / "broken-20260928-201414.json").write_text("{}", encoding="utf-8")
    store = db.MemoryRuns()
    assert run(db.import_runs(store, tmp_path)) == 1
```

- [ ] **Step 4: Run to verify they fail**

Run: `python -m pytest tests/test_runs_store.py -q`
Expected: FAIL, `ImportError: cannot import name 'db'`.

- [ ] **Step 5: Implement `receipts/db.py`**

```python
"""Runs storage: Neon Postgres (psycopg 3 async pool, plain SQL) and an in-memory twin for tests."""
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import config

SUMMARY_KEYS = ("id", "user_id", "instance_id", "pr", "status", "verdict", "reason", "seconds", "tokens",
                "started_at", "finished_at")
SUMMARY = ", ".join(SUMMARY_KEYS)
MIGRATIONS = config.ROOT / "migrations"
ACTIVE = ("queued", "running")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def result_columns(evidence: dict) -> dict:
    tokens = sum(int(u.get("total_tokens", 0)) for u in (evidence.get("tokens") or {}).values())
    return {"verdict": evidence.get("verdict"), "reason": evidence.get("reason"),
            "seconds": evidence.get("seconds"), "tokens": tokens}


def encode_cursor(started_at: datetime, run_id: str) -> str:
    return base64.urlsafe_b64encode(f"{started_at.isoformat()}|{run_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        started_at, run_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(started_at), run_id
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"bad cursor: {cursor!r}") from e


def pr_from_run_id(run_id: str, instance_id: str) -> str:
    """Run ids are <instance>-<label>-<YYYYmmdd-HHMMSS>; labels other than gold/none were diff files."""
    label = run_id[len(instance_id) + 1:-16]
    return label if label in ("gold", "none") else "diff"


def _started(evidence: dict) -> datetime:
    try:
        return datetime.fromisoformat(evidence["started_at"])
    except (KeyError, TypeError, ValueError):
        return _now()


class MemoryRuns:
    """Same interface as PgRuns, in a dict. Used by tests and when DATABASE_URL is not set."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def create(self, run_id, user_id, instance_id, pr, started_at=None):
        self.rows[run_id] = {**dict.fromkeys(SUMMARY_KEYS), "id": run_id, "user_id": user_id,
                             "instance_id": instance_id, "pr": pr, "status": "queued",
                             "started_at": started_at or _now(), "evidence": None}

    async def mark_running(self, run_id):
        self.rows[run_id]["status"] = "running"

    async def finish(self, run_id, status, evidence):
        self.rows[run_id].update(status=status, evidence=evidence, finished_at=_now(), **result_columns(evidence))

    async def get(self, run_id):
        row = self.rows.get(run_id)
        return dict(row) if row else None

    async def list_for_user(self, user_id, limit, after=None):
        mine = sorted((r for r in self.rows.values() if r["user_id"] == user_id),
                      key=lambda r: (r["started_at"], r["id"]), reverse=True)
        if after:
            mine = [r for r in mine if (r["started_at"], r["id"]) < after]
        return [{k: r[k] for k in SUMMARY_KEYS} for r in mine[:limit]]

    async def usage(self, user_id, since):
        mine = [r for r in self.rows.values() if r["user_id"] == user_id]
        return sum(r["status"] in ACTIVE for r in mine), sum(r["started_at"] >= since for r in mine)

    async def fail_unfinished(self):
        stale = [r for r in self.rows.values() if r["status"] in ACTIVE]
        for r in stale:
            r.update(status="error", reason="backend restarted before the check finished", finished_at=_now())
        return len(stale)

    async def import_run(self, run_id, evidence):
        if run_id in self.rows:
            return
        instance_id = evidence["instance_id"]
        self.rows[run_id] = {**dict.fromkeys(SUMMARY_KEYS), "id": run_id, "instance_id": instance_id,
                             "pr": evidence.get("pr") or pr_from_run_id(run_id, instance_id), "status": "done",
                             "started_at": _started(evidence), "finished_at": _started(evidence),
                             "evidence": evidence, **result_columns(evidence)}


class PgRuns:
    """Runs in Neon. Each method is one short statement on a pooled connection."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def _one(self, sql, params=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchone()

    async def _all(self, sql, params=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()

    async def create(self, run_id, user_id, instance_id, pr, started_at=None):
        await self._all("INSERT INTO runs (id, user_id, instance_id, pr, status, started_at) "
                        "VALUES (%s, %s, %s, %s, 'queued', coalesce(%s, now())) RETURNING id",
                        (run_id, user_id, instance_id, pr, started_at))

    async def mark_running(self, run_id):
        await self._all("UPDATE runs SET status = 'running' WHERE id = %s RETURNING id", (run_id,))

    async def finish(self, run_id, status, evidence):
        c = result_columns(evidence)
        await self._all("UPDATE runs SET status = %s, verdict = %s, reason = %s, seconds = %s, tokens = %s, "
                        "evidence = %s, finished_at = now() WHERE id = %s RETURNING id",
                        (status, c["verdict"], c["reason"], c["seconds"], c["tokens"], Jsonb(evidence), run_id))

    async def get(self, run_id):
        return await self._one(f"SELECT {SUMMARY}, evidence FROM runs WHERE id = %s", (run_id,))

    async def list_for_user(self, user_id, limit, after=None):
        if after:
            return await self._all(f"SELECT {SUMMARY} FROM runs WHERE user_id = %s AND (started_at, id) < (%s, %s) "
                                   "ORDER BY started_at DESC, id DESC LIMIT %s", (user_id, *after, limit))
        return await self._all(f"SELECT {SUMMARY} FROM runs WHERE user_id = %s "
                               "ORDER BY started_at DESC, id DESC LIMIT %s", (user_id, limit))

    async def usage(self, user_id, since):
        row = await self._one("SELECT count(*) FILTER (WHERE status IN ('queued', 'running')) AS active, "
                              "count(*) FILTER (WHERE started_at >= %s) AS recent FROM runs WHERE user_id = %s",
                              (since, user_id))
        return row["active"], row["recent"]

    async def fail_unfinished(self):
        rows = await self._all("UPDATE runs SET status = 'error', finished_at = now(), "
                               "reason = 'backend restarted before the check finished' "
                               "WHERE status IN ('queued', 'running') RETURNING id")
        return len(rows)

    async def import_run(self, run_id, evidence):
        c = result_columns(evidence)
        instance_id = evidence["instance_id"]
        await self._all("INSERT INTO runs (id, user_id, instance_id, pr, status, verdict, reason, seconds, tokens, "
                        "started_at, finished_at, evidence) VALUES (%s, NULL, %s, %s, 'done', %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING RETURNING id",
                        (run_id, instance_id, evidence.get("pr") or pr_from_run_id(run_id, instance_id),
                         c["verdict"], c["reason"], c["seconds"], c["tokens"], _started(evidence), _started(evidence),
                         Jsonb(evidence)))


async def open_pool(url: str) -> AsyncConnectionPool:
    # Neon computes can scale to zero; the first connection may take a few seconds.
    pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False, kwargs={"row_factory": dict_row})
    await pool.open(wait=True, timeout=30)
    return pool


async def migrate(url: str) -> list[str]:
    """Apply migrations/*.sql not yet recorded, in name order, over a direct connection."""
    applied = []
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                           "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.stem in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            applied.append(path.stem)
    return applied


async def import_runs(store, runs_dir: Path) -> int:
    """Load finished runs saved by the CLI (runs/*.json) as public example receipts (no owner)."""
    count = 0
    for path in sorted(runs_dir.glob("*.json")):
        evidence = json.loads(path.read_text(encoding="utf-8"))
        if evidence.get("instance_id") and evidence.get("verdict"):
            await store.import_run(path.stem, evidence)
            count += 1
    return count
```

- [ ] **Step 6: Add the CLI commands** in `receipts/__main__.py`

Add below `_closing`:
```python
def _run_async(coro):
    """psycopg's async mode needs a selector event loop on Windows."""
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


async def _import_runs() -> int:
    from . import db

    pool = await db.open_pool(config.DATABASE_URL)
    try:
        return await db.import_runs(db.PgRuns(pool), config.RUNS_DIR)
    finally:
        await pool.close()
```
Register parsers next to `serve`:
```python
    sub.add_parser("migrate", help="apply database migrations (uses DATABASE_URL_UNPOOLED)")
    sub.add_parser("import-runs", help="load runs/*.json into the database as public example receipts")
```
Dispatch before the `from .engine import ...` line:
```python
    if a.cmd in ("migrate", "import-runs"):
        if not config.DATABASE_URL:
            sys.exit("error: set DATABASE_URL (and DATABASE_URL_UNPOOLED) in .env first")
        if a.cmd == "migrate":
            from . import db

            applied = _run_async(db.migrate(config.DATABASE_URL_UNPOOLED))
            return print(f"applied: {', '.join(applied) or 'nothing new'}")
        return print(f"imported {_run_async(_import_runs())} runs")
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_runs_store.py -q`
Expected: all pass (memory store; the Neon parametrization runs only when `TEST_DATABASE_URL` is set).

- [ ] **Step 8: Commit**

```bash
git add receipts/config.py receipts/db.py receipts/__main__.py migrations tests/test_runs_store.py
git commit -m "feat: runs store in Neon Postgres with migrate and import-runs commands"
```

---

### Task 3: Auth proxy to Neon Managed Better Auth

**Files:**
- Create: `receipts-backend/receipts/auth.py`
- Test: `receipts-backend/tests/test_auth.py`

**Interfaces:**
- Consumes: `config.NEON_AUTH_URL`, `config.FRONTEND_URL`, `config.COOKIE_DOMAIN`
- Produces: `auth.router` (FastAPI `APIRouter`, prefix `/api/auth`), `auth.current_user(request) -> dict` (dependency; 401 if signed out), `auth.close()` (async, closes the HTTP client), helpers `auth.neon_cookies(str) -> str`, `auth.rewrite_set_cookie(str, domain) -> str`, `auth.safe_next(str | None) -> str`, `auth.sessions` (cache with `.items` dict)

- [ ] **Step 1: Write the failing tests** `tests/test_auth.py`

```python
import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from receipts import auth, config

AUTH = "https://ep-test.neonauth.aws.neon.tech/neondb/auth"
SESSION = "__Secure-neon-auth.session_token=tok123"
CHALLENGE = "__Secure-neon-auth.session_challenge=chal456"


@pytest.fixture
def upstream(monkeypatch):
    """Fake Neon Auth: records requests, answers from `upstream.reply`."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return upstream.reply(request)

    upstream.calls = calls
    upstream.reply = lambda r: httpx.Response(200, json={"user": {"id": "u1", "email": "a@b.c"}})
    monkeypatch.setattr(config, "NEON_AUTH_URL", AUTH)
    monkeypatch.setattr(config, "FRONTEND_URL", "http://localhost:5173")
    monkeypatch.setattr(config, "COOKIE_DOMAIN", None)
    monkeypatch.setattr(auth, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    auth.sessions.items.clear()
    return upstream


@pytest.fixture
def client(upstream):
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/me")
    async def me(user: dict = Depends(auth.current_user)):
        return user

    return TestClient(app, follow_redirects=False)


def test_neon_cookies_keeps_only_neon_auth_cookies():
    assert auth.neon_cookies(f"theme=dark; {SESSION}; other=1") == SESSION


def test_rewrite_set_cookie_makes_it_first_party():
    out = auth.rewrite_set_cookie(f"{SESSION}; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT; "
                                  "SameSite=None; Partitioned; Secure", ".example.com")
    parts = [p.strip() for p in out.split(";")]
    assert parts[0] == SESSION and "Partitioned" not in parts and "SameSite=None" not in parts
    assert {"Secure", "HttpOnly", "SameSite=Lax", "Domain=.example.com", "Path=/"} <= set(parts)
    assert "Expires=Wed, 21 Oct 2026 07:28:00 GMT" in parts


def test_safe_next_blocks_other_sites(monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_URL", "http://localhost:5173")
    assert auth.safe_next("http://localhost:5173/runs/x") == "http://localhost:5173/runs/x"
    for bad in (None, "https://evil.com/app", "http://localhost:5173.evil.com/app", "//evil.com"):
        assert auth.safe_next(bad) == "http://localhost:5173/app"


def test_proxy_forwards_like_neon_sdk(client, upstream):
    upstream.reply = lambda r: httpx.Response(
        200, json={"ok": True},
        headers=[("set-cookie", f"{SESSION}; Path=/; SameSite=None; Partitioned"), ("set-auth-jwt", "jwt1"),
                 ("server", "leak")])
    r = client.post("/api/auth/sign-in/email?x=1", json={"email": "a@b.c", "password": "pw123456"},
                    headers={"origin": "http://localhost:5173", "cookie": f"{SESSION}; theme=dark",
                             "user-agent": "ua", "x-other": "no"})
    sent = upstream.calls[0]
    assert str(sent.url) == f"{AUTH}/sign-in/email?x=1" and sent.method == "POST"
    assert sent.headers["origin"] == "http://localhost:5173" and sent.headers["cookie"] == SESSION
    assert sent.headers["x-neon-auth-middleware"] == "true" and sent.headers["user-agent"] == "ua"
    assert "x-other" not in sent.headers and b'"password":"pw123456"' in sent.content
    assert r.status_code == 200 and r.json() == {"ok": True} and r.headers["set-auth-jwt"] == "jwt1"
    assert "server" not in r.headers or r.headers["server"] != "leak"
    cookie = r.headers["set-cookie"]
    assert "SameSite=Lax" in cookie and "Partitioned" not in cookie and "Secure" in cookie


def test_proxy_rejects_paths_outside_the_auth_api(client, upstream):
    for path in ("../../rest", "Sign-In", "a//b"):
        assert client.get(f"/api/auth/{path}").status_code == 404
    assert upstream.calls == []


def test_proxy_503_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(config, "NEON_AUTH_URL", "")
    assert client.post("/api/auth/sign-in/email", json={}).status_code == 503


def test_proxy_502_when_upstream_unreachable(client, upstream):
    def boom(request):
        raise httpx.ConnectError("down")

    upstream.reply = boom
    assert client.get("/api/auth/get-session").status_code == 502


def test_oauth_complete_exchanges_verifier_and_redirects(client, upstream):
    upstream.reply = lambda r: httpx.Response(200, json={"user": {"id": "u1"}},
                                              headers=[("set-cookie", f"{SESSION}; Path=/")])
    r = client.get("/api/auth/complete?next=http://localhost:5173/app&neon_auth_session_verifier=v1",
                   headers={"cookie": CHALLENGE})
    sent = upstream.calls[0]
    assert sent.url.path.endswith("/get-session") and sent.url.params["neon_auth_session_verifier"] == "v1"
    assert sent.headers["cookie"] == CHALLENGE
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/app"
    assert "tok123" in r.headers["set-cookie"]


def test_oauth_complete_without_verifier_just_redirects(client, upstream):
    r = client.get("/api/auth/complete?next=https://evil.com")
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/app" and upstream.calls == []


def test_oauth_complete_failure_goes_to_signin(client, upstream):
    upstream.reply = lambda r: httpx.Response(401, json=None)
    r = client.get("/api/auth/complete?neon_auth_session_verifier=v1", headers={"cookie": CHALLENGE})
    assert r.headers["location"] == "http://localhost:5173/signin?error=oauth"


def test_current_user_401_without_cookie(client, upstream):
    assert client.get("/me").status_code == 401 and upstream.calls == []


def test_current_user_401_when_upstream_session_is_null(client, upstream):
    upstream.reply = lambda r: httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
    assert client.get("/me", headers={"cookie": SESSION}).status_code == 401


def test_current_user_is_cached_and_sign_out_clears_it(client, upstream):
    assert client.get("/me", headers={"cookie": SESSION}).json()["id"] == "u1"
    assert client.get("/me", headers={"cookie": SESSION}).json()["id"] == "u1"
    assert len(upstream.calls) == 1  # second call served from the 60 s cache
    client.post("/api/auth/sign-out", json={}, headers={"cookie": SESSION})
    client.get("/me", headers={"cookie": SESSION})
    assert len(upstream.calls) == 3  # sign-out forwarded, then a fresh lookup
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth.py -q`
Expected: FAIL, `ImportError: cannot import name 'auth'`.

- [ ] **Step 3: Implement `receipts/auth.py`**

```python
"""Sign-in through Neon Managed Better Auth, proxied by FastAPI.

Mirrors Neon's own server SDK proxy (github.com/neondatabase/neon-js, packages/auth/src/server): the same
forwarded headers, the same cookie rewriting and the same OAuth session-verifier exchange, so the browser
only ever talks to this API and every auth cookie is first-party. Docs: https://neon.com/docs/auth/overview
"""
import hashlib
import re
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import config

COOKIE_PREFIX = "__Secure-neon-auth"
# Neon still accepts a legacy misspelled challenge cookie; recognise both.
CHALLENGE_COOKIES = (f"{COOKIE_PREFIX}.session_challenge", f"{COOKIE_PREFIX}.session_challange")
VERIFIER_PARAM = "neon_auth_session_verifier"
FORWARD_REQUEST_HEADERS = ("user-agent", "authorization", "referer", "content-type")
# content-encoding is not forwarded: httpx has already decoded the body we send on.
FORWARD_RESPONSE_HEADERS = ("content-type", "date", "set-auth-jwt", "set-auth-token", "x-neon-ret-request-id")
AUTH_PATH = re.compile(r"[a-z0-9-]+(/[a-z0-9-]+)*")
SESSION_TTL_S = 60

router = APIRouter(prefix="/api/auth")
_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def close() -> None:
    if _client is not None:
        await _client.aclose()


def neon_cookies(cookie_header: str) -> str:
    """Only Neon Auth cookies travel upstream."""
    pairs = (p.strip() for p in cookie_header.split(";"))
    return "; ".join(p for p in pairs if p.startswith(COOKIE_PREFIX) and "=" in p)


def rewrite_set_cookie(header: str, domain: str | None = None) -> str:
    """Make an upstream cookie first-party: no Partitioned, always Secure + HttpOnly, SameSite=Lax."""
    name_value, *attrs = (part.strip() for part in header.split(";"))
    drop = {"partitioned", "secure", "httponly", "samesite", "domain"}
    kept = [a for a in attrs if a and a.split("=", 1)[0].strip().lower() not in drop]
    return "; ".join([name_value, *kept, "HttpOnly", "Secure", "SameSite=Lax",
                      *([f"Domain={domain}"] if domain else [])])


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def safe_next(target: str | None) -> str:
    """Only send people back into our own frontend (no open redirect)."""
    base = config.FRONTEND_URL
    if target and (target == base or target.startswith(base + "/")):
        return target
    return f"{base}/app"


def _upstream_headers(request: Request) -> dict:
    headers = {name: request.headers[name] for name in FORWARD_REQUEST_HEADERS if name in request.headers}
    # Same order as the SDK: Origin header, then the Referer's origin, then this request's own origin.
    headers["origin"] = (request.headers.get("origin") or _origin(request.headers.get("referer", ""))
                         or _origin(str(request.url)))
    headers["cookie"] = neon_cookies(request.headers.get("cookie", ""))
    headers["x-neon-auth-middleware"] = "true"
    return headers


async def _call(method: str, path: str, *, query: str, headers: dict, body: bytes | None) -> httpx.Response:
    if not config.NEON_AUTH_URL:
        raise HTTPException(503, "Sign-in isn't configured on this server (set NEON_AUTH_URL).")
    url = f"{config.NEON_AUTH_URL}/{path}" + (f"?{query}" if query else "")
    try:
        return await http().request(method, url, headers=headers, content=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, "Couldn't reach the sign-in service. Try again in a moment.") from e


def _copy_cookies(upstream: httpx.Response, response: Response) -> Response:
    for cookie in upstream.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", rewrite_set_cookie(cookie, config.COOKIE_DOMAIN))
    return response


class SessionCache:
    """get-session results kept SESSION_TTL_S seconds, keyed by a hash of the Neon cookie string."""

    def __init__(self, ttl: float = SESSION_TTL_S):
        self.ttl = ttl
        self.items: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def _key(cookies: str) -> str:
        return hashlib.sha256(cookies.encode()).hexdigest()

    def get(self, cookies: str) -> dict | None:
        hit = self.items.get(self._key(cookies))
        return hit[1] if hit and hit[0] > time.monotonic() else None

    def put(self, cookies: str, user: dict) -> None:
        if len(self.items) > 10_000:  # ponytail: crude bound for one instance; use Redis when scaling out
            self.items.clear()
        self.items[self._key(cookies)] = (time.monotonic() + self.ttl, user)

    def drop(self, cookies: str) -> None:
        self.items.pop(self._key(cookies), None)


sessions = SessionCache()


async def current_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or 401."""
    cookies = neon_cookies(request.headers.get("cookie", ""))
    if not cookies:
        raise HTTPException(401, "Sign in to continue.")
    if (user := sessions.get(cookies)) is not None:
        return user
    upstream = await _call("GET", "get-session", query="", body=None, headers={
        "cookie": cookies, "origin": config.FRONTEND_URL, "x-neon-auth-middleware": "true"})
    body = upstream.json() if upstream.status_code == 200 and upstream.content else None
    user = (body or {}).get("user")
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    sessions.put(cookies, user)
    return user


@router.get("/complete")
async def complete(request: Request) -> Response:
    """OAuth return: trade Neon's session verifier + our challenge cookie for session cookies."""
    target = safe_next(request.query_params.get("next"))
    cookie_header = request.headers.get("cookie", "")
    has_challenge = any(f"{name}=" in cookie_header for name in CHALLENGE_COOKIES)
    if VERIFIER_PARAM not in request.query_params or not has_challenge:
        return RedirectResponse(target, status_code=302)
    upstream = await _call("GET", "get-session", query=request.url.query, headers=_upstream_headers(request), body=None)
    if upstream.status_code != 200:
        return RedirectResponse(f"{config.FRONTEND_URL}/signin?error=oauth", status_code=302)
    return _copy_cookies(upstream, RedirectResponse(target, status_code=302))


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> Response:
    if not AUTH_PATH.fullmatch(path):
        raise HTTPException(404, "Not found.")
    headers = _upstream_headers(request)
    upstream = await _call(request.method, path, query=request.url.query, headers=headers,
                           body=await request.body() or None)
    if path == "sign-out":
        sessions.drop(headers["cookie"])
    response = Response(content=upstream.content, status_code=upstream.status_code)
    for name in FORWARD_RESPONSE_HEADERS:
        if name in upstream.headers:
            response.headers[name] = upstream.headers[name]
    return _copy_cookies(upstream, response)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auth.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add receipts/auth.py tests/test_auth.py
git commit -m "feat: auth proxy to Neon Managed Better Auth (mirrors Neon's server SDK)"
```

---

### Task 4: API on Neon: ownership, limits, caching (+ serve on Windows)

**Files:**
- Modify (rewrite): `receipts-backend/receipts/server.py`
- Modify: `receipts-backend/receipts/__main__.py` (`serve`), `receipts-backend/.env.example`, `receipts-backend/README.md`
- Test (rewrite): `receipts-backend/tests/test_server.py`

**Interfaces:**
- Consumes: Task 2 store interface, `db.encode_cursor/decode_cursor`; Task 3 `auth.router`, `auth.current_user`, `auth.close`; `engine.check(inst, patch, emit)`, `engine.new_run_id(instance_id, label, taken)`, `engine.safe_run_id(run_id)`
- Produces: endpoints per the spec's API table; `app.state.runs` (the store); `server.LIVE: dict[str, LiveRun]`

- [ ] **Step 1: Write the failing tests** `tests/test_server.py` (replaces the old file)

```python
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from receipts import auth, config, db, server, swebench
from receipts.swebench import Instance

ROWS = {
    "psf__requests-1": {"instance_id": "psf__requests-1", "repo": "psf/requests", "difficulty": "<15 min fix",
                        "problem_statement": "\nGET sends Content-Length\nmore", "patch": "GOLD", "PASS_TO_PASS": "[]"},
    "django__django-1": {"instance_id": "django__django-1", "repo": "django/django", "difficulty": "<15 min fix",
                         "problem_statement": "x", "patch": "", "PASS_TO_PASS": "[]"},
}
USER = {"id": "u1", "email": "a@b.c", "name": "A", "image": None}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(swebench, "_dataset", lambda: ROWS)
    server._instances.cache_clear()
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    server.LIVE.clear()
    seen = {}

    async def fake_check(inst: Instance, patch, emit=None):
        seen["patch"] = patch
        emit("claim", {"kind": "fix", "claim": "c"})
        emit("fork", {"side": "base", "n": 1, "passed": False, "message": "assert 1 == 2"})
        emit("done", {})
        return {"instance_id": inst.instance_id, "verdict": "PROVEN", "reason": "r", "seconds": 1.0,
                "tokens": {"m": {"total_tokens": 7}}, "events": [{"type": "claim", "data": {}}]}

    monkeypatch.setattr(server.engine, "check", fake_check)
    with TestClient(server.app) as c:
        c.seen = seen
        c.store = server.app.state.runs
        yield c
    server.app.dependency_overrides.clear()


def signed_in(api):
    server.app.dependency_overrides[auth.current_user] = lambda: USER
    return api


def events(api, run_id):
    with api.stream("GET", f"/api/runs/{run_id}/events") as r:
        return [line.split(":", 1)[1].strip() for line in r.iter_lines() if line.startswith("event:")]


def test_health(api):
    assert api.get("/api/health").json() == {"ok": True}


def test_instances_are_pytest_only_and_cacheable(api):
    r = api.get("/api/instances")
    assert [i["id"] for i in r.json()] == ["psf__requests-1"] and r.json()[0]["title"] == "GET sends Content-Length"
    assert "max-age=3600" in r.headers["cache-control"]
    assert api.get("/api/instances", headers={"if-none-match": r.headers["etag"]}).status_code == 304


def test_starting_a_check_needs_sign_in(api):
    assert api.post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"}).status_code == 401
    assert api.get("/api/runs").status_code == 401 and api.get("/api/me").status_code == 401


def test_me(api):
    assert signed_in(api).get("/api/me").json() == {"id": "u1", "email": "a@b.c", "name": "A", "image": None}


def test_run_is_stored_for_its_owner_and_streams(api):
    run_id = signed_in(api).post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"}).json()["run_id"]
    assert events(api, run_id) == ["status", "claim", "fork", "done"]
    row = api.get(f"/api/runs/{run_id}").json()
    assert row["status"] == "done" and row["evidence"]["verdict"] == "PROVEN" and api.seen["patch"] == "GOLD"
    assert [r["id"] for r in api.get("/api/runs").json()["runs"]] == [run_id]


def test_receipt_is_public(api):
    run_id = signed_in(api).post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "none"}).json()["run_id"]
    events(api, run_id)
    server.app.dependency_overrides.clear()  # signed out now
    r = api.get(f"/api/runs/{run_id}")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert events(api, run_id)[-1] == "done"


def test_my_runs_are_only_mine_with_pages_and_etag(api):
    t0 = datetime(2026, 9, 29, tzinfo=timezone.utc)
    import asyncio
    for i in range(3):
        asyncio.run(api.store.create(f"r{i}", "u1", "psf__requests-1", "gold", t0 + timedelta(minutes=i)))
    asyncio.run(api.store.create("theirs", "u2", "psf__requests-1", "gold", t0))
    first = signed_in(api).get("/api/runs?limit=2")
    body = first.json()
    assert [r["id"] for r in body["runs"]] == ["r2", "r1"] and body["next_cursor"]
    rest = api.get(f"/api/runs?limit=2&cursor={body['next_cursor']}").json()
    assert [r["id"] for r in rest["runs"]] == ["r0"] and rest["next_cursor"] is None
    assert api.get("/api/runs?limit=2", headers={"if-none-match": first.headers["etag"]}).status_code == 304


def test_bad_cursor_is_400(api):
    assert signed_in(api).get("/api/runs?cursor=garbage").status_code == 400


def test_active_limit(api, monkeypatch):
    import asyncio
    monkeypatch.setattr(config, "MAX_ACTIVE_RUNS", 2)
    for i in range(2):
        asyncio.run(api.store.create(f"a{i}", "u1", "psf__requests-1", "gold"))
    r = signed_in(api).post("/api/runs", json={"instance_id": "psf__requests-1", "pr": "gold"})
    assert r.status_code == 429 and "running" in r.json()["detail"]


def test_daily_limit(api, monkeypatch):
    import asyncio
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


def test_unfinished_runs_fail_on_startup(monkeypatch, tmp_path):
    import asyncio
    store = db.MemoryRuns()
    asyncio.run(store.create("stuck", "u1", "psf__requests-1", "gold"))
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(server.db, "MemoryRuns", lambda: store)
    with TestClient(server.app) as c:
        assert c.get("/api/runs/stuck").json()["status"] == "error"


def test_cors_allows_only_the_frontend(api):
    ok = api.options("/api/runs", headers={"origin": config.FRONTEND_URL, "access-control-request-method": "POST",
                                           "access-control-request-headers": "content-type"})
    assert ok.headers.get("access-control-allow-origin") == config.FRONTEND_URL
    assert ok.headers.get("access-control-allow-credentials") == "true"
    bad = api.options("/api/runs", headers={"origin": "https://evil.com", "access-control-request-method": "POST"})
    assert bad.headers.get("access-control-allow-origin") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_server.py -q`
Expected: FAIL (no `/api/health`, no `app.state.runs`, no `LIVE`).

- [ ] **Step 3: Rewrite `receipts/server.py`**

```python
"""Receipts API: sign-in (proxied to Neon Auth), checks and receipts. Runs live in Neon Postgres.

python -m receipts serve   ->   http://127.0.0.1:8000   (the frontend is a separate repo)
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

from . import auth, config, db, engine, swebench

MAX_DIFF_BYTES = 200_000
MAX_CONCURRENT_CHECKS = 2  # whole server: Daytona quota + Token Factory credit


class RunRequest(BaseModel):
    instance_id: str
    pr: Literal["gold", "none", "diff"]
    diff: str | None = None


@dataclass
class LiveRun:
    """Progress of a run while it is active; once finished the stored evidence takes over."""

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
    warmup = asyncio.get_running_loop().run_in_executor(None, _instances)
    pool = await db.open_pool(config.DATABASE_URL) if config.DATABASE_URL else None
    # ponytail: without DATABASE_URL nothing persists (tests, quick local runs)
    app.state.runs = db.PgRuns(pool) if pool else db.MemoryRuns()
    await app.state.runs.fail_unfinished()
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
app.add_middleware(GZipMiddleware, minimum_size=1000)  # skips text/event-stream by default
app.add_middleware(CORSMiddleware, allow_origins=[config.FRONTEND_URL], allow_credentials=True,
                   allow_methods=["GET", "POST"], allow_headers=["content-type", "authorization"])


def runs_store(request: Request):
    return request.app.state.runs


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def cached_json(request: Request, payload, cache_control: str) -> Response:
    """JSON with an ETag; a matching If-None-Match gets an empty 304."""
    body = json.dumps(payload, separators=(",", ":"), default=_json_default).encode()
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
            evidence["run_id"] = live.id
            status = "done"
    except Exception as e:  # engine.check never raises by design; the database can
        live.publish("error", {"message": f"{type(e).__name__}: {e}"})
        evidence["events"] = live.events
    finally:
        try:
            await runs.finish(live.id, status, evidence)
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
    live = LIVE.get(run_id)
    evidence = row["evidence"] or {"instance_id": row["instance_id"], "started_at": row["started_at"],
                                   "events": live.events if live else []}
    evidence = {**evidence, "pr": evidence.get("pr") or row["pr"]}
    finished = row["status"] in ("done", "error")
    body = json.dumps({"status": row["status"], "evidence": evidence}, default=_json_default).encode()
    cache = "public, max-age=31536000, immutable" if finished else "no-store"
    return Response(body, media_type="application/json", headers={"Cache-Control": cache})


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
```

- [ ] **Step 4: Update `serve` in `receipts/__main__.py`**

Replace the `serve` branch body:
```python
    if a.cmd == "serve":
        import uvicorn

        # psycopg's async pool needs a selector event loop; uvicorn defaults to Proactor on Windows
        loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"
        return uvicorn.run("receipts.server:app", host="127.0.0.1", port=a.port, loop=loop)
```
and its help text to `"API server (the UI is the separate receipts-frontend repo)"`.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Update `.env.example` and `README.md`**

Append to `.env.example`:
```dotenv
# --- Neon Postgres (Neon Console > Connect). Pooled for the app, direct for migrations ---
DATABASE_URL=
DATABASE_URL_UNPOOLED=

# --- Neon Managed Better Auth (Neon Console > Auth > Configuration > Auth URL) ---
NEON_AUTH_URL=

# --- Where the frontend and this API live ---
FRONTEND_URL=http://localhost:5173
API_URL=http://localhost:8000
# COOKIE_DOMAIN=.example.com   # production: app.example.com + api.example.com

# --- Per-user limits ---
# MAX_ACTIVE_RUNS=2
# RUNS_PER_DAY=20
```
In `README.md`, replace the "Web UI" section with "API server" (run `python -m receipts migrate`, `python -m receipts import-runs`, `python -m receipts serve`; the UI lives in `receipts-frontend`) and add a "Sign-in and database" section listing the Neon console steps from the spec's Environment section.

- [ ] **Step 7: Commit**

```bash
git add receipts/server.py receipts/__main__.py tests/test_server.py .env.example README.md
git commit -m "feat: API on Neon with sign-in, ownership, limits, caching; serve on a selector loop"
```

---

### Task 5: Frontend: landing, sign-in/sign-up, protected app

**Files (in `receipts-frontend/`):**
- Modify: `package.json` (add `@neondatabase/auth@0.5.0-beta`), `src/api.ts`, `src/App.tsx`, `src/styles.css`, `src/components/RecentRuns.tsx`, `src/components/ReceiptCard.tsx`, `src/components/EvidenceDetails.tsx`, `src/pages/NewCheck.tsx`
- Create: `src/auth.ts`, `src/session.tsx`, `src/pages/Landing.tsx`, `src/pages/AuthPage.tsx`, `src/example-receipt.json`, `src/nav.ts`, `src/nav.test.ts`, `src/example.test.ts`, `public/_redirects`, `vercel.json`, `.env.example`, `.gitignore`, `README.md`

**Interfaces:**
- Consumes: backend endpoints (Task 4), `/api/auth/*` proxy + `/api/auth/complete` (Task 3)
- Produces: routes `/`, `/signin`, `/signup`, `/app` (guarded), `/runs/:runId`

- [ ] **Step 1: Install the Neon auth client**

```bash
cd /c/Users/Bhuvansai/OneDrive/Documents/Nebiusxnemo/receipts-frontend
npm install @neondatabase/auth@0.5.0-beta
```

- [ ] **Step 2: Write the failing tests**

`src/nav.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { safeNext } from "./nav";

describe("safeNext", () => {
  it("keeps in-app paths", () => {
    expect(safeNext("/runs/abc?x=1")).toBe("/runs/abc?x=1");
  });
  it("falls back to /app for anything that could leave the site", () => {
    for (const bad of [null, "", "https://evil.com", "//evil.com", "/\\evil.com", "app"]) expect(safeNext(bad)).toBe("/app");
  });
});
```
`src/example.test.ts`:
```ts
import { expect, it } from "vitest";
import type { Evidence } from "./api";
import example from "./example-receipt.json";
import { fromEvidence } from "./receipt";

it("the landing page's bundled receipt is a real PROVEN run", () => {
  const r = fromEvidence(example as unknown as Evidence);
  expect(r.verdict?.verdict).toBe("PROVEN");
  expect(r.base).toHaveLength(3);
  expect(r.pr).toHaveLength(3);
});
```

- [ ] **Step 3: Run to verify they fail**

Run: `npx vitest run`
Expected: FAIL (missing `./nav`, missing `./example-receipt.json`).

- [ ] **Step 4: Generate the bundled example receipt** (trimmed copy of a real run)

```bash
python - <<'EOF'
import json, pathlib
src = pathlib.Path("../receipts-backend/runs/pydata__xarray-4629-gold-20260928-201414.json")
ev = json.loads(src.read_text(encoding="utf-8"))
for group in ("base_with_test", "pr_with_test", "base_suite", "pr_suite"):
    for run in ev["forks"].get(group) or []:
        run["output_tail"] = ""
ev["writer"] = {"attempts": ev["writer"]["attempts"], "reason": ev["writer"]["reason"], "test_code": None}
ev["run_id"], ev["pr"] = src.stem, "gold"
pathlib.Path("src/example-receipt.json").write_text(json.dumps(ev, indent=1), encoding="utf-8")
EOF
```

- [ ] **Step 5: Implement `src/nav.ts`, `src/auth.ts`, `src/session.tsx`**

`src/nav.ts`:
```ts
/** Where to go after signing in: only paths inside this app (no open redirect). */
export function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/app";
}
```
`src/auth.ts`:
```ts
import { createAuthClient } from "@neondatabase/auth";
import { API_URL } from "./api";

// Every auth call goes through our FastAPI proxy (receipts-backend receipts/auth.py), never to Neon directly.
export const authClient = createAuthClient(`${API_URL}/api/auth`);
```
`src/session.tsx`:
```tsx
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { authClient } from "./auth";

export interface User {
  id: string;
  email: string;
  name?: string | null;
  image?: string | null;
}

interface Session {
  user: User | null;
  loading: boolean;
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await authClient.getSession();
      setUser((data?.user as User | undefined) ?? null);
    } catch {
      setUser(null); // API or auth unreachable: behave as signed out
    } finally {
      setLoading(false);
    }
  }, []);

  const signOut = useCallback(async () => {
    await authClient.signOut();
    setUser(null);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return <SessionContext.Provider value={{ user, loading, refresh, signOut }}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside <SessionProvider>");
  return session;
}
```

- [ ] **Step 6: Point the API client at the backend** (`src/api.ts`)

At the top:
```ts
export const API_URL = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const url = (path: string) => `${API_URL}${path}`;
```
Replace `RunSummary` with the backend's row shape:
```ts
export interface RunSummary {
  id: string;
  instance_id: string;
  pr: string;
  status: RunStatus;
  verdict: Verdict | null;
  reason: string | null;
  seconds: number | null;
  tokens: number | null;
  started_at: string;
  finished_at: string | null;
}
```
Replace the `api` object and `subscribe`'s URL:
```ts
const get = <T,>(path: string) => fetch(url(path), { credentials: "include" }).then(json<T>);

export const api = {
  instances: () => get<InstanceSummary[]>("/api/instances"),
  instance: (id: string) => get<InstanceDetail>(`/api/instances/${enc(id)}`),
  myRuns: (cursor?: string) =>
    get<{ runs: RunSummary[]; next_cursor: string | null }>(`/api/runs${cursor ? `?cursor=${enc(cursor)}` : ""}`),
  run: (id: string) => get<RunResponse>(`/api/runs/${enc(id)}`),
  start: (body: { instance_id: string; pr: PrKind; diff?: string }) =>
    fetch(url("/api/runs"), {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<{ run_id: string }>),
};
```
and in `subscribe`: `new EventSource(url(`/api/runs/${enc(runId)}/events`))`.

- [ ] **Step 7: Pages**

`src/pages/Landing.tsx`:
```tsx
import { useEffect } from "react";
import { Link } from "react-router-dom";
import type { Evidence } from "../api";
import { ReceiptCard } from "../components/ReceiptCard";
import example from "../example-receipt.json";
import { fromEvidence } from "../receipt";
import { useSession } from "../session";

const EXAMPLE = example as unknown as Evidence;

const STEPS = [
  { title: "Write the missing test", text: "From the issue alone. The test writer never sees the pull request." },
  { title: "Run it before and after", text: "Three times on the original code and three times with the change, in isolated sandboxes." },
  { title: "Get the receipt", text: "Proven, refuted or unproven, with the test and every run's output attached." },
];

export function LandingPage() {
  const { user } = useSession();
  useEffect(() => {
    document.title = "Receipts · Check whether a pull request does what it claims";
  }, []);

  return (
    <>
      <section className="landing" aria-labelledby="hero-title">
        <div>
          <h1 id="hero-title" className="hero-title">
            Every pull request makes a claim. Get the receipt.
          </h1>
          <p className="hero-lead">
            Receipts writes the test a pull request is missing, runs it before and after the change, and shows you
            the evidence. Review what is proven first.
          </p>
          <div className="hero-ctas">
            <Link to={user ? "/app" : "/signup"} className="btn btn--primary">
              {user ? "Check a pull request" : "Get started"}
            </Link>
            <Link to={`/runs/${EXAMPLE.run_id}`} className="btn btn--quiet">
              See an example receipt
            </Link>
          </div>
        </div>
        <ReceiptCard
          receipt={fromEvidence(EXAMPLE)}
          evidence={EXAMPLE}
          live={false}
          queued={false}
          elapsed={null}
          onReveal={() => undefined}
          actions={false}
        />
      </section>

      <section aria-labelledby="how-title" className="how">
        <h2 id="how-title" className="section-title">
          How it works
        </h2>
        <ol className="steps">
          {STEPS.map((step, i) => (
            <li key={step.title}>
              <span className="step-n">{String(i + 1).padStart(2, "0")}</span>
              <h3 className="step-title">{step.title}</h3>
              <p className="step-text">{step.text}</p>
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
```
`src/pages/AuthPage.tsx`:
```tsx
import { useEffect, useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { API_URL } from "../api";
import { authClient } from "../auth";
import { safeNext } from "../nav";
import { useSession } from "../session";

const GITHUB_MARK =
  "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z";

export function AuthPage({ mode }: { mode: "signin" | "signup" }) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const { user, refresh } = useSession();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>(
    params.get("error") === "oauth" ? "GitHub sign-in didn't finish. Try again." : undefined,
  );
  const signup = mode === "signup";

  useEffect(() => {
    document.title = `${signup ? "Create your account" : "Sign in"} · Receipts`;
  }, [signup]);

  if (user) return <Navigate to={next} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    const result = signup
      ? await authClient.signUp.email({ name: name.trim() || email.split("@")[0], email, password })
      : await authClient.signIn.email({ email, password });
    if (result.error) {
      setError(result.error.message ?? "That didn't work. Check your details and try again.");
      setBusy(false);
      return;
    }
    await refresh();
    navigate(next, { replace: true });
  }

  async function github() {
    setBusy(true);
    const back = `${window.location.origin}${next}`;
    await authClient.signIn.social({
      provider: "github",
      callbackURL: `${API_URL}/api/auth/complete?next=${encodeURIComponent(back)}`,
      errorCallbackURL: `${window.location.origin}/signin?error=oauth`,
    });
  }

  return (
    <div className="auth">
      <h1 className="page-title">{signup ? "Create your account" : "Sign in"}</h1>
      <p className="lead">{signup ? "Check pull requests and keep every receipt." : "Welcome back."}</p>
      <div className="auth-card">
        <button type="button" className="btn btn--github btn--block" onClick={github} disabled={busy}>
          <svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true">
            <path fill="currentColor" d={GITHUB_MARK} />
          </svg>
          Continue with GitHub
        </button>
        <p className="divider">or with email</p>
        <form className="form form--tight" onSubmit={submit}>
          {signup && (
            <div className="field">
              <label className="label" htmlFor="name">
                Name
              </label>
              <input id="name" className="input" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
          )}
          <div className="field">
            <label className="label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              className="input"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete={signup ? "new-password" : "current-password"}
              required
              minLength={8}
              aria-describedby="password-hint"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <p id="password-hint" className="hint">
              At least 8 characters.
            </p>
          </div>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button type="submit" className="btn btn--primary btn--block" disabled={busy}>
            {busy ? "One moment…" : signup ? "Create account" : "Sign in"}
          </button>
        </form>
      </div>
      <p className="hint auth-switch">
        {signup ? "Already have an account? " : "New to Receipts? "}
        <Link to={`${signup ? "/signin" : "/signup"}${next !== "/app" ? `?next=${encodeURIComponent(next)}` : ""}`}>
          {signup ? "Sign in" : "Create an account"}
        </Link>
      </p>
    </div>
  );
}
```

- [ ] **Step 8: Routing, guard and nav** in `src/App.tsx`

Wrap the router in `<SessionProvider>`; routes:
```tsx
<Route path="/" element={<LandingPage />} />
<Route path="/signin" element={<AuthPage mode="signin" />} />
<Route path="/signup" element={<AuthPage mode="signup" />} />
<Route path="/app" element={<RequireAuth><NewCheckPage /></RequireAuth>} />
<Route path="/runs/:runId" element={<RunPage />} />
<Route path="*" element={<NotFound />} />
```
Add in `App.tsx`:
```tsx
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useSession();
  const location = useLocation();
  if (loading) return <p className="hint" aria-busy="true">Checking your session…</p>;
  if (!user) return <Navigate to={`/signin?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  return <>{children}</>;
}
```
Replace the `<nav>` content in `Shell`:
```tsx
const { user, signOut } = useSession();
const navigate = useNavigate();
...
<nav aria-label="Primary" className="topnav">
  {user ? (
    <>
      {pathname !== "/app" && (
        <Link to="/app" className="btn btn--primary btn--sm">New check</Link>
      )}
      <span className="nav-user" title={user.email}>{user.email}</span>
      <button type="button" className="link-btn" onClick={() => signOut().then(() => navigate("/"))}>Sign out</button>
    </>
  ) : (
    <>
      {pathname !== "/signin" && <Link to="/signin" className="link-btn">Sign in</Link>}
      {pathname !== "/signup" && <Link to="/signup" className="btn btn--primary btn--sm">Get started</Link>}
    </>
  )}
</nav>
```

- [ ] **Step 9: Existing components**

- `ReceiptCard.tsx`: add prop `actions?: boolean` (default `true`); render the `receipt__actions` block only when `actions`.
- `EvidenceDetails.tsx`: raw link `href={`${API_URL}/api/runs/${encodeURIComponent(runId)}`}` (import `API_URL`).
- `RecentRuns.tsx`: title "Your receipts"; load `api.myRuns()`; poll every 5 s only while some run has status `queued` or `running`, and reload on `window` focus; rows link to `/runs/${run.id}`; empty state "No receipts yet. Your checks will show up here."
- `NewCheck.tsx`: unchanged flow (it now lives at `/app`).

- [ ] **Step 10: Styles** (append to `src/styles.css`)

```css
/* ---------- landing ---------- */
.landing {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 440px);
  gap: 64px;
  align-items: center;
}
.hero-title {
  font-size: clamp(38px, 5vw, 58px);
  font-weight: 700;
  letter-spacing: -0.04em;
  line-height: 1.04;
}
.hero-lead {
  margin-top: 20px;
  font-size: 19px;
  color: var(--ink-2);
  max-width: 46ch;
}
.hero-ctas {
  margin-top: 30px;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.how {
  margin-top: 112px;
}
.steps {
  margin: 24px 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 32px;
}
.steps li {
  border-top: 1px solid var(--hair);
  padding-top: 18px;
}
.step-n {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--ink-3);
}
.step-title {
  margin-top: 8px;
  font-size: 18px;
  font-weight: 650;
}
.step-text {
  margin-top: 6px;
  color: var(--ink-2);
}
@media (max-width: 960px) {
  .landing,
  .steps {
    grid-template-columns: 1fr;
  }
  .how {
    margin-top: 72px;
  }
}

/* ---------- sign in / sign up ---------- */
.auth {
  max-width: 420px;
  margin: 8px auto 0;
}
.auth-card {
  margin-top: 28px;
  padding: 28px;
  border: 1px solid var(--hair);
  border-radius: var(--radius-lg);
  display: grid;
  gap: 20px;
}
.form--tight {
  margin-top: 0;
  gap: 18px;
}
.divider {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--ink-3);
}
.divider::before,
.divider::after {
  content: "";
  flex: 1;
  border-top: 1px solid var(--hair);
}
.btn--block {
  width: 100%;
}
.btn--github {
  background: #fff;
  border-color: var(--line);
  color: var(--ink);
}
.btn--github:hover:not(:disabled) {
  background: var(--surface);
}
.auth-switch {
  margin-top: 18px;
  text-align: center;
}
.nav-user {
  font-size: 14px;
  color: var(--ink-2);
  max-width: 22ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 560px) {
  .nav-user {
    display: none;
  }
}
```

- [ ] **Step 11: Hosting files, env, readme**

`public/_redirects`: `/*  /index.html  200`
`vercel.json`: `{"rewrites": [{"source": "/(.*)", "destination": "/"}]}`
`.env.example`: `VITE_API_URL=http://localhost:8000`
`.gitignore`: `node_modules/`, `dist/`, `.env`
`README.md`: setup (`npm install`, `.env`, `npm run dev`), deploy (static host, SPA fallback, `VITE_API_URL`, same parent domain as the API).

- [ ] **Step 12: Tests and build**

Run: `npx vitest run && npm run build`
Expected: all tests pass (15), build succeeds.

- [ ] **Step 13: Commit**

```bash
git add -A
git commit -m "feat: landing page, sign-in/sign-up through the API, protected app, own receipts"
```

---

### Task 6: Live verification with Neon (needs the owner's credentials)

**Files:** none (configuration in `receipts-backend/.env` and `receipts-frontend/.env`)

- [ ] **Step 1:** With `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `NEON_AUTH_URL` set: `python -m receipts migrate` → expect `applied: 001_init`; again → `nothing new`.
- [ ] **Step 2:** `python -m receipts import-runs` → expect `imported 30 runs` (or the current count).
- [ ] **Step 3:** Start both preview servers (`receipts-api`, `receipts-web`); open `http://localhost:5173`.
- [ ] **Step 4:** Landing renders the example receipt; "See an example receipt" opens the imported run signed out.
- [ ] **Step 5:** Sign up with email/password → lands on `/app`; `SELECT email FROM neon_auth.user` shows the user.
- [ ] **Step 6:** Start a check → receipt prints live → appears in "Your receipts" (row in `runs` with the user's id).
- [ ] **Step 7:** Sign out → `/app` redirects to `/signin?next=/app`; the receipt link still opens.
- [ ] **Step 8:** If a GitHub OAuth app is configured in Neon: "Continue with GitHub" → back on `/app` signed in.
