# Agent and backend optimizations: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the time and model credit a check costs (blind-test reuse, a leaner writer with skills as middleware,
overlapped setup, environments kept across restarts) and trim the deploy image, with before and after numbers.

**Architecture:** Reuse lives in `engine.py` with a small store on `db.py`'s run stores (new table
`blind_tests`). The writer composes Deepagents' `FilesystemMiddleware`, `SkillsMiddleware` and
`PatchToolCallsMiddleware` with LangChain's `create_agent` instead of `create_deep_agent`; skills are files in
`receipts/skills/`, served read-only from the server through a `CompositeBackend`. Environments are tagged in
Nebius Sandboxes. The frontend only learns one new field, `reused_from`.

**Tech Stack:** Python 3.12 (global install, no venv), FastAPI, psycopg 3, Deepagents 0.7.15, LangChain 1.4,
Contree SDK 0.3.6, pytest; React 19 + Vitest in `receipts-frontend`.

**Spec:** `docs/superpowers/specs/2026-10-01-agent-and-backend-optimizations-design.md`

## Global Constraints

- Verdict rules in `receipts/verdict.py` keep their behaviour; new code may only add pure helpers there.
- Asymmetry rule: anything uncertain is UNPROVEN, never REFUTED. No new path can produce a verdict by itself.
- The writer never sees the pull request. A reused test was written blind for the same issue and base.
- Every cache path degrades to today's behaviour on any error (log, then carry on).
- No venv: run everything with the global `python`; settings come from `.env` (never print its values).
- Copy rules for user-facing text: plain words, no em dashes, no marketing words.
- Backend tests: `python -m pytest -q` (230 pass today). Frontend: `npx vitest run` and `npm run build` in `receipts-frontend`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Pushing `main` redeploys
  Render (backend) or Vercel (frontend): push only in Task 8.
- Live runs (Token Factory credit, sandbox time) and the Neon migration need the user's yes first.

## Review Focus

1. Prompt injection through an issue tells the writer to write into `/skills/` or read `/skills/../.env`: the
   write is refused and the path is rejected (Task 4 tests both).
2. The migration isn't applied yet when new code runs: lookups and saves fail, and checks behave exactly as
   today (Task 2, broken-store test).
3. A stored test no longer reproduces on its base (environment drift, sandbox error): it is forgotten and a new
   test is written (Task 2, stale-test test).
4. Two checks of the same issue start together: both write a test, the first save wins, nothing errors
   (Task 1, first-save-wins test).
5. The skills folder is missing or a skill's front matter is broken in the deployed image: the writer still
   runs (Task 4, skill-files test; Docker copies `receipts/` whole).

---

### Task 0: Credit report and the "before" numbers

**Files:**
- Create: `scripts/usage_report.py`
- Test: `tests/test_usage_report.py`

**Interfaces:**
- Produces: `usage(ev: dict) -> dict` with keys `run, verdict, seconds, attempts, commands, reused, models,
  tokens, usd`; CLI `python scripts/usage_report.py [--api URL] REF...` printing one JSON line per check and a
  totals line.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_report.py
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("usage_report", Path(__file__).parent.parent / "scripts" / "usage_report.py")
usage_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage_report)

EV = {"run_id": "r1", "verdict": "PROVEN", "seconds": 50.0,
      "tokens": {"nvidia/nemotron-3-super-120b-a12b": {"input_tokens": 40_000, "output_tokens": 1_000, "total_tokens": 41_000},
                 "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": {"input_tokens": 4_000, "output_tokens": 800, "total_tokens": 4_800}},
      "writer": {"attempts": 1, "tool_log": [{}, {}, {}]}}


def test_usage_sums_tokens_by_model_and_prices_them(monkeypatch):
    monkeypatch.setattr(usage_report, "PRICES", {"nemotron-3-super-120b-a12b": (1.0, 2.0),
                                                 "NVIDIA-Nemotron-3-Nano-30B-A3B": (0.5, 0.5)})
    row = usage_report.usage(EV)
    assert row["tokens"] == 45_800 and row["commands"] == 3 and row["reused"] is False
    assert row["models"]["nemotron-3-super-120b-a12b"] == (40_000, 1_000)
    assert row["usd"] == round((40_000 * 1.0 + 1_000 * 2.0 + 4_800 * 0.5) / 1e6, 4)


def test_an_unpriced_model_reports_tokens_only(monkeypatch):
    monkeypatch.setattr(usage_report, "PRICES", {})
    assert usage_report.usage(EV)["usd"] is None
```

- [ ] **Step 2: Run it and see it fail**

Run: `python -m pytest tests/test_usage_report.py -q`
Expected: FAIL (`FileNotFoundError` for `scripts/usage_report.py`)

- [ ] **Step 3: Write the script**

```python
# scripts/usage_report.py
"""Tokens, time and estimated model spend per check, from stored receipts: compare pipeline versions.

    python scripts/usage_report.py runs/eval/eval-final-pr16.json ...
    python scripts/usage_report.py --api https://receipts-backend-wnjy.onrender.com RUN_ID ...

PRICES: Nebius Token Factory list prices, USD per million tokens (input, output). A model missing from the
table is reported in tokens only.
"""
import argparse
import json
import urllib.request
from pathlib import Path

PRICES: dict[str, tuple[float, float]] = {}


def load(ref: str, api: str | None) -> dict:
    if api:
        with urllib.request.urlopen(f"{api.rstrip('/')}/api/runs/{ref}", timeout=30) as r:
            return json.load(r)["evidence"]
    return json.loads(Path(ref).read_text(encoding="utf-8"))


def usage(ev: dict) -> dict:
    models, usd, priced = {}, 0.0, True
    for name, u in (ev.get("tokens") or {}).items():
        short = name.split("/")[-1]
        i, o = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        models[short] = (i, o)
        if short in PRICES:
            usd += (i * PRICES[short][0] + o * PRICES[short][1]) / 1e6
        else:
            priced = False
    w = ev.get("writer") or {}
    return {"run": ev.get("run_id") or ev.get("instance_id"), "verdict": ev.get("verdict"),
            "seconds": ev.get("seconds"), "attempts": w.get("attempts"), "commands": len(w.get("tool_log") or []),
            "reused": bool(w.get("reused_from")), "models": models,
            "tokens": sum(i + o for i, o in models.values()), "usd": round(usd, 4) if priced else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", help="read stored runs from this Receipts API instead of files")
    parser.add_argument("refs", nargs="+", help="receipt files, or run ids with --api")
    args = parser.parse_args()
    rows = [usage(load(ref, args.api)) for ref in args.refs]
    for row in rows:
        print(json.dumps(row))
    total = {"checks": len(rows), "tokens": sum(r["tokens"] for r in rows),
             "seconds": round(sum(r["seconds"] or 0 for r in rows), 1)}
    if all(r["usd"] is not None for r in rows):
        total["usd"] = round(sum(r["usd"] for r in rows), 4)
    total["per_check"] = {k: round(v / len(rows), 3) for k, v in total.items() if k != "checks"}
    print(json.dumps(total))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_usage_report.py -q`
Expected: 2 passed

- [ ] **Step 5: Fill in prices**

Look up Token Factory's list prices for `NVIDIA-Nemotron-3-Nano-30B-A3B`, `nemotron-3-super-120b-a12b` and
`Nemotron-3-Ultra-550b-a55b` (Nebius Token Factory pricing page) and set `PRICES` with the source date in a
comment. If a price isn't published, leave that model out (tokens only).

- [ ] **Step 6: Record the "before" numbers (no credit spent)**

Run: `python -c "import json,urllib.request;print([(r['id'],r['instance_id'],r['pr'],r['started_at']) for r in json.load(urllib.request.urlopen('https://receipts-backend-wnjy.onrender.com/api/demo'))['gallery']])"`
Pick the six Phase 1 demo runs (1 October, about 08:00 UTC: xarray-4629 gold/none/diff, requests-1142
gold/none/diff), then run `python scripts/usage_report.py --api https://receipts-backend-wnjy.onrender.com <six ids>`
and keep the output for Task 8. The writer's per-turn overhead today was measured on 1 October with the
same stub model Task 4 uses, against `create_deep_agent`: system prompt 2,515 characters, tool definitions
12,342 characters (10 tools: ls, read_file, write_file, edit_file, delete, glob, grep, execute, task,
submit_test).

- [ ] **Step 7: Commit**

```bash
git add scripts/usage_report.py tests/test_usage_report.py
git commit -m "Credit report per check from stored receipts, for before and after comparisons"
```

### Task 1: A store for blind tests

**Files:**
- Create: `migrations/003_blind_tests.sql`
- Modify: `receipts/db.py` (`MemoryRuns.__init__` and new methods on both stores)
- Modify: `receipts/verdict.py` (new pure helper `reproduces`)
- Test: `tests/test_runs_store.py`, `tests/test_verdict.py`

**Interfaces:**
- Produces: `await store.get_blind_test(key) -> {"run_id": str, "test_code": str, ...} | None`,
  `await store.save_blind_test(key, repo, run_id, test_code) -> None` (first save wins),
  `await store.forget_blind_test(key) -> None`; `verdict.reproduces(base_runs: list[PytestRun]) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runs_store.py (append; the store fixture runs it on memory, and on Neon when TEST_DATABASE_URL is set)
def test_blind_tests_keep_the_first_save_and_can_be_forgotten(store, run):
    async def go():
        assert await store.get_blind_test("k") is None
        await store.save_blind_test("k", "psf/requests", "r1", "def test_a(): assert 0")
        await store.save_blind_test("k", "psf/requests", "r2", "def test_b(): assert 0")  # a racing check
        first = await store.get_blind_test("k")
        await store.forget_blind_test("k")
        return first, await store.get_blind_test("k")
    first, gone = run(go())
    assert (first["run_id"], first["test_code"]) == ("r1", "def test_a(): assert 0") and gone is None
```

In `_neon_store`, change `TRUNCATE runs` to `TRUNCATE runs, blind_tests`.

```python
# tests/test_verdict.py (append)
def test_reproduces_needs_every_base_run_to_fail_the_same_way():
    fail = lambda msg: PytestRun({"t::a": TestResult("failed", "AssertionError", msg)})  # noqa: E731
    assert verdict.reproduces([fail("1 != 2")] * 3)
    assert not verdict.reproduces([fail("1 != 2"), fail("1 != 2"), PytestRun()])          # a run that never ran
    assert not verdict.reproduces([fail("1 != 2"), fail("3 != 4"), fail("1 != 2")])       # flaky
    assert not verdict.reproduces([])
```

(Use the module's existing imports for `PytestRun`, `TestResult` and `verdict`; add them if missing.)

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_runs_store.py tests/test_verdict.py -q -k "blind or reproduces"`
Expected: FAIL (`AttributeError: 'MemoryRuns' object has no attribute 'get_blind_test'`, no `reproduces`)

- [ ] **Step 3: Implement**

```sql
-- migrations/003_blind_tests.sql
-- Blind tests worth reusing: a test depends only on the issue and the unpatched code, never on the PR.
CREATE TABLE blind_tests (
  key        text PRIMARY KEY,               -- sha256 of repo, base and issue text
  repo       text NOT NULL,
  run_id     text NOT NULL,                  -- the check whose writer wrote it
  test_code  text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

```python
# receipts/db.py, MemoryRuns.__init__: add
        self.blind_tests: dict[str, dict] = {}

# receipts/db.py, MemoryRuns: add
    async def get_blind_test(self, key):
        hit = self.blind_tests.get(key)
        return dict(hit) if hit else None

    async def save_blind_test(self, key, repo, run_id, test_code):
        self.blind_tests.setdefault(key, {"repo": repo, "run_id": run_id, "test_code": test_code})

    async def forget_blind_test(self, key):
        self.blind_tests.pop(key, None)

# receipts/db.py, PgRuns: add
    async def get_blind_test(self, key):
        return await self._one("SELECT run_id, test_code FROM blind_tests WHERE key = %s", (key,))

    async def save_blind_test(self, key, repo, run_id, test_code):
        await self._exec("INSERT INTO blind_tests (key, repo, run_id, test_code) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (key) DO NOTHING", (key, repo, run_id, jsonb_safe(test_code)))

    async def forget_blind_test(self, key):
        await self._exec("DELETE FROM blind_tests WHERE key = %s", (key,))
```

```python
# receipts/verdict.py, after repro_check
def reproduces(base_runs: list[PytestRun]) -> bool:
    """Every base run fails with an assertion, all the same way: a blind test worth reusing."""
    return (bool(base_runs) and all(repro_check(r)[0] for r in base_runs)
            and len({frozenset(_failing(r)) for r in base_runs}) == 1)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_runs_store.py tests/test_verdict.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add migrations/003_blind_tests.sql receipts/db.py receipts/verdict.py tests/test_runs_store.py tests/test_verdict.py
git commit -m "A store for blind tests worth reusing"
```

### Task 2: Reuse the blind test in the engine

**Files:**
- Modify: `receipts/engine.py` (imports, `blind_test_key`, `check`, `_pipeline`, new `_reuse`, `_stored_test`,
  `_forget`, `_remember`)
- Modify: `receipts/writer.py` (`WriterResult.reused_from`)
- Modify: `receipts/checks.py:266` (pass the store and run id)
- Test: `tests/test_engine.py`; fakes in `tests/test_demo.py:34`, `tests/test_github.py:51`,
  `tests/test_server.py:31,125,239` gain `**kw`; new test in `tests/test_server.py`

**Interfaces:**
- Consumes: Task 1's store methods and `verdict.reproduces`.
- Produces: `engine.blind_test_key(inst) -> str`;
  `await engine.check(inst, patch, emit=None, *, tests=None, run_id=None) -> dict`; evidence
  `ev["writer"]["reused_from"]` (only when reused); events `test_reused {"from": run_id}` and
  `test_accepted {"attempts": 0, "reused_from": run_id}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_engine.py`; add `TEST_PATH` to the sandbox
  import and `from receipts import db` to the imports)

```python
def test_checks_of_the_same_issue_and_base_share_a_key():
    same = Instance("x__y-1", "psf/requests", "issue", "another patch", [])
    assert engine.blind_test_key(INST) == engine.blind_test_key(same)
    assert engine.blind_test_key(INST) != engine.blind_test_key(Instance("x__y-2", "psf/requests", "issue", PATCH, []))
    assert engine.blind_test_key(INST) != engine.blind_test_key(Instance("x__y-1", "psf/requests", "other", PATCH, []))


def test_a_pull_request_check_keys_on_its_base_commit():
    from receipts.targets import RepoTarget
    a, b = (RepoTarget(f"o/r#{n}", "o/r", "issue", "sha1", [], None) for n in (1, 2))
    assert engine.blind_test_key(a) == engine.blind_test_key(b)
    assert engine.blind_test_key(a) != engine.blind_test_key(RepoTarget("o/r#3", "o/r", "issue", "sha2", [], None))


def _store_with(code="def test_bug(): assert 1 == 2", run_id="r1"):
    store = db.MemoryRuns()
    asyncio.run(store.save_blind_test(engine.blind_test_key(INST), INST.repo, run_id, code))
    return store


def test_a_stored_test_is_reused_without_research_or_writer(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("a reused test needs no research and no writer")

    monkeypatch.setattr(engine, "write_test", never)
    monkeypatch.setattr(engine.research, "research", never)
    seen = []
    ev = asyncio.run(engine.check(INST, PATCH, emit=lambda t, d: seen.append(t), tests=_store_with(), run_id="r2"))
    assert ev["verdict"] == "PROVEN", ev["reason"]
    assert ev["writer"]["reused_from"] == "r1" and ev["writer"]["attempts"] == 0
    assert seen.index("test_reused") < seen.index("test_accepted") and "research" not in seen


class StaleImg(Img):
    """The stored test passes on this base: it no longer reproduces the bug here."""

    async def run(self, shell=None, files=None, **kw):
        if b"stale" in (files or {}).get(TEST_PATH, b""):
            res = {"receipts_test.py::test_bug": PASSED}
            return SimpleNamespace(stdout=MARKER + json.dumps(res), stderr="", exit_code=0)
        return await super().run(shell, files, **kw)


def test_a_stale_stored_test_is_replaced_by_a_new_one(monkeypatch):
    async def base_image(iid):
        return StaleImg("base")

    monkeypatch.setattr(swebench, "base_image", base_image)
    store = _store_with(code="def test_bug(): assert 'stale'")
    ev = asyncio.run(engine.check(INST, PATCH, tests=store, run_id="r2"))
    assert ev["verdict"] == "PROVEN" and "reused_from" not in ev["writer"]
    assert store.blind_tests[engine.blind_test_key(INST)]["run_id"] == "r2"


def test_an_accepted_test_is_kept_for_the_next_check():
    store = db.MemoryRuns()
    asyncio.run(engine.check(INST, PATCH, tests=store, run_id="r1"))
    kept = store.blind_tests[engine.blind_test_key(INST)]
    assert kept["run_id"] == "r1" and kept["test_code"] == "def test_bug(): assert 1 == 2"


def _doubting(monkeypatch):
    async def judge(*a):
        return engine.Judgement(faithful=False, reason="asks more than the issue")

    monkeypatch.setattr(engine, "judge", judge)


def test_a_doubted_test_is_never_kept(monkeypatch):
    _doubting(monkeypatch)
    store = db.MemoryRuns()
    ev = asyncio.run(engine.check(INST, None, tests=store, run_id="r1"))
    assert ev["verdict"] == "UNPROVEN" and store.blind_tests == {}


def test_a_reused_test_that_gets_doubted_is_forgotten(monkeypatch):
    _doubting(monkeypatch)
    store = _store_with()
    asyncio.run(engine.check(INST, None, tests=store, run_id="r2"))
    assert store.blind_tests == {}


def test_a_broken_store_never_fails_a_check():
    class Broken:
        async def get_blind_test(self, key):
            raise OSError("database down")

        async def save_blind_test(self, *a):
            raise OSError("database down")

        async def forget_blind_test(self, key):
            raise OSError("database down")

    ev = asyncio.run(engine.check(INST, PATCH, tests=Broken(), run_id="r1"))
    assert ev["verdict"] == "PROVEN", ev["reason"]
```

```python
# tests/test_server.py: the fixture's fake_check becomes `async def fake_check(inst: Instance, patch, emit=None, **kw):`
# and records `seen["kw"] = kw` on its first line; crash (line 125) and slow_check (line 239) gain `**kw`. Append:
def test_a_check_gets_the_run_store_for_reusing_blind_tests(api):
    run_id = start(signed_in(api))
    for _ in range(100):
        if "kw" in api.seen:
            break
        time.sleep(0.02)
    assert api.seen["kw"]["tests"] is api.store and api.seen["kw"]["run_id"] == run_id
```

(`tests/test_demo.py:34` and `tests/test_github.py:51`: add `**kw` to `fake_check`. Import `time` in
`tests/test_server.py` if it isn't imported.)

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_engine.py tests/test_server.py -q`
Expected: FAIL (`AttributeError: module 'receipts.engine' has no attribute 'blind_test_key'`, `TypeError:
check() got an unexpected keyword argument 'tests'`)

- [ ] **Step 3: Implement**

```python
# receipts/writer.py, WriterResult: add the field
    reused_from: str = ""  # run id of the check whose blind test this one reuses
```

```python
# receipts/engine.py, imports
import asyncio
import hashlib
import json
import logging
import re
...
from .sandbox import TEST_ARGS, TEST_PATH, apply_patch, run_pytest, suite_files
from .verdict import (MIXED, PytestRun, Verdict, fix_verdict, partial_fix, repro_check, reproduces, restrict,
                      suite_candidates)
from .writer import WriterResult, retry_history, write_test

log = logging.getLogger("uvicorn.error")
```

```python
# receipts/engine.py, after changed_files
def blind_test_key(inst) -> str:
    """Checks that can share a blind test: same repository, base and issue text. The test never saw a PR."""
    base = getattr(inst, "base_sha", None) or inst.instance_id  # a SWE-bench image fixes its commit
    return hashlib.sha256(f"{inst.repo}\n{base}\n{inst.problem_statement}".encode()).hexdigest()


async def _stored_test(tests, key: str) -> dict | None:
    if tests is None:
        return None
    try:
        return await tests.get_blind_test(key)
    except Exception as e:  # a cache can make a check faster, never fail it
        log.warning("blind test lookup failed: %s", e)
        return None


async def _forget(tests, key: str) -> None:
    try:
        await tests.forget_blind_test(key)
    except Exception as e:
        log.warning("forgetting a blind test failed: %s", e)


async def _reuse(stored: dict, base, tests, key: str, say) -> WriterResult | None:
    """The stored test, if it still reproduces the bug on this base. Otherwise it is forgotten (None)."""
    run = await run_pytest(base, TEST_ARGS, {TEST_PATH: stored["test_code"].encode()})
    if not repro_check(run)[0]:  # a changed environment or a sandbox error: write a fresh test
        await _forget(tests, key)
        return None
    say("test_reused", {"from": stored["run_id"]})
    return WriterResult(test_code=stored["test_code"], base_run=run, reason="reused", reused_from=stored["run_id"])


async def _remember(tests, key: str, repo: str, run_id: str | None, w, reproduced: bool, opinion) -> None:
    """Keep a written test that reproduced on every base run; forget one a second opinion doubted."""
    if tests is None or w.test_code is None:
        return
    if opinion and opinion.get("faithful") is False:
        await _forget(tests, key)
    elif reproduced and run_id and not getattr(w, "reused_from", ""):
        try:
            await tests.save_blind_test(key, repo, run_id, w.test_code)
        except Exception as e:
            log.warning("keeping a blind test failed: %s", e)
```

`check` gains the two keyword arguments and passes them on (docstring line: "tests: where reusable blind tests
are kept (the run store); None, as in the CLI and evaluations, always writes a new one."):

```python
async def check(inst: Instance, patch: str | None, emit=None, *, tests=None, run_id: str | None = None) -> dict:
    ...
            v, reason = await _pipeline(inst, patch, ev, say, tests, run_id)
```

`_pipeline` becomes (the writer path and the forks are unchanged apart from the lines shown):

```python
async def _pipeline(inst, patch, ev, say, tests=None, run_id=None) -> tuple[Verdict, str]:
    # The environment doesn't depend on the claim, so it builds while the claim is classified (~10 s saved).
    # ponytail: a PR with nothing to check pays for a few seconds of an abandoned build.
    env = asyncio.ensure_future(inst.base_image())
    key = blind_test_key(inst)
    try:
        stored = await _stored_test(tests, key)
        claim = await classify(inst.problem_statement, patch or "")
    except BaseException:
        env.cancel()
        raise
    ev["claim"] = claim.model_dump()
    say("claim", ev["claim"])
    if claim.kind != "fix":
        env.cancel()
        return Verdict.NO_CHECKABLE_CLAIM, f"classified as '{claim.kind}': nothing to check"

    brief_task = None if stored else asyncio.ensure_future(research.research(inst.repo, inst.problem_statement))
    base = await env
    say("env_ready")
    w = await _reuse(stored, base, tests, key, say) if stored else None
    if w is None:
        brief = await (brief_task or research.research(inst.repo, inst.problem_statement))
        ev["research"] = {"queries": brief.queries, "sources": brief.sources, "notes": brief.notes,
                          "errors": brief.errors}
        say("research", {"sources": len(brief.sources)})
        w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer())
        # (the existing one-retry block, unchanged)
    ev["writer"] = {"attempts": w.attempts, "reason": w.reason,
                    "test_code": w.test_code, "scope_check": getattr(w, "scope", ""), "tool_log": w.log,
                    "submissions": getattr(w, "submissions", [])}
    reused = getattr(w, "reused_from", "")
    if reused:
        ev["writer"]["reused_from"] = reused
    # (the two existing UNPROVEN returns for a missing test, unchanged)
    say("test_accepted", {"attempts": w.attempts, **({"reused_from": reused} if reused else {})})
    # (the forks, suites and fix_verdict, unchanged)
    v, reason = fix_verdict(base_runs, pr_runs, base_suites, pr_suites)
    if v is Verdict.REFUTED:
        j = await judge(inst.problem_statement, w.test_code, base_runs[0].output)
        ev["second_opinion"] = j.model_dump()
        say("second_opinion", ev["second_opinion"])
        if not j.faithful:
            v, reason = Verdict.UNPROVEN, f"second opinion doubts the test: {j.reason}"
    # an explanation for the reader, never a verdict change; asked only when the runs show a partial fix, since
    # any other mix (a new failure, a sandbox outage) may be the test's doing (sympy #15 live)
    elif reason == MIXED and partial_fix(base_runs, pr_runs):
        try:
            j = await judge_mixed(inst.problem_statement, w.test_code, pr_runs[0].output)
            ev["second_opinion"] = {**j.model_dump(), "about": "mixed"}
            say("second_opinion", ev["second_opinion"])
        except Exception:  # only an explanation: without it the page uses neutral words
            pass
    await _remember(tests, key, inst.repo, run_id, w, reproduces(base_runs), ev.get("second_opinion"))
    return v, reason
```

```python
# receipts/checks.py:266
            evidence = await engine.check(target, patch, emit=lambda t, d: t != "done" and live.publish(t, d),
                                          tests=runs, run_id=live.id)
```

- [ ] **Step 4: Run the whole backend suite**

Run: `python -m pytest -q`
Expected: all pass (230 + the new ones)

- [ ] **Step 5: Commit**

```bash
git add receipts/engine.py receipts/writer.py receipts/checks.py tests/test_engine.py tests/test_server.py tests/test_demo.py tests/test_github.py
git commit -m "Reuse the blind test for the same issue and base; keep only tests nobody doubted"
```

### Task 3: Show a reused test on the receipt (frontend)

**Files (in `receipts-frontend`):**
- Modify: `src/api.ts:148-155`, `src/receipt.ts`, `src/run/progress.ts:29-37`, `src/components/ReceiptCard.tsx`
  (blind test line), `src/components/EvidenceDetails.tsx` (blind test disclosure)
- Test: `src/receipt.test.ts`, `src/run/progress.test.ts`

**Interfaces:**
- Consumes: Task 2's `test_reused {"from"}` event and `writer.reused_from`.
- Produces: `Receipt.reusedFrom?: string`.

- [ ] **Step 1: Write the failing tests**

```ts
// src/receipt.test.ts (inside describe("fromEvents") or a new describe)
it("marks a reused blind test, live and stored", () => {
  const live = fromEvents([
    e("claim", { kind: "fix", claim: "c" }),
    e("env_ready"),
    e("test_reused", { from: "x-gold-1" }),
    e("test_accepted", { attempts: 0, reused_from: "x-gold-1" }),
  ]);
  expect(live.reusedFrom).toBe("x-gold-1");
  expect(live.testAttempts).toBe(0);
  expect(blindTestNote(live)).toBe("written from the issue alone in an earlier check");
  const stored = fromEvidence({
    instance_id: "x",
    writer: { attempts: 0, reason: "reused", test_code: "def test_a(): assert 0", reused_from: "x-gold-1" },
  } as Evidence);
  expect(stored.reusedFrom).toBe("x-gold-1");
});
```

```ts
// src/run/progress.test.ts (import fromEvents from "../receipt" if the file doesn't already)
it("says when the blind test was reused", () => {
  const r = fromEvents([
    { type: "claim", data: { kind: "fix", claim: "c" } },
    { type: "env_ready", data: {} },
    { type: "test_reused", data: { from: "x" } },
    { type: "test_accepted", data: { attempts: 0 } },
  ]);
  expect(progressSteps(r, true, false).steps.find((s) => s.id === "writer")?.detail).toBe("reused from an earlier check");
});
```

- [ ] **Step 2: Run them and see them fail**

Run: `npx vitest run src/receipt.test.ts src/run/progress.test.ts`
Expected: FAIL (`reusedFrom` undefined)

- [ ] **Step 3: Implement**

```ts
// src/api.ts, Evidence.writer: after test_code
    /** Run id of the earlier check whose blind test this check reused. */
    reused_from?: string;
```

```ts
// src/receipt.ts, Receipt: after testAttempts
  /** Set when the check reused the blind test an earlier check wrote for the same issue: that run's id. */
  reusedFrom?: string;
// fromEvents: before the test_accepted branch
    else if (type === "test_reused") r.reusedFrom = String(d.from ?? "");
// derive: after the testAttempts line
  if (ev.writer?.reused_from) r.reusedFrom = ev.writer.reused_from;
// blindTestNote
export function blindTestNote(r: Receipt): string {
  if (r.reusedFrom) return "written from the issue alone in an earlier check";
  return r.writerRetry ? "written from the issue alone, after one automatic retry" : "written from the issue alone";
}
```

```ts
// src/run/progress.ts, the writer step's detail
      detail: wrote
        ? r.reusedFrom
          ? "reused from an earlier check"
          : `${r.testAttempts} attempt${r.testAttempts === 1 ? "" : "s"}`
        : r.writerRetry
```

```tsx
// src/components/ReceiptCard.tsx, the Blind test line's value
              wroteTest ? (
                r.reusedFrom ? "reused" : `${r.testAttempts} attempt${r.testAttempts === 1 ? "" : "s"}`
              ) : live ? (
```

```tsx
// src/components/EvidenceDetails.tsx: import { Link } from "react-router-dom";
              <span className="disclosure__meta">
                {w.reused_from
                  ? "written from the issue alone in an earlier check"
                  : `written from the issue alone, ${w.attempts} attempt${w.attempts === 1 ? "" : "s"}`}
              </span>
            </summary>
            <div className="disclosure__body">
              {w.reused_from && (
                <p>
                  An earlier check of the same issue wrote this test.{" "}
                  <Link to={`/runs/${encodeURIComponent(w.reused_from)}`}>See how it was written</Link>
                </p>
              )}
              <CodeBlock filename="receipts_test.py" code={w.test_code.trim()} />
```

- [ ] **Step 4: Run the frontend checks**

Run: `npx vitest run && npm run build`
Expected: all tests pass, build succeeds

- [ ] **Step 5: Commit (frontend repo)**

```bash
git add src/api.ts src/receipt.ts src/run/progress.ts src/components/ReceiptCard.tsx src/components/EvidenceDetails.tsx src/receipt.test.ts src/run/progress.test.ts
git commit -m "Say when a check reused an earlier check's blind test"
```

### Task 4: A leaner writer with skills as middleware

**Files:**
- Modify: `receipts/writer.py` (imports, `PROMPT`, constants, `SkillsFolder`, `build_agent`,
  `ExplorationBudget`, `WriterResult.skills_read`, `write_test`)
- Modify: `receipts/engine.py` (`ev["writer"]["skills_read"]`)
- Create: `receipts/skills/{exception-bugs,expected-values,sympy,arrays,requests,plotting}/SKILL.md`
- Test: `tests/test_writer.py` (rename every `monkeypatch.setattr(writer, "create_deep_agent", ...)` to
  `"build_agent"`; update `test_prompt_builds_expected_values_from_the_issues_code`; new tests)

**Interfaces:**
- Produces: `writer.build_agent(*, model, tools, system_prompt, backend, middleware)` (same keywords the tests
  already pass); `writer.SKILLS_DIR`, `writer.SKILLS_ROOT = "/skills/"`, `writer.WRITER_TOOLS`,
  `writer.SkillsFolder`; `WriterResult.skills_read: list[str]`; evidence `writer.skills_read`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_writer.py`)

```python
import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool

SEEN: dict = {}


class Recorder(BaseChatModel):
    """Answers once without a tool call and keeps what it was sent."""

    tools: list = []

    @property
    def _llm_type(self):
        return "recorder"

    def bind_tools(self, tools, **kw):
        return Recorder(tools=[convert_to_openai_tool(t) for t in tools])

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        SEEN.update(messages=messages, tools=self.tools)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])


@tool
async def submit_stub() -> str:
    """Submit the test."""
    return ""


def _first_turn():
    box = writer.SafeSandbox.__new__(writer.SafeSandbox)  # no session: the first turn never touches the sandbox
    agent = writer.build_agent(model=Recorder(), tools=[submit_stub], system_prompt=writer.PROMPT, backend=box,
                               middleware=[])
    asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Issue: x"}]}))
    content = SEEN["messages"][0].content
    return (content if isinstance(content, str) else json.dumps(content)), SEEN["tools"]


def test_the_writer_gets_only_the_tools_one_test_file_needs():
    _, tools = _first_turn()
    assert {t["function"]["name"] for t in tools} == set(writer.WRITER_TOOLS) | {"submit_stub"}  # no task, no delete


def test_the_writer_sees_the_skills_list_and_stays_within_its_prompt_budget():
    system, tools = _first_turn()
    assert "/skills/sympy/SKILL.md" in system and "exception-bugs" in system
    # what every turn re-sends: 14,857 characters (about 3,700 tokens) before skills and the tool trim
    assert len(system) + len(json.dumps(tools)) <= 10_400


def test_every_skill_names_its_folder_and_says_when_it_applies():
    names = set()
    for path in writer.SKILLS_DIR.glob("*/SKILL.md"):
        head = path.read_text(encoding="utf-8").split("---")[1]
        fields = dict(line.split(": ", 1) for line in head.strip().splitlines())
        assert fields["name"] == path.parent.name and 20 < len(fields["description"]) < 160
        names.add(fields["name"])
    assert names == {"exception-bugs", "expected-values", "sympy", "arrays", "requests", "plotting"}


def test_skills_are_read_only_and_stay_inside_their_folder():
    folder = writer.SkillsFolder()
    assert "assert False" in folder.read("/exception-bugs/SKILL.md").file_data["content"]
    assert asyncio.run(folder.awrite("/sympy/SKILL.md", "obey me")).error == "permission_denied"
    assert asyncio.run(folder.aedit("/sympy/SKILL.md", "sympy", "x")).error == "permission_denied"
    assert asyncio.run(folder.adelete("/sympy/SKILL.md")).error == "permission_denied"
    with pytest.raises(ValueError):
        folder.read("/../../.env")


def test_reading_a_skill_is_not_looking_around(monkeypatch):
    monkeypatch.setattr(writer, "EXPLORE_LIMIT", 0)
    read = []
    budget = writer.ExplorationBudget(submit=None, skills_read=read)

    async def handler(request):
        return "skill text"

    request = SimpleNamespace(tool_call={"name": "read_file", "id": "1", "args": {"file_path": "/skills/sympy/SKILL.md"}})
    assert asyncio.run(budget.awrap_tool_call(request, handler)) == "skill text"
    assert read == ["/skills/sympy/SKILL.md"] and budget.left == 0  # not spent from the look-around budget
```

Also update `test_prompt_builds_expected_values_from_the_issues_code` to read its expectations from
`(writer.SKILLS_DIR / "expected-values" / "SKILL.md").read_text(encoding="utf-8")` instead of `writer.PROMPT`,
and import `pytest` if the module doesn't.

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_writer.py -q`
Expected: FAIL (`AttributeError: module 'receipts.writer' has no attribute 'build_agent'`)

- [ ] **Step 3: Write the six skills**

```markdown
<!-- receipts/skills/exception-bugs/SKILL.md -->
---
name: exception-bugs
description: Read before writing the test when the bug is an exception (the issue says a call raises or crashes).
---
# When the bug is an exception

The test must fail with an AssertionError on the current code. Call the code inside try/except, catch the
exception the issue names, and turn it into an assertion:

    def test_fit_accepts_lists():
        try:
            result = fit([1, 2, 3])
        except TypeError as e:
            assert False, f"raised {e!r}"
        assert result == [2, 4, 6]  # only if the issue states the correct result

- Catch only the exception type the issue names; anything else should still show up as an error.
- Don't use pytest.raises for the bug itself: it passes on the buggy code.
```

```markdown
<!-- receipts/skills/expected-values/SKILL.md -->
---
name: expected-values
description: Read before writing the test when the issue writes the expected result as code (a call, a literal).
---
# Expected results written as code

Build the expected value in the test from the same code the issue writes, and compare objects with ==:

    expected = Mul(-1, Add(x, 2, evaluate=False), evaluate=False)  # copied from the issue
    assert parse_expr("-(x + 2)", evaluate=False) == expected

Never retype what you think str(), repr() or a printer shows for it. Printers can leave out what the code says
(sympy's srepr never prints evaluate=False), and a retyped string can fail even after a correct fix. Compare
printed output only when the issue itself only shows printed output, and then use the printer the issue used.
```

```markdown
<!-- receipts/skills/sympy/SKILL.md -->
---
name: sympy
description: Read for sympy issues (expressions, evaluate=False, assumptions, solve, simplify or printing).
---
# sympy

- Create symbols with the assumptions the issue uses, e.g. Symbol("x", positive=True); a plain Symbol("x") has
  no sign assumptions.
- Keep values exact: Rational(1, 3), sqrt(2), pi. Use .evalf() and a tolerance only when the issue itself gives
  decimals.
- For unevaluated expressions, build the expected object with evaluate=False exactly as the issue writes it and
  compare objects (see the expected-values skill); srepr never shows evaluate=False.
- solve() returns a list of solutions (a list of dicts with dict=True). When order doesn't matter, compare sets.
- Keep inputs small; simplify() on large expressions is slow.
```

```markdown
<!-- receipts/skills/arrays/SKILL.md -->
---
name: arrays
description: Read when the issue's result is a numpy array or a pandas or xarray object.
---
# numpy, pandas and xarray results

- These helpers raise AssertionError, so they count as assertion failures:
  numpy.testing.assert_array_equal and assert_allclose, pandas.testing.assert_frame_equal and
  assert_series_equal, xarray.testing.assert_identical and assert_equal.
- `assert a == b` on arrays is ambiguous (it compares element by element); use a helper, `.all()`, or compare
  `.tolist()`.
- xarray's assert_identical also compares names and attrs; assert_equal ignores them. Use the one that matches
  what the issue says.
- When the bug is that an object is shared instead of copied (attrs, data), test it the way the issue does:
  change the result and check the input didn't change, or check `result.attrs is not source.attrs`.
- Use small inline data; no files.
```

```markdown
<!-- receipts/skills/requests/SKILL.md -->
---
name: requests
description: Read for requests issues (headers, bodies, URLs, sessions or prepared requests).
---
# requests, without sending anything

- Tests must not make network requests. Build and prepare the request instead:

      req = requests.Request("GET", "http://example.com").prepare()
      assert "Content-Length" not in req.headers

- Session behaviour without sending: s = requests.Session(); p = s.prepare_request(requests.Request(...)).
- To test response handling, build one by hand: r = requests.models.Response(); r.status_code = 200;
  r._content = b"..."; r.headers["Content-Type"] = "application/json".
- Header names are case-insensitive: req.headers.get("content-length") works.
```

```markdown
<!-- receipts/skills/plotting/SKILL.md -->
---
name: plotting
description: Read for matplotlib or seaborn issues (figures, axes, artists, colors or layout).
---
# matplotlib and seaborn

- Pick the non-interactive backend before pyplot is imported: import matplotlib; matplotlib.use("Agg").
- Never call plt.show(); check the objects instead: ax.get_xlim(), line.get_color(), fig.get_size_inches(),
  ax.get_legend().
- Compare colors with matplotlib.colors.to_rgba(...) rather than by name.
- Draw image output to an in-memory io.BytesIO, not a file, and finish with plt.close("all").
```

(Each file starts with the `---` front matter; the HTML comment line above shows only the path and is not part
of the file.)

- [ ] **Step 4: Implement the writer changes**

```python
# receipts/writer.py, imports: drop `from deepagents import create_deep_agent`; add
from pathlib import Path

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import (PERMISSION_DENIED, DeleteResult, EditResult, ExecuteResponse,
                                          FileUploadResponse, WriteResult)
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents import create_agent
```

```python
# receipts/writer.py, after UNCHANGED_LIMIT
SKILLS_DIR = Path(__file__).parent / "skills"
SKILLS_ROOT = "/skills/"
# Deepagents' file tools minus `task` (sub-agents outside the look-around budget) and `delete`, with short
# descriptions: every turn re-sends them, and the stock ones are 4,481 characters for these seven.
WRITER_TOOLS = ["ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"]
TOOL_TEXT = {
    "ls": "List a directory (absolute path).",
    "read_file": "Read a text file (absolute path): 100 lines from `offset` unless you pass `limit`. The lines after "
                 "the `@@ ... @@` header are the file's content.",
    "write_file": "Create or overwrite a file (absolute path) with `content`.",
    "edit_file": "Replace the exact text `old_string` with `new_string` in a file you have read. `old_string` must be "
                 "unique unless `replace_all` is true; keep the file's indentation.",
    "glob": "Find files by glob pattern under `path`, e.g. `**/test_*.py`.",
    "grep": "Search files under `path` for a literal string (not a regex); `glob` limits which files. Returns matching "
            "files; output_mode='content' shows the lines. For a regex, use execute with grep -rnE.",
    "execute": "Run a shell command in the sandbox with the repository's Python environment active (cd /testbed "
               "first for repo paths). Returns the output and exit code.",
}
# The stock skills prompt adds about 500 tokens of generic instructions to every turn; this says what matters.
SKILLS_PROMPT = """## Skills
Guides for situations only some issues have. When one fits this issue, read it with read_file (limit=1000) before writing the test; ignore the others.
{skills_list}"""
```

`PROMPT`: delete the two rules that moved to skills (the "If the bug IS an exception" bullet and the "When the
issue writes an expected result as code" bullet); keep every other line.

```python
# receipts/writer.py, after SafeSandbox
class SkillsFolder(FilesystemBackend):
    """This package's skills, served from the server's disk. Read-only and confined to the folder: the writer
    reads untrusted issue text, so nothing it is told can change a skill or reach other files."""

    def __init__(self):
        super().__init__(root_dir=SKILLS_DIR, virtual_mode=True)

    def write(self, file_path, content):
        return WriteResult(error=PERMISSION_DENIED)

    async def awrite(self, file_path, content):
        return self.write(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return EditResult(error=PERMISSION_DENIED)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return self.edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path):
        return DeleteResult(error=PERMISSION_DENIED)

    async def adelete(self, file_path):
        return self.delete(file_path)

    def upload_files(self, files):
        return [FileUploadResponse(path=path, error=PERMISSION_DENIED) for path, _ in files]

    async def aupload_files(self, files):
        return self.upload_files(files)


def build_agent(*, model, tools, system_prompt, backend, middleware):
    """Deepagents' own middleware with only what one test file needs: its file tools (no task, no delete), its
    skills (from the server, so reading one is no sandbox command), and dangling tool call repair."""
    files = CompositeBackend(default=backend, routes={SKILLS_ROOT: SkillsFolder()})
    return create_agent(model=model, tools=tools, system_prompt=system_prompt, middleware=[
        FilesystemMiddleware(backend=files, tools=WRITER_TOOLS, custom_tool_descriptions=TOOL_TEXT),
        SkillsMiddleware(backend=files, sources=[SKILLS_ROOT], system_prompt=SKILLS_PROMPT),
        PatchToolCallsMiddleware(),
        *middleware,
    ])
```

```python
# receipts/writer.py, ExplorationBudget
    def __init__(self, submit, skills_read: list | None = None):
        super().__init__()
        self.left = EXPLORE_LIMIT
        self.submit = submit  # the agent's submit_test tool
        self.seen: dict[str, str] = {}  # look-around call -> its output, until the next write changes things
        self.skills_read = [] if skills_read is None else skills_read  # skill files the agent read

    async def awrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        path = str(request.tool_call.get("args", {}).get("file_path", ""))
        if name == "read_file" and path.startswith(SKILLS_ROOT):  # guidance from the server, not looking around
            self.skills_read.append(path)
            return await handler(request)
        # (the rest of the method, unchanged)
```

```python
# receipts/writer.py, WriterResult: add
    skills_read: list[str] = field(default_factory=list)  # skill files the agent read

# receipts/writer.py, write_test: the agent line becomes
    agent = build_agent(model=config.llm(role), tools=[submit_test], system_prompt=PROMPT, backend=backend,
                        middleware=[ExplorationBudget(submit_test, out.skills_read)])
```

```python
# receipts/engine.py, ev["writer"]: add the key
                    "skills_read": getattr(w, "skills_read", []),
```

- [ ] **Step 5: Run the backend suite**

Run: `python -m pytest -q`
Expected: all pass. If `test_the_writer_sees_the_skills_list...` fails on the budget, print
`len(system)` and `len(json.dumps(tools))` and shorten `TOOL_TEXT`; do not raise the limit.

- [ ] **Step 6: Record the "after" overhead (no credit spent)**

Run `_first_turn()` from the test module in a one-off `python -c` and note `len(system)`,
`len(json.dumps(tools))` and the tool names for the results table (before: 2,515 + 12,342 characters, 10 tools).

- [ ] **Step 7: Commit**

```bash
git add receipts/writer.py receipts/engine.py receipts/skills tests/test_writer.py
git commit -m "Writer: Deepagents middleware without task or delete, short tool texts, read-only skills"
```

### Task 5: Start the writer's setup alongside classification

**Files:**
- Modify: `receipts/engine.py` (`_pipeline`, new `_drop`, `_writer_setup`; import `blind_workspace`,
  `stated_cases` from `.writer`)
- Modify: `receipts/writer.py` (`write_test(..., cases=None, workspace=None)`)
- Test: `tests/test_engine.py` (fixture stubs `engine.stated_cases` and `engine.blind_workspace`; new tests)

**Interfaces:**
- Consumes: Task 2's `_pipeline` shape.
- Produces: `write_test(issue, base_image, emit=None, *, brief="", history="", role="writer", cases=None,
  workspace=None)`: `cases` skips the stated-cases call, `workspace` skips making the blind workspace.

- [ ] **Step 1: Write the failing tests**

In the `fakes` fixture of `tests/test_engine.py` add:

```python
    async def no_cases(issue):
        return []

    async def own_copy(img):
        return img

    monkeypatch.setattr(engine, "stated_cases", no_cases)
    monkeypatch.setattr(engine, "blind_workspace", own_copy)
```

Append:

```python
def test_the_writers_setup_starts_while_the_claim_is_classified(monkeypatch):
    started, all_started = set(), asyncio.Event()

    def mark(name):
        started.add(name)
        if len(started) == 3:
            all_started.set()

    async def brief(repo, issue, search=None):
        mark("research")
        return research.Brief()

    async def cases(issue):
        mark("cases")
        return ["f(2) == 4"]

    async def workspace(img):
        mark("workspace")
        return img

    async def classify(issue, patch):
        await asyncio.wait_for(all_started.wait(), 1)  # setup that waits for the claim would time out here
        return engine.Claim(kind="fix", claim="c")

    seen = {}

    async def write_test(issue, img, emit=None, **kw):
        seen.update(kw)
        return SimpleNamespace(test_code="def test_bug(): assert 1 == 2", attempts=1, reason="ok", log=[],
                               submissions=[])

    for name, fn in [("classify", classify), ("stated_cases", cases), ("blind_workspace", workspace),
                     ("write_test", write_test)]:
        monkeypatch.setattr(engine, name, fn)
    monkeypatch.setattr(engine.research, "research", brief)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "PROVEN", ev["reason"]
    assert seen["cases"] == ["f(2) == 4"] and seen["workspace"] is not None


def test_a_claim_with_nothing_to_check_stops_the_writers_setup(monkeypatch):
    cancelled = []

    async def slow(*a, **k):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def classify(issue, patch):
        await asyncio.sleep(0.01)
        return engine.Claim(kind="none", claim="docs")

    monkeypatch.setattr(engine, "classify", classify)
    monkeypatch.setattr(engine, "stated_cases", slow)
    monkeypatch.setattr(engine.research, "research", slow)
    ev = asyncio.run(asyncio.wait_for(engine.check(INST, PATCH), 5))
    assert ev["verdict"] == "NO_CHECKABLE_CLAIM" and cancelled == [True, True]


def test_the_retry_reuses_the_stated_cases(monkeypatch):
    calls = _writer_sequence(monkeypatch, [None, "def test_bug(): assert 1 == 2"])
    asyncio.run(engine.check(INST, PATCH))
    assert calls[1]["cases"] == calls[0]["cases"] == [] and calls[1]["workspace"] is None  # a fresh copy
```

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_engine.py -q`
Expected: FAIL (`KeyError: 'cases'` / setup starts after the claim and times out)

- [ ] **Step 3: Implement**

```python
# receipts/writer.py, write_test signature and the two lines that use them
async def write_test(issue: str, base_image, emit=None, *, brief: str = "", history: str = "",
                     role: str = "writer", cases: list[str] | None = None, workspace=None) -> WriterResult:
    """... cases: the issue's stated cases, if already known; workspace: the blind copy of base, if made."""
    ...
    backend = await agent_backend(workspace or await blind_workspace(base_image), out.log)
    ...
    if cases is None:
        cases = await stated_cases(issue)
```

```python
# receipts/engine.py
from .writer import WriterResult, blind_workspace, retry_history, stated_cases, write_test


def _drop(*tasks) -> None:
    """Stop work nobody will wait for; finished work's failure is already part of the result."""
    for t in tasks:
        if t is None:
            continue
        if not t.done():
            t.cancel()
        elif not t.cancelled():
            t.exception()  # retrieved, so asyncio doesn't log it as lost


def _writer_setup(inst, env) -> list:
    """What the writer needs and the claim doesn't: research, the stated cases and its blind workspace."""
    async def workspace():
        return await blind_workspace(await env)

    return [asyncio.ensure_future(research.research(inst.repo, inst.problem_statement)),
            asyncio.ensure_future(stated_cases(inst.problem_statement)),
            asyncio.ensure_future(workspace())]
```

`_pipeline`'s first half (Task 2's version up to `say("test_accepted", ...)`) becomes the code below; the
forks, suites, opinions and `_remember` that follow stay as Task 2 left them, now inside the `try`. The two
`env.cancel()` calls go: the `finally` drops whatever is still running on every way out.

```python
async def _pipeline(inst, patch, ev, say, tests=None, run_id=None) -> tuple[Verdict, str]:
    # The environment doesn't depend on the claim, so it builds while the claim is classified (~10 s saved);
    # without a stored test, so does everything the writer needs.
    # ponytail: a PR with nothing to check pays for a few seconds of abandoned build and setup.
    env = asyncio.ensure_future(inst.base_image())
    key = blind_test_key(inst)
    setup = []
    try:
        stored = await _stored_test(tests, key)
        setup = [] if stored else _writer_setup(inst, env)
        claim = await classify(inst.problem_statement, patch or "")
        ev["claim"] = claim.model_dump()
        say("claim", ev["claim"])
        if claim.kind != "fix":
            return Verdict.NO_CHECKABLE_CLAIM, f"classified as '{claim.kind}': nothing to check"

        base = await env
        say("env_ready")
        w = await _reuse(stored, base, tests, key, say) if stored else None
        if w is None:
            setup = setup or _writer_setup(inst, env)  # a stale stored test: set up now
            brief_task, cases_task, workspace_task = setup
            brief = await brief_task
            ev["research"] = {"queries": brief.queries, "sources": brief.sources, "notes": brief.notes,
                              "errors": brief.errors}
            say("research", {"sources": len(brief.sources)})
            cases = await cases_task
            w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer(), cases=cases,
                                 workspace=await workspace_task)
            # The writer failed, not the PR, and no fork has run yet: one retry, never more. Not when the model
            # provider failed: its client already retried, and the same provider would only fail again.
            if w.test_code is None and not getattr(w, "provider_error", ""):
                say("writer_retry", {"model": config.MODELS["writer_strong"], "why": w.reason[:300]})
                ev["writer_first"] = {"attempts": w.attempts, "reason": w.reason,
                                      "submissions": getattr(w, "submissions", []), "tool_log": w.log}
                w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer(),
                                     history=retry_history(w), role="writer_strong", cases=cases)
        ev["writer"] = {"attempts": w.attempts, "reason": w.reason,
                        "test_code": w.test_code, "scope_check": getattr(w, "scope", ""), "tool_log": w.log,
                        "submissions": getattr(w, "submissions", []), "skills_read": getattr(w, "skills_read", [])}
        reused = getattr(w, "reused_from", "")
        if reused:
            ev["writer"]["reused_from"] = reused
        if w.test_code is None and getattr(w, "provider_error", ""):
            return Verdict.UNPROVEN, f"the test writer's model was unavailable: {w.provider_error}"
        if w.test_code is None:
            return Verdict.UNPROVEN, f"no valid reproducing test after {w.attempts} attempt(s): {w.reason}"
        say("test_accepted", {"attempts": w.attempts, **({"reused_from": reused} if reused else {})})
        # (from `test = {TEST_PATH: ...}` to the end: Task 2's code, indented one level, unchanged)
    finally:
        _drop(env, *setup)
```

The retry gets the same `cases` and no `workspace` (it makes a fresh copy).

- [ ] **Step 4: Run the backend suite**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add receipts/engine.py receipts/writer.py tests/test_engine.py
git commit -m "Start research, stated cases and the blind workspace while the claim is classified"
```

### Task 6: Keep built environments across restarts

**Files:**
- Modify: `receipts/targets.py` (`env_tag`, `_kept_env`, `_keep`, `RepoTarget.base_image`, `RepoTarget._build`)
- Test: `tests/test_targets.py`

**Interfaces:**
- Produces: `targets.env_tag(repo, base_sha) -> str`.

- [ ] **Step 1: Spike against Nebius Sandboxes (needs the user's yes; a few seconds of sandbox time)**

```bash
python - <<'EOF'
import asyncio, inspect
from receipts import config
async def main():
    c = config.contree()
    img = await c.images.oci("docker://docker.io/library/python:3.11")
    env = await img.run(shell="echo hi > /tmp/x", disposable=False, timeout=120)
    tag = "receipts-env/spike--probe:0123456789abcdef0123456789abcdef01234567"
    r = env.tag_as(tag)
    if inspect.isawaitable(r): r = await r
    found = await c.images.use(tag, strict=True)
    out = await found.run(shell="cat /tmp/x", timeout=120)
    print("roundtrip:", out.exit_code, out.stdout)
asyncio.run(main())
EOF
```

Expected: `roundtrip: 0 b'hi\n'`. If `tag_as` is synchronous or the lookup needs another reference form, use the
form that worked in Step 3. If no form round-trips, stop this task and record "dropped: tags don't round-trip"
in the spec results (no database table instead).

- [ ] **Step 2: Write the failing tests** (append to `tests/test_targets.py`)

```python
def _contree(images):
    return lambda: SimpleNamespace(images=images)


def test_a_kept_environment_skips_the_download_after_a_restart(monkeypatch):
    class Kept:
        exit_code, stdout = 0, b"tests/test_calc.py\npkg/test_util.py\n"

        async def run(self, **kw):
            return self

    class Images:
        async def use(self, ref, strict=False):
            assert ref == targets.env_tag("Octo/Hello", "SHA") and strict
            return Kept()

    async def fetch():
        raise AssertionError("a kept environment needs no download")

    monkeypatch.setattr(targets.config, "contree", _contree(Images()))
    monkeypatch.setattr(targets, "_envs", {})
    t = targets.RepoTarget("Octo/Hello#1", "Octo/Hello", "claim", "SHA", ["pkg/calc.py"], fetch)
    asyncio.run(t.base_image())
    assert t.suite == ["tests/test_calc.py"]


def test_a_new_environment_is_built_and_kept(monkeypatch):
    tags = []

    class Built:
        exit_code = 0

        async def run(self, **kw):
            return self

        async def tag_as(self, tag):
            tags.append(tag)
            return self

    class Images:
        async def use(self, ref, strict=False):
            raise LookupError("no such tag")

        async def oci(self, ref):
            return Built()

    async def fetch():
        return make_tarball({"octo-hello-abc/pkg/calc.py": b"", "octo-hello-abc/tests/test_calc.py": b""})

    monkeypatch.setattr(targets.config, "contree", _contree(Images()))
    monkeypatch.setattr(targets, "_envs", {})
    t = targets.RepoTarget("octo/hello#1", "octo/hello", "claim", "sha", ["pkg/calc.py"], fetch)
    asyncio.run(t.base_image())
    assert tags == [targets.env_tag("octo/hello", "sha")] and t.suite == ["tests/test_calc.py"]


def test_env_tags_are_lowercase_and_safe():
    assert targets.env_tag("Octo/Hello.World", "ABC123") == "receipts-env/octo--hello.world:abc123"
```

- [ ] **Step 3: Implement** (use Step 1's working call forms if they differ)

```python
# receipts/targets.py
import inspect
import logging
...
log = logging.getLogger("uvicorn.error")
TEST_FILES = "cd /testbed && find . -name 'test_*.py' -not -path './.git/*' | sed 's|^./||'"


def env_tag(repo: str, base_sha: str) -> str:
    """Where a built environment is kept in Nebius Sandboxes, so a restart doesn't rebuild it."""
    return f"receipts-env/{repo.lower().replace('/', '--')}:{base_sha.lower()}"


async def _kept_env(repo: str, base_sha: str):
    """(environment, its test files) built for this commit before a restart, or None."""
    try:
        env = await config.contree().images.use(env_tag(repo, base_sha), strict=True)
        listing = await env.run(shell=TEST_FILES, timeout=config.SANDBOX_TIMEOUT_S)
        return (env, set(text(listing.stdout).split())) if listing.exit_code == 0 else None
    except Exception as e:  # not kept, or the lookup failed: build it as before
        log.info("no kept environment for %s@%s: %s", repo, base_sha[:12], e)
        return None


async def _keep(env, repo: str, base_sha: str) -> None:
    try:
        tagged = env.tag_as(env_tag(repo, base_sha))
        if inspect.isawaitable(tagged):
            await tagged
    except Exception as e:
        log.warning("couldn't keep the environment for %s@%s: %s", repo, base_sha[:12], e)
```

```python
# receipts/targets.py, RepoTarget
    async def base_image(self):
        """The repo installed at base_sha. Downloads here, not in pr_target: the engine builds this while it
        classifies the claim, and a cached or kept environment needs no download at all."""
        key = (self.repo, self.base_sha)
        if key not in _envs:  # ponytail: per-process cache in front of the tags in Nebius Sandboxes
            _envs[key] = await _kept_env(self.repo, self.base_sha) or await self._build()
        env, files = _envs[key]
        self.suite = suite_for(self.changed, files)
        return env

    async def _build(self):
        tarball = await self.fetch()
        image = await config.contree().images.oci(PYTHON_IMAGE)
        env = await image.run(shell=SETUP, files={"/tmp/src.tar.gz": tarball}, disposable=False,
                              timeout=config.SANDBOX_TIMEOUT_S)
        if env.exit_code != 0:
            raise EnvironmentSetupError(f"setting up {self.repo} failed: {text(env.stderr)[-300:].strip()}")
        await _keep(env, self.repo, self.base_sha)
        return env, tar_files(tarball)
```

The existing `test_base_image_downloads_the_repo_once_and_picks_the_suite` fakes `Images` without `use`: add
`async def use(self, ref, strict=False): raise LookupError("no such tag")` to its `Images`, and give its `Image`
an `async def tag_as(self, tag): return self`.

- [ ] **Step 4: Run the backend suite**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add receipts/targets.py tests/test_targets.py
git commit -m "Keep each built environment in Nebius Sandboxes so a restart doesn't rebuild it"
```

### Task 7: Lean image and small cleanups

**Files:**
- Modify: `Dockerfile`, `receipts/demo.py` (`overview`), `receipts/research.py` (`research`), `README.md:11-12`
- Test: `tests/test_demo.py`, `tests/test_research.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_demo.py (append)
def test_the_demo_overview_asks_the_database_once(api, monkeypatch):
    calls = []
    store = api.store
    for name in ("usage", "list_for_user"):
        real = getattr(store, name)

        async def counted(*a, _real=real, _name=name, **k):
            calls.append(_name)
            return await _real(*a, **k)

        monkeypatch.setattr(store, name, counted)
    assert api.get("/api/demo").json()["left_today"] == config.DEMO_RUNS_PER_DAY
    assert calls == ["list_for_user"]
```

```python
# tests/test_research.py (append; use the module's imports for asyncio and research)
def test_the_two_searches_run_at_the_same_time():
    started, both = [], asyncio.Event()

    class Search:
        async def ainvoke(self, args):
            started.append(args["query"])
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)  # one at a time would time out here
            return {"results": []}

    issue = "```python\nsimplify(nsimplify(x))\n```"
    brief = asyncio.run(research.research("sympy/sympy", issue, search=Search()))
    assert len(started) == 2 and brief.errors == []
```

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_demo.py tests/test_research.py -q`
Expected: FAIL (`calls == ["usage", "list_for_user"]`; the second search times out)

- [ ] **Step 3: Implement**

```python
# receipts/demo.py, overview
@router.get("")
async def overview(runs=Depends(_runs)) -> dict:
    # One query: today's demo runs are among the latest max(50, cap), since the cap counts 24 hours.
    since = _since()
    recent = await runs.list_for_user(DEMO_USER, max(50, config.DEMO_RUNS_PER_DAY))
    today = sum(r["started_at"] >= since for r in recent)
    return {"cases": [{k: c[k] for k in PUBLIC_KEYS} for c in cases()], "live": live_run(),
            "gallery": [r for r in recent if r["status"] == "done"][:GALLERY_SIZE],
            "left_today": max(0, config.DEMO_RUNS_PER_DAY - today)}
```

```python
# receipts/research.py, research: replace the sequential loop
@traceable(name="research_brief")
async def research(repo: str, issue: str, search=None) -> Brief:
    library = library_of(repo, issue)
    brief = Brief(queries=[f"{library} {name}" for name in api_names(issue, library)])
    if not brief.queries:
        return brief
    try:
        if search is None:
            from langchain_tavily import TavilySearch

            search = TavilySearch(max_results=3, exclude_domains=CODE_HOSTS,
                                  include_domains=[DOCS[library]] if library in DOCS else DOC_DOMAINS)
    except Exception as e:  # no key: the writer works without docs, as it always did (one error per query)
        brief.errors += [f"{q}: {type(e).__name__}: {e}"[:300] for q in brief.queries]
        log.warning("research search unavailable: %s", e)
        return brief

    async def one(query: str) -> dict:
        try:  # no results, quota, timeout: the writer works without this part
            found = await asyncio.wait_for(search.ainvoke({"query": query}), SEARCH_TIMEOUT_S)
            return found if isinstance(found, dict) else {}
        except Exception as e:
            brief.errors.append(f"{query}: {type(e).__name__}: {e}"[:300])
            log.warning("research search failed: %s", brief.errors[-1])
            return {}

    notes = []
    for found in await asyncio.gather(*(one(q) for q in brief.queries)):  # results stay in query order
        for item in found.get("results", []):
            url = item.get("url") or ""
            if _allowed(url) and url not in {s["url"] for s in brief.sources}:
                brief.sources.append({"title": item.get("title") or url, "url": url})
                text = " ".join(str(item.get("content", "")).split())[:350]
                notes.append(f"- {item.get('title') or 'Docs'}: {text}")  # no link: a docs page can lead to source
    brief.notes = "\n".join(notes)[:NOTES_LIMIT]
    return brief
```

```dockerfile
# Dockerfile
# The Receipts API image (Render; Cloud Run works the same). The UI is the separate receipts-frontend repository.

# SWE-bench Verified, cached as one JSON file, so a cold start skips the 15-30 s Hugging Face download. Only
# this stage needs `datasets` (with pyarrow and pandas); the API never imports it.
FROM python:3.12-slim AS dataset
WORKDIR /app
COPY requirements.txt .
RUN grep -E '^(datasets|python-dotenv)==' requirements.txt > build.txt && pip install --no-cache-dir -r build.txt
COPY receipts/ receipts/
RUN python -c "from receipts import swebench; swebench._dataset()"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN grep -v '^datasets==' requirements.txt > runtime.txt && pip install --no-cache-dir -r runtime.txt
COPY receipts/ receipts/
COPY migrations/ migrations/
COPY --from=dataset /app/.cache/ .cache/

# Settings and secrets come from the host's environment (no .env in the image). The host sets PORT, and
# `serve` then listens on 0.0.0.0:$PORT.
CMD ["python", "-m", "receipts", "serve"]
```

README setup line:

```bash
pip install -r requirements.txt
```

- [ ] **Step 4: Check the Dockerfile's two filters locally (Docker isn't installed here)**

Run: `grep -E '^(datasets|python-dotenv)==' requirements.txt; grep -v '^datasets==' requirements.txt | grep -c .`
Expected: the two build pins, and 18 runtime lines (19 minus `datasets`).

- [ ] **Step 5: Run the backend suite**

Run: `python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add Dockerfile receipts/demo.py receipts/research.py README.md tests/test_demo.py tests/test_research.py
git commit -m "Leaner image without datasets; one query for the demo; both research searches at once"
```

### Task 8: Live verification, credit comparison and rollout (each live step needs the user's yes)

- [ ] **Step 1: Apply migration 003 to Neon**

Run: `python -m receipts migrate`
Expected: prints `003_blind_tests` as applied.

- [ ] **Step 2: The six demo cases, writer only (credit comparison with Task 0)**

Run each through the CLI (no store, so no reuse):

```bash
python -m receipts run pydata__xarray-4629 --patch gold
python -m receipts run pydata__xarray-4629 --patch none
python -m receipts run pydata__xarray-4629 --patch receipts/demo_patches/xarray-4629-copy-unused.diff
python -m receipts run psf__requests-1142 --patch gold
python -m receipts run psf__requests-1142 --patch none
python -m receipts run psf__requests-1142 --patch receipts/demo_patches/requests-1142-head-only.diff
```

Move the six new `runs/*.json` files to `runs/eval/` with an `eval-skills-` prefix (never public), then
`python scripts/usage_report.py runs/eval/eval-skills-*.json`. Expected: the same six verdicts as Phase 1
(PROVEN, REFUTED, REFUTED for each issue); tokens per check below the "before" average.

- [ ] **Step 3: Reuse, end to end on the local API**

Start the API (`python -m receipts serve`), start demo case `xarray-fix` with
`curl -s -X POST localhost:8000/api/demo/runs -H 'content-type: application/json' -d '{"case":"xarray-fix"}'`
(use the case ids from `GET /api/demo`), wait for it to finish, then start `xarray-empty`. Expected: the
second's events include `test_reused` and it finishes in under 45 s; report both with `usage_report.py --api
http://localhost:8000`.

- [ ] **Step 4: A kept environment across processes**

Run `python scripts/eval_prs.py --repo LaZy-Wolf/receipts-demo-sympy --label tag1 30`, then the same with
`--label tag2` (a new process). Expected: in `runs/eval/eval-tag2-pr30.json` the `env_ready` event comes at
least 20 s earlier than in `tag1`.

- [ ] **Step 5: Write the results into the spec, then push**

Add a `## Results` section to the spec: per-turn overhead before and after (characters and tools), the six-case
credit table before and after (tokens and, if priced, USD), the reuse timing, and the environment-tag timing.
Commit, then push `receipts-backend` and `receipts-frontend` to `main`; watch
`https://receipts-backend-wnjy.onrender.com/api/health` until it answers after the redeploy, and open one demo
receipt on the live site.
