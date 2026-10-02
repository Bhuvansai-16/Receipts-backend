"""Runs storage: Neon Postgres (psycopg 3 async pool, plain SQL) and an in-memory twin for tests."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import config

SUMMARY_KEYS = ("id", "user_id", "instance_id", "pr", "status", "verdict", "reason", "seconds", "tokens",
                "started_at", "finished_at", "repo", "pr_number", "head_sha", "check_run_id")
SUMMARY = ", ".join(SUMMARY_KEYS)
MIGRATIONS = config.ROOT / "migrations"
ACTIVE = ("queued", "running")
RESTARTED = "backend restarted before the check finished"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def jsonb_safe(value):
    """Postgres text and jsonb can't hold NUL characters (tool output sometimes has them): drop them."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {jsonb_safe(k): jsonb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonb_safe(v) for v in value]
    return value


def result_columns(evidence: dict) -> dict:
    tokens = sum(int(u.get("total_tokens") or 0) for u in (evidence.get("tokens") or {}).values())
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


def pr_of(run_id: str, evidence: dict) -> str:
    """The run's PR kind: evidence["pr"] when valid, else the label in <instance>-<label>-<YYYYmmdd-HHMMSS>."""
    if evidence.get("pr") in ("gold", "none", "diff"):
        return evidence["pr"]
    label = run_id[len(evidence["instance_id"]) + 1:-16]
    return label if label in ("gold", "none") else "diff"  # other labels were diff file names


def _imported(run_id: str, evidence: dict, user_id: str | None = None) -> dict:
    try:
        started = datetime.fromisoformat(evidence["started_at"])
    except (KeyError, TypeError, ValueError):
        started = _now()
    return {**dict.fromkeys(SUMMARY_KEYS), **result_columns(evidence), "id": run_id, "user_id": user_id,
            "instance_id": evidence["instance_id"], "pr": pr_of(run_id, evidence), "status": "done",
            "started_at": started, "finished_at": started + timedelta(seconds=evidence.get("seconds") or 0),
            "evidence": evidence}


class MemoryRuns:
    """Same interface as PgRuns, in a dict. Used by tests and when DATABASE_URL is not set."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.installations: dict[int, dict] = {}
        self.installation_users: list[tuple[int, str]] = []  # in link order: the first is the owner
        self.repo_settings: dict[int, dict] = {}
        self.blind_tests: dict[str, dict] = {}

    async def create(self, run_id, user_id, instance_id, pr, started_at=None, source=None):
        self.rows[run_id] = {**dict.fromkeys(SUMMARY_KEYS), "id": run_id, "user_id": user_id,
                             "instance_id": instance_id, "pr": pr, "status": "queued",
                             "started_at": started_at or _now(), "evidence": None, **(source or {})}

    async def set_check_run(self, run_id, check_run_id):
        self.rows[run_id]["check_run_id"] = check_run_id

    async def has_run_for_head(self, repo, pr_number, head_sha):
        return any((r["repo"], r["pr_number"], r["head_sha"]) == (repo, pr_number, head_sha) for r in self.rows.values())

    async def latest_for_prs(self, repo, numbers):
        latest = {}
        for r in sorted(self.rows.values(), key=lambda r: r["started_at"]):
            if r["repo"] == repo and r["pr_number"] in numbers:
                latest[r["pr_number"]] = {k: r[k] for k in ("id", "status", "verdict")}
        return latest

    async def sync_installations(self, user_id, installations):
        for i in installations:
            self.installations[i["id"]] = {k: i[k] for k in ("id", "account_login", "account_type")}
        keep = {i["id"] for i in installations}
        self.installation_users = [(iid, uid) for iid, uid in self.installation_users if uid != user_id or iid in keep]
        linked = {iid for iid, uid in self.installation_users if uid == user_id}
        self.installation_users += [(iid, user_id) for iid in sorted(keep - linked)]

    async def user_installations(self, user_id):
        ids = {iid for iid, uid in self.installation_users if uid == user_id}
        return sorted((dict(self.installations[i]) for i in ids), key=lambda i: i["account_login"])

    async def installation_owner(self, installation_id):
        return next((uid for iid, uid in self.installation_users if iid == installation_id), None)

    async def delete_installation(self, installation_id):
        self.installations.pop(installation_id, None)
        self.installation_users = [(iid, uid) for iid, uid in self.installation_users if iid != installation_id]
        self.repo_settings = {k: v for k, v in self.repo_settings.items() if v["installation_id"] != installation_id}

    async def set_auto_check(self, repo_id, installation_id, full_name, enabled):
        self.repo_settings[repo_id] = {"installation_id": installation_id, "full_name": full_name, "auto_check": enabled}

    async def auto_checks(self, repo_ids):
        return {r for r in repo_ids if self.repo_settings.get(r, {}).get("auto_check")}

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

    async def global_recent(self, since):
        return sum(r["started_at"] >= since for r in self.rows.values())

    async def fail_unfinished(self):
        stale = [r for r in self.rows.values() if r["status"] in ACTIVE]
        for r in stale:
            r.update(status="error", reason=RESTARTED, finished_at=_now())
        return len(stale)

    async def import_run(self, run_id, evidence, user_id=None):
        self.rows.setdefault(run_id, _imported(run_id, evidence, user_id))

    async def get_blind_test(self, key):
        hit = self.blind_tests.get(key)
        return dict(hit) if hit else None

    async def save_blind_test(self, key, repo, run_id, test_code):
        self.blind_tests.setdefault(key, {"repo": repo, "run_id": run_id, "test_code": test_code})

    async def forget_blind_test(self, key):
        self.blind_tests.pop(key, None)


class PgRuns:
    """Runs in Neon. Each method is one short statement on a pooled connection."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def _query(self, sql, params, fetch):
        for retry in (False, True):
            try:
                async with self.pool.connection() as conn:
                    cur = await conn.execute(sql, params)
                    return await fetch(cur)
            except psycopg.OperationalError:
                if retry:
                    raise
                # Neon suspends an idle compute and drops its connections: discard the dead ones, try once more.
                # Safe to repeat: a statement on a dropped connection never ran (autocommit, one statement each).
                await self.pool.check()

    async def _exec(self, sql, params=()) -> int:
        async def rowcount(cur):
            return cur.rowcount

        return await self._query(sql, params, rowcount)

    async def _one(self, sql, params=()):
        return await self._query(sql, params, lambda cur: cur.fetchone())

    async def _all(self, sql, params=()):
        return await self._query(sql, params, lambda cur: cur.fetchall())

    async def create(self, run_id, user_id, instance_id, pr, started_at=None, source=None):
        src = source or {}
        await self._exec("INSERT INTO runs (id, user_id, instance_id, pr, status, started_at, repo, pr_number, "
                         "head_sha) VALUES (%s, %s, %s, %s, 'queued', coalesce(%s::timestamptz, now()), %s, %s, %s)",
                         (run_id, user_id, instance_id, pr, started_at, src.get("repo"), src.get("pr_number"),
                          src.get("head_sha")))

    async def set_check_run(self, run_id, check_run_id):
        await self._exec("UPDATE runs SET check_run_id = %s WHERE id = %s", (check_run_id, run_id))

    async def has_run_for_head(self, repo, pr_number, head_sha):
        row = await self._one("SELECT 1 AS hit FROM runs WHERE repo = %s AND pr_number = %s AND head_sha = %s "
                              "LIMIT 1", (repo, pr_number, head_sha))
        return row is not None

    async def latest_for_prs(self, repo, numbers):
        rows = await self._all("SELECT DISTINCT ON (pr_number) pr_number, id, status, verdict FROM runs "
                               "WHERE repo = %s AND pr_number = ANY(%s) ORDER BY pr_number, started_at DESC",
                               (repo, list(numbers)))
        return {r["pr_number"]: {k: r[k] for k in ("id", "status", "verdict")} for r in rows}

    async def sync_installations(self, user_id, installations):
        for i in installations:
            await self._exec("INSERT INTO github_installations (id, account_login, account_type) VALUES (%s, %s, %s) "
                             "ON CONFLICT (id) DO UPDATE SET account_login = EXCLUDED.account_login, "
                             "account_type = EXCLUDED.account_type", (i["id"], i["account_login"], i["account_type"]))
        ids = [i["id"] for i in installations]
        await self._exec("DELETE FROM github_installation_users WHERE user_id = %s "
                         "AND NOT (installation_id = ANY(%s))", (user_id, ids))
        for iid in ids:
            await self._exec("INSERT INTO github_installation_users (installation_id, user_id) VALUES (%s, %s) "
                             "ON CONFLICT DO NOTHING", (iid, user_id))

    async def user_installations(self, user_id):
        return await self._all("SELECT i.id, i.account_login, i.account_type FROM github_installations i "
                               "JOIN github_installation_users u ON u.installation_id = i.id "
                               "WHERE u.user_id = %s ORDER BY i.account_login", (user_id,))

    async def installation_owner(self, installation_id):
        row = await self._one("SELECT user_id FROM github_installation_users WHERE installation_id = %s "
                              "ORDER BY created_at LIMIT 1", (installation_id,))
        return row["user_id"] if row else None

    async def delete_installation(self, installation_id):
        await self._exec("DELETE FROM github_installations WHERE id = %s", (installation_id,))

    async def set_auto_check(self, repo_id, installation_id, full_name, enabled):
        await self._exec("INSERT INTO github_repo_settings (repo_id, installation_id, full_name, auto_check) "
                         "VALUES (%s, %s, %s, %s) ON CONFLICT (repo_id) DO UPDATE SET "
                         "auto_check = EXCLUDED.auto_check, installation_id = EXCLUDED.installation_id, "
                         "full_name = EXCLUDED.full_name", (repo_id, installation_id, full_name, enabled))

    async def auto_checks(self, repo_ids):
        rows = await self._all("SELECT repo_id FROM github_repo_settings WHERE auto_check AND repo_id = ANY(%s)",
                               (list(repo_ids),))
        return {r["repo_id"] for r in rows}

    async def mark_running(self, run_id):
        await self._exec("UPDATE runs SET status = 'running' WHERE id = %s", (run_id,))

    async def finish(self, run_id, status, evidence):
        evidence = jsonb_safe(evidence)
        c = result_columns(evidence)
        await self._exec("UPDATE runs SET status = %s, verdict = %s, reason = %s, seconds = %s, tokens = %s, "
                         "evidence = %s, finished_at = now() WHERE id = %s",
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

    async def global_recent(self, since):
        return (await self._one("SELECT count(*) AS n FROM runs WHERE started_at >= %s", (since,)))["n"]

    async def fail_unfinished(self):
        # ponytail: assumes one API instance; with several, only fail runs this instance owned
        return await self._exec("UPDATE runs SET status = 'error', reason = %s, finished_at = now() "
                                "WHERE status IN ('queued', 'running')", (RESTARTED,))

    async def import_run(self, run_id, evidence, user_id=None):
        r = _imported(run_id, jsonb_safe(evidence), user_id)
        await self._exec(f"INSERT INTO runs ({SUMMARY}, evidence) VALUES ({', '.join(['%s'] * (len(SUMMARY_KEYS) + 1))}) "
                         "ON CONFLICT (id) DO NOTHING", (*(r[k] for k in SUMMARY_KEYS), Jsonb(r["evidence"])))

    async def get_blind_test(self, key):
        return await self._one("SELECT run_id, test_code FROM blind_tests WHERE key = %s", (key,))

    async def save_blind_test(self, key, repo, run_id, test_code):
        await self._exec("INSERT INTO blind_tests (key, repo, run_id, test_code) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (key) DO NOTHING", (key, repo, run_id, jsonb_safe(test_code)))

    async def forget_blind_test(self, key):
        await self._exec("DELETE FROM blind_tests WHERE key = %s", (key,))


async def open_pool(url: str) -> AsyncConnectionPool:
    # autocommit: every call is one statement, so no BEGIN/COMMIT round trips (each costs ~0.35 s to us-east-2)
    pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False,
                               kwargs={"row_factory": dict_row, "autocommit": True})
    await pool.open(wait=True, timeout=30)  # a suspended compute takes a few seconds to wake
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
    """Load finished runs saved by the CLI (runs/*.json) as public example receipts (no owner).

    Eval output (runs/eval/, and any stray eval-*.json) is for comparing pipeline versions, never public."""
    count = 0
    for path in sorted(runs_dir.glob("*.json")):
        if path.name.startswith("eval-"):
            continue
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:  # a half-written file must not stop the API from starting
            continue
        if isinstance(evidence, dict) and evidence.get("instance_id") and evidence.get("verdict"):
            await store.import_run(path.stem, evidence)
            count += 1
    return count


async def import_eval(store, eval_dir: Path) -> int:
    """Publish an evaluation's receipts (runs/eval/<experiment>/*.json) under owner "eval", which no gallery lists:
    each opens by its link, like any receipt."""
    count = 0
    for path in sorted(eval_dir.glob("*.json")):
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(evidence, dict) and evidence.get("instance_id") and evidence.get("verdict"):
            await store.import_run(path.stem, evidence, user_id="eval")
            count += 1
    return count
