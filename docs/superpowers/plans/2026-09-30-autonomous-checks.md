# Autonomous Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut writer-caused Unproven verdicts with a Tavily research brief and one self-recovery retry, explain verdicts on the GitHub check run, and cap spend now that the GitHub App is public.

**Architecture:** A deterministic research step (`receipts/research.py`) builds a docs brief from the issue text alone and hands it to the writer. The engine retries test-writing once, before any pull request fork runs, when no test was accepted, optionally on a stronger model chosen by an A/B run. `github_app.check_output` turns the evidence into the check run's text. `checks.enforce_limits` gains a global daily cap and the webhook an account allowlist.

**Tech Stack:** Python 3.12, FastAPI, Deepagents/LangChain, langchain-tavily, Nebius Token Factory models, Contree sandboxes, psycopg; React 19 + TypeScript frontend, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-30-autonomous-checks-design.md`

## Global Constraints

- Blindness: research and the writer see the issue and the unpatched repository only; queries come from issue text; `CODE_HOSTS` stay excluded from every Tavily search.
- Asymmetry: anything uncertain is Unproven; `verdict.py` rules do not change.
- Recovery runs only when `write_test` accepted no test, at most once per check, before any fork.
- No PR comments; no new GitHub App permissions; Auto-check stays opt-in.
- `GLOBAL_RUNS_PER_DAY` default 60; `MODEL_TEST_WRITER_STRONG` default `nvidia/nemotron-3-super-120b-a12b` unless the A/B (Task 3) says otherwise.
- Research never fails a check: any error gives an empty brief.
- Existing style: tests with fakes (no network), commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, no new dependencies, no em dashes in UI copy.
- Run backend tests with `python -m pytest -q` from `receipts-backend`; frontend with `npx vitest run` and `npm run build` from `receipts-frontend`.

## Review Focus

1. Tavily raises or `TAVILY_API_KEY` is missing: the brief is empty and the check continues (Task 2 test).
2. An issue with no code and no API names: no Tavily call at all, empty brief (Task 2 test).
3. The writer succeeds first time: no recovery; the writer fails twice: exactly one recovery, then the normal Unproven (Task 4 tests).
4. The global cap is reached: a manual start gets a clear 429; a webhook auto-check is skipped and logged, never raised (Task 1 tests).
5. A huge blind test or reason: the check-run output stays within GitHub's limits (Task 5 test).

---

### Task 1: Cost guard

**Files:**
- Modify: `receipts/config.py`, `receipts/db.py` (MemoryRuns, PgRuns), `receipts/checks.py:enforce_limits`, `receipts/github.py:_auto_check`, `.env.example`, `README.md`
- Test: `tests/test_github.py`, `tests/test_runs_store.py`

**Interfaces:**
- Produces: `runs.global_recent(since) -> int`; `config.GLOBAL_RUNS_PER_DAY: int`; `config.ALLOWED_GITHUB_ACCOUNTS: set[str]` (lowercase, empty = everyone).

- [ ] **Step 1: Failing tests**

```python
# tests/test_github.py
def test_global_daily_cap_refuses_new_checks(client, monkeypatch):
    signed_in(client).post("/api/github/installations/sync")
    monkeypatch.setattr(config, "GLOBAL_RUNS_PER_DAY", 0)
    r = client.post("/api/github/repos/octo/hello/pulls/12/check")
    assert r.status_code == 429 and "everyone" in r.json()["detail"]


def test_global_cap_skips_webhook_auto_checks_quietly(client, monkeypatch):
    signed_in(client).post("/api/github/installations/sync")
    client.put("/api/github/repos/42/auto-check", json={"enabled": True})
    monkeypatch.setattr(config, "GLOBAL_RUNS_PER_DAY", 0)
    assert webhook(client, PR_EVENT).status_code == 202 and client.store.rows == {}


def test_auto_checks_only_for_allowed_accounts(client, monkeypatch):
    signed_in(client).post("/api/github/installations/sync")
    client.put("/api/github/repos/42/auto-check", json={"enabled": True})
    event = {**PR_EVENT, "repository": {**PR_EVENT["repository"], "owner": {"login": "Octo"}}}
    monkeypatch.setattr(config, "ALLOWED_GITHUB_ACCOUNTS", {"someone-else"})
    webhook(client, event, delivery="a1")
    assert client.store.rows == {}
    monkeypatch.setattr(config, "ALLOWED_GITHUB_ACCOUNTS", {"octo"})
    webhook(client, event, delivery="a2")
    assert len(client.store.rows) == 1
```

```python
# tests/test_runs_store.py (MemoryRuns)
def test_global_recent_counts_every_user():
    import asyncio
    from datetime import datetime, timedelta, timezone
    runs = MemoryRuns()
    asyncio.run(runs.create("a", "u1", "i", "gold"))
    asyncio.run(runs.create("b", "u2", "i", "gold"))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert asyncio.run(runs.global_recent(since)) == 2
```

- [ ] **Step 2:** `python -m pytest tests/test_github.py tests/test_runs_store.py -q` → the four new tests FAIL (missing attribute / 202 instead of 429).

- [ ] **Step 3: Implement**

```python
# receipts/config.py, after RUNS_PER_DAY
GLOBAL_RUNS_PER_DAY = int(os.environ.get("GLOBAL_RUNS_PER_DAY", "60"))  # every user together: caps spend
# When set, webhook auto-checks run only on these GitHub accounts (the app is public; manual checks still work)
ALLOWED_GITHUB_ACCOUNTS = {a.strip().lower() for a in os.environ.get("ALLOWED_GITHUB_ACCOUNTS", "").split(",")
                           if a.strip()}
```

```python
# receipts/db.py, MemoryRuns
    async def global_recent(self, since):
        return sum(r["started_at"] >= since for r in self.rows.values())

# receipts/db.py, PgRuns
    async def global_recent(self, since):
        return (await self._one("SELECT count(*) AS n FROM runs WHERE started_at >= %s", (since,)))["n"]
```

```python
# receipts/checks.py, enforce_limits: compute since once and add after the per-user checks
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
```
(`usage()` in checks.py stays for `/api/me`.)

```python
# receipts/github.py, _auto_check, right after the auto-check setting test
    login = (repo.get("owner") or {}).get("login", "").lower()
    if config.ALLOWED_GITHUB_ACCOUNTS and login not in config.ALLOWED_GITHUB_ACCOUNTS:
        log.info("auto-check skipped for %s: account not allowed", repo["full_name"])
        return
```
The existing `except HTTPException` around `enforce_limits` in `_auto_check` already logs and skips a 429.

`.env.example`: add `GLOBAL_RUNS_PER_DAY=60` and `ALLOWED_GITHUB_ACCOUNTS=` with one comment line each. `README.md` limits section: one sentence each.

- [ ] **Step 4:** `python -m pytest -q` → all pass.
- [ ] **Step 5:** Commit `feat: global daily check cap and an allowlist for webhook auto-checks`.

---

### Task 2: Research brief (Tavily)

**Files:**
- Create: `receipts/research.py`, `tests/test_research.py`
- Modify: `receipts/writer.py` (remove `docs_search`, `TavilySearch`, `CODE_HOSTS`, `DOC_DOMAINS`, `WriterResult.queries`; add `brief` to `write_test`), `receipts/engine.py`, `tests/test_writer.py` (drop `TavilySearch` monkeypatches), `tests/test_engine.py`

**Interfaces:**
- Produces: `research.Brief(queries: list[str], sources: list[dict], notes: str)` with `.for_writer() -> str`; `async research.research(repo: str, issue: str, search=None) -> Brief`; `research.library_of(repo, issue) -> str`; `research.api_names(issue, library) -> list[str]`.
- `writer.write_test(issue, base_image, emit=None, *, brief="", history="", role="writer")` (history and role used in Task 4).
- Evidence: `ev["research"] = {"queries": [...], "sources": [{"title", "url"}]}`; event `research {"sources": n}`.

- [ ] **Step 1: Failing tests** (`tests/test_research.py`)

```python
import asyncio

from receipts import research

ISSUE_2 = """TypeError: Invalid NaN comparison when printing an expression containing f(nan)
```python
>>> f = Function('f')
>>> str(f(nan) + f(1))
TypeError: Invalid NaN comparison
>>> sorted([f(nan), f(-1), f(1)], key=default_sort_key)
```"""


class FakeSearch:
    def __init__(self, fail=False):
        self.queries, self.fail = [], fail

    async def ainvoke(self, args):
        self.queries.append(args["query"])
        if self.fail:
            raise RuntimeError("tavily down")
        return {"results": [{"title": "Sorting", "url": f"https://docs.sympy.org/{len(self.queries)}",
                             "content": "default_sort_key(item, order=None) returns a key ..."}]}


def test_library_and_api_names_come_from_the_issue():
    assert research.library_of("LaZy-Wolf/receipts-demo-sympy", ISSUE_2) == "sympy"
    assert research.library_of("psf/requests", "requests.get(url)") == "requests"
    assert research.api_names(ISSUE_2, "sympy") == ["default_sort_key", "Function"]  # no builtins, no `f`


def test_brief_searches_the_library_docs_and_keeps_sources():
    search = FakeSearch()
    brief = asyncio.run(research.research("LaZy-Wolf/receipts-demo-sympy", ISSUE_2, search))
    assert search.queries == ["sympy default_sort_key", "sympy Function"]
    assert [s["url"] for s in brief.sources] == ["https://docs.sympy.org/1", "https://docs.sympy.org/2"]
    assert "default_sort_key(item" in brief.for_writer() and len(brief.notes) <= 1200


def test_research_never_fails_a_check():
    assert asyncio.run(research.research("a/sympy", ISSUE_2, FakeSearch(fail=True))).sources == []
    quiet = FakeSearch()
    assert asyncio.run(research.research("a/b", "Something is slow sometimes.", quiet)).for_writer() == ""
    assert quiet.queries == []  # no names in the issue: no search
```

- [ ] **Step 2:** `python -m pytest tests/test_research.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `receipts/research.py`**

```python
"""Research brief: the library's own docs for the APIs an issue names, fetched before the test writer starts.

The writer had Tavily as an optional tool and called it 0 times in 40 runs; this step always runs. Blind like the
writer: queries come from the issue text alone and code hosts are excluded, so it cannot find the pull request.
Any failure gives an empty brief; research never fails a check.
"""
import builtins
import re
from dataclasses import dataclass, field

from langsmith import traceable

CODE_HOSTS = ["github.com", "gitlab.com", "bitbucket.org", "githubusercontent.com", "sourcegraph.com",
              "gitee.com", "codeberg.org", "huggingface.co", "swebench.com"]
# ponytail: docs sites for the repos we check; readthedocs `_modules` pages can still show newer source, so
# blindness against the web is best-effort (see README).
DOCS = {"sympy": "docs.sympy.org", "requests": "requests.readthedocs.io", "xarray": "docs.xarray.dev",
        "sklearn": "scikit-learn.org", "pylint": "pylint.readthedocs.io", "seaborn": "seaborn.pydata.org",
        "matplotlib": "matplotlib.org", "numpy": "numpy.org", "pandas": "pandas.pydata.org",
        "django": "docs.djangoproject.com", "flask": "flask.palletsprojects.com", "pytest": "docs.pytest.org",
        "astropy": "docs.astropy.org", "sphinx": "www.sphinx-doc.org"}
DOC_DOMAINS = sorted(set(DOCS.values()) | {"readthedocs.io", "docs.python.org"})
NOTES_LIMIT = 1200
_BUILTINS = set(dir(builtins))


@dataclass
class Brief:
    queries: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)  # {"title", "url"}
    notes: str = ""

    def for_writer(self) -> str:
        if not self.notes:
            return ""
        return "\n\nDocs for the APIs in the issue (usage only; the issue decides what is correct):\n" + self.notes


def library_of(repo: str, issue: str) -> str:
    for name in re.findall(r"(?:^|>>>\s*)\s*(?:from|import)\s+([A-Za-z_]\w*)", issue, re.M):
        return name
    name = repo.split("/")[-1].lower()
    return next((lib for lib in DOCS if lib in name), name)


def api_names(issue: str, library: str) -> list[str]:
    """Up to two names the issue calls or passes, longest first: the likeliest real APIs."""
    found = re.findall(r"\b([A-Za-z_]\w*)\s*\(", issue) + re.findall(r"=\s*([A-Za-z_]\w*)\b", issue)
    names = list(dict.fromkeys(n for n in found if len(n) >= 4 and n not in _BUILTINS and n != library))
    return sorted(names, key=len, reverse=True)[:2]


@traceable(name="research_brief")
async def research(repo: str, issue: str, search=None) -> Brief:
    library = library_of(repo, issue)
    brief = Brief(queries=[f"{library} {name}" for name in api_names(issue, library)])
    if not brief.queries:
        return brief
    notes = []
    try:
        if search is None:
            from langchain_tavily import TavilySearch

            search = TavilySearch(max_results=3, include_domains=[DOCS[library]] if library in DOCS else DOC_DOMAINS,
                                  exclude_domains=CODE_HOSTS)
        for query in brief.queries:
            found = await search.ainvoke({"query": query})
            for item in found.get("results", []) if isinstance(found, dict) else []:
                url = item.get("url")
                if url and url not in {s["url"] for s in brief.sources}:
                    title = item.get("title") or url
                    brief.sources.append({"title": title, "url": url})
                    notes.append(f"- {title} ({url}): {' '.join(str(item.get('content', '')).split())[:350]}")
    except Exception:  # no key, network, quota: the writer works without a brief, as it always did
        pass
    brief.notes = "\n".join(notes)[:NOTES_LIMIT]
    return brief
```

- [ ] **Step 4: Writer and engine**

`receipts/writer.py`: delete `CODE_HOSTS`, `DOC_DOMAINS`, the `TavilySearch` import, the `docs_search` tool, the `queries` field of `WriterResult`, and the prompt line "- docs_search is for library/API documentation only."; change the signature and the first message:

```python
async def write_test(issue: str, base_image, emit=None, *, brief: str = "", history: str = "",
                     role: str = "writer") -> WriterResult:
    ...
    agent = create_deep_agent(model=config.llm(role), tools=[submit_test],
                              system_prompt=PROMPT, backend=backend, middleware=[ExplorationBudget(submit_test)])
    brief_text = (f"Issue:\n\n{issue}"
                  + ("\n\nCases the issue states (cover each):" + "".join(f"\n- {c}" for c in cases) if cases else "")
                  + brief + history)
```
(keep the rest of the message handling; `brief_text` replaces the old `brief` variable name to avoid the clash.)

`receipts/engine.py` in `_pipeline`, after the claim check:

```python
    brief_task = asyncio.ensure_future(research.research(inst.repo, inst.problem_statement))
    base = await env
    say("env_ready")
    brief = await brief_task
    ev["research"] = {"queries": brief.queries, "sources": brief.sources}
    say("research", {"sources": len(brief.sources)})
    w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer())
```
and in `ev["writer"]` drop `"docs_queries": w.queries`. Import `research` at the top of engine.py.

Tests: in `tests/test_writer.py` delete every `monkeypatch.setattr(writer, "TavilySearch", ...)` line; in `tests/test_engine.py` make the fake `write_test(issue, img, emit=None, **kw)` and patch research in the `fakes` fixture:

```python
    async def no_research(repo, issue, search=None):
        return research.Brief()
    monkeypatch.setattr(engine.research, "research", no_research)
```
Add an engine test that the brief reaches the writer:

```python
def test_research_brief_reaches_the_writer(monkeypatch):
    seen = {}

    async def brief(repo, issue, search=None):
        return research.Brief(queries=["q"], sources=[{"title": "t", "url": "u"}], notes="- t (u): docs")

    async def write_test(issue, img, emit=None, **kw):
        seen.update(kw)
        return SimpleNamespace(test_code="def test_bug(): assert 1 == 2", attempts=1, reason="ok", log=[],
                               submissions=[])

    monkeypatch.setattr(engine.research, "research", brief)
    monkeypatch.setattr(engine, "write_test", write_test)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert "docs" in seen["brief"] and ev["research"]["sources"] == [{"title": "t", "url": "u"}]
```

- [ ] **Step 5:** `python -m pytest -q` → all pass. Live smoke (no sandbox): `python -c "import asyncio; from receipts import research; b = asyncio.run(research.research('LaZy-Wolf/receipts-demo-sympy', open('issue2.txt').read())); print(b.queries, b.sources)"` shows docs.sympy.org sources.
- [ ] **Step 6:** Commit `feat: a Tavily research brief of the library's docs before the test writer starts`.

---

### Task 3: A/B the writer model (experiment, no product code)

**Files:**
- Create: `scripts/eval_prs.py` (committed so the numbers can be reproduced)
- Modify: `receipts/config.py` (add the `writer_strong` role), `docs/superpowers/specs/2026-09-30-autonomous-checks-design.md` (record the result)

**Interfaces:**
- Produces: `config.MODELS["writer_strong"]`.

- [ ] **Step 1:** Add to `MODELS` in config.py:

```python
    # the self-recovery writer (Task 4); set equal to MODEL_TEST_WRITER if the A/B shows no gain
    "writer_strong": os.environ.get("MODEL_TEST_WRITER_STRONG", "nvidia/nemotron-3-super-120b-a12b"),
```

- [ ] **Step 2:** Create `scripts/eval_prs.py`: arguments `--repo OWNER/NAME --label L PR [PR ...]`; looks up the installation for the owner in `github_installations` and the repository id from `GET https://api.github.com/repos/OWNER/NAME`; runs `targets.pr_target` then `engine.check` two at a time; writes `runs/eval-<label>-pr<N>.json` and prints one JSON line per PR with verdict, seconds, tokens, attempts, accepted.
- [ ] **Step 3:** Run both arms on sympy #13 #14 #15 #16 #26 (10 checks):
  - `MODEL_TEST_WRITER=nvidia/Nemotron-3_5-Lightning python scripts/eval_prs.py --repo LaZy-Wolf/receipts-demo-sympy --label lightning 13 14 15 16 26`
  - `MODEL_TEST_WRITER=nvidia/nemotron-3-super-120b-a12b python scripts/eval_prs.py --repo LaZy-Wolf/receipts-demo-sympy --label super 13 14 15 16 26`
- [ ] **Step 4:** Decide by the spec's rule: Super becomes `writer_strong` if it gets an accepted test in at least 3 of 5 and stays under 1M tokens per check; otherwise set the default of `MODEL_TEST_WRITER_STRONG` to the Lightning id. Write the table and the decision into the spec's Open questions.
- [ ] **Step 5:** Commit `chore: eval script and the writer-model A/B result`.

---

### Task 4: Self-recovery

**Files:**
- Modify: `receipts/engine.py:_pipeline`, `receipts/writer.py` (add `retry_history`)
- Test: `tests/test_engine.py`, `tests/test_writer.py`

**Interfaces:**
- Consumes: `write_test(..., brief=, history=, role=)`, `config.MODELS["writer_strong"]`.
- Produces: `writer.retry_history(first: WriterResult) -> str`; event `writer_retry {"model", "why"}`; `ev["writer_first"] = {"attempts", "reason", "submissions"}`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_engine.py
def _writer_sequence(monkeypatch, results):
    calls = []

    async def write_test(issue, img, emit=None, **kw):
        calls.append(kw)
        code = results[len(calls) - 1]
        return SimpleNamespace(test_code=code, attempts=1, reason="writer gave up" if code is None else "ok",
                               log=[], submissions=[{"attempt": 1, "accepted": False, "reason": "r", "code": "x = 1"}])

    monkeypatch.setattr(engine, "write_test", write_test)
    return calls


def test_writer_failure_gets_one_retry_on_the_stronger_model(monkeypatch):
    calls = _writer_sequence(monkeypatch, [None, "def test_bug(): assert 1 == 2"])
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "PROVEN" and len(calls) == 2
    assert calls[1]["role"] == "writer_strong" and "writer gave up" in calls[1]["history"]
    assert ev["writer_first"]["reason"] == "writer gave up"
    assert [e["type"] for e in ev["events"]].count("writer_retry") == 1


def test_no_retry_when_the_first_test_is_accepted(monkeypatch):
    calls = _writer_sequence(monkeypatch, ["def test_bug(): assert 1 == 2"])
    ev = asyncio.run(engine.check(INST, PATCH))
    assert len(calls) == 1 and "writer_first" not in ev


def test_only_one_retry(monkeypatch):
    calls = _writer_sequence(monkeypatch, [None, None])
    ev = asyncio.run(engine.check(INST, PATCH))
    assert len(calls) == 2 and ev["verdict"] == "UNPROVEN" and "no valid reproducing test" in ev["reason"]
```

```python
# tests/test_writer.py
def test_retry_history_carries_the_last_rejection_and_file():
    first = writer.WriterResult(reason="every test passed on the unpatched code",
                                submissions=[{"attempt": 1, "accepted": False, "reason": "r", "code": "def test_a(): pass"}])
    text = writer.retry_history(first)
    assert "every test passed" in text and "def test_a(): pass" in text
```

- [ ] **Step 2:** `python -m pytest tests/test_engine.py tests/test_writer.py -q` → the new tests FAIL.
- [ ] **Step 3: Implement**

```python
# receipts/writer.py
def retry_history(first: "WriterResult") -> str:
    """What the failed first attempt ended on, so the retry doesn't repeat it."""
    last = first.submissions[-1]["code"] if first.submissions else None
    return (f"\n\nA previous attempt at this test failed: {first.reason}"
            + (f"\nIts last test file was:\n```python\n{last[:3000]}\n```\nDon't repeat its mistakes." if last else ""))
```

```python
# receipts/engine.py, replacing the single write_test call
    w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer())
    if w.test_code is None:  # the writer failed, not the PR, and no fork has run: one retry, never more
        say("writer_retry", {"model": config.MODELS["writer_strong"], "why": w.reason[:300]})
        first = w
        w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer(),
                             history=retry_history(first), role="writer_strong")
        ev["writer_first"] = {"attempts": first.attempts, "reason": first.reason,
                              "submissions": getattr(first, "submissions", [])}
```
Import `retry_history` from `.writer`.

- [ ] **Step 4:** `python -m pytest -q` → all pass.
- [ ] **Step 5: Live check:** `python scripts/eval_prs.py --repo LaZy-Wolf/receipts-demo-sympy --label recovery 13 14 15 16 26 30` (6 checks; #30 is the known PROVEN control). Record Unproven count before/after in the spec.
- [ ] **Step 6:** Commit `feat: one automatic retry, on the stronger writer, when no test was accepted`.

---

### Task 5: Check-run output

**Files:**
- Modify: `receipts/github_app.py` (`check_output`, `finish_check`), `receipts/github.py:start_pr_check` (pass evidence)
- Test: `tests/test_github_app.py`, `tests/test_github.py`

**Interfaces:**
- Produces: `github_app.check_output(evidence: dict, details_url: str) -> dict` with keys `title`, `summary`, `text`; `finish_check(installation_id, repo_id, full_name, check_run_id, evidence, details_url)`.

- [ ] **Step 1: Failing tests** (`tests/test_github_app.py`)

```python
def _ev(verdict, base_fail=3, pr_fail=0, msg="Expected z**4, got -z**4"):
    fail = {"tests": 1, "not_passed": {"t.py::t": {"outcome": "failed", "exc": "AssertionError", "msg": msg}},
            "output_tail": ""}
    ok = {"tests": 1, "not_passed": {}, "output_tail": ""}
    return {"verdict": verdict, "reason": "r", "claim": {"kind": "fix", "claim": "refine misses Abs(z)**4"},
            "writer": {"test_code": "def test_t():\n    assert 1\n" * 100},
            "forks": {"base_with_test": [fail] * base_fail + [ok] * (3 - base_fail),
                      "pr_with_test": [fail] * pr_fail + [ok] * (3 - pr_fail)}}


def test_check_output_explains_a_proven_run():
    out = github_app.check_output(_ev("PROVEN"), "https://r/1")
    assert out["title"].startswith("Proven")
    assert "fails on the original code in 3 of 3 runs" in out["summary"]
    assert "passes with this pull request in 3 of 3 runs" in out["summary"] and "https://r/1" in out["summary"]
    assert out["text"].startswith("### Blind test") and out["text"].count("\n") < 70  # first 60 lines only


def test_check_output_names_the_case_still_failing():
    out = github_app.check_output(_ev("UNPROVEN", pr_fail=3), "u")
    assert "Still failing with the change: Expected z**4, got -z**4" in out["summary"]


def test_check_output_stays_within_githubs_limits():
    ev = _ev("UNPROVEN", pr_fail=3, msg="x" * 100_000)
    ev["reason"] = "y" * 100_000
    out = github_app.check_output(ev, "u")
    assert len(out["summary"]) <= 65_535 and len(out["text"]) <= 65_535
```
Update `test_check_button_starts_a_run_and_a_check_run` in tests/test_github.py: `json.loads(complete.content)["output"]["title"]` starts with `"Proven"`.

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** (`receipts/github_app.py`)

```python
HEADLINE = {
    "PROVEN": "Proven: the pull request does what it claims",
    "REFUTED": "Refuted: the test still fails the same way with this change",
    "REGRESSION": "Regression: the fix breaks tests that passed before",
    "UNPROVEN": "Unproven: not enough evidence either way (this says nothing against the PR)",
    "NO_CHECKABLE_CLAIM": "No checkable claim: the PR doesn't claim to fix a bug",
}
LIMIT = 60_000  # GitHub allows 65,535 characters per output field


def _fails(runs) -> int:
    return sum(1 for r in runs if r.get("not_passed")) if isinstance(runs, list) else 0


def check_output(ev: dict, details_url: str) -> dict:
    """The check run's title, summary and text: what the verdict means and the evidence behind it."""
    verdict = ev.get("verdict") or "UNPROVEN"
    forks, lines = ev.get("forks") or {}, [f"**{HEADLINE.get(verdict, verdict)}**", ""]
    if claim := (ev.get("claim") or {}).get("claim"):
        lines.append(f"Claim: {claim}")
    base, pr = forks.get("base_with_test"), forks.get("pr_with_test")
    if isinstance(base, list) and base:
        lines.append(f"- Blind test fails on the original code in {_fails(base)} of {len(base)} runs")
    if isinstance(pr, list) and pr:
        lines.append(f"- Blind test passes with this pull request in {len(pr) - _fails(pr)} of {len(pr)} runs")
        still = next((f["msg"] for r in pr for f in r.get("not_passed", {}).values()), None)
        if still:
            lines.append(f"- Still failing with the change: {still.splitlines()[0][:500]}")
    elif isinstance(pr, str):
        lines.append("- The pull request's patch did not apply at its base, so it was never run")
    lines += ["", f"Reason: {(ev.get('reason') or '')[:2000]}", "", f"Full receipt: {details_url}"]
    code = ((ev.get("writer") or {}).get("test_code") or "").splitlines()[:60]
    text = "### Blind test (written from the issue alone)\n```python\n" + "\n".join(code)[:LIMIT] + "\n```" if code else ""
    return {"title": HEADLINE.get(verdict, verdict).split(":")[0], "summary": "\n".join(lines)[:LIMIT],
            "text": text[:LIMIT]}
```
Note: the phrases in the tests read "fails on the original code in 3 of 3 runs" / "passes with this pull request in 3 of 3 runs"; keep the list text consistent with them.

`finish_check(installation_id, repo_id, full_name, check_run_id, evidence, details_url)` sends
`"conclusion": CONCLUSION.get(evidence.get("verdict") or "UNPROVEN", "neutral")` and
`"output": check_output(evidence, details_url)`. In `github.start_pr_check.on_finish`, call
`github_app.finish_check(inst, repo_id, full, check.get("id"), evidence, details)`.

- [ ] **Step 4:** `python -m pytest -q` → all pass.
- [ ] **Step 5:** Commit `feat: the GitHub check run explains the verdict and shows the blind test`.

---

### Task 6: Frontend

**Files (receipts-frontend):**
- Modify: `src/api.ts` (Evidence: `research`, `writer.submissions`, `writer_first`; drop `docs_queries`), `src/receipt.ts` (`writerRetry` from the `writer_retry` event), `src/run/progress.ts`, `src/run/explain.ts`, `src/components/EvidenceDetails.tsx`
- Test: `src/run/progress.test.ts`, `src/run/explain.test.ts`, `src/receipt.test.ts`

**Interfaces:**
- Consumes: events `research`, `writer_retry`; evidence keys `research`, `writer.submissions`, `writer_first`.

- [ ] **Step 1: Failing tests**

```ts
// src/receipt.test.ts
it("notes a writer retry", () => {
  expect(fromEvents([e("writer_retry", { model: "m", why: "no test" })]).writerRetry).toBe(true);
});

// src/run/progress.test.ts (inside the existing describe)
it("says when the writer is retrying", () => {
  const r = fromEvents([e("claim", { kind: "fix", claim: "c" }), e("env_ready"), e("writer_retry", { model: "m" })]);
  expect(progressSteps(r, true, false).steps[2].detail).toBe("retrying with a stronger model");
});

// src/run/explain.test.ts
it("mentions an automatic retry", () => {
  const ev = { ...proven, writer_first: { attempts: 5, reason: "gave up", submissions: [] } } as Evidence;
  expect(explainVerdict(fromEvidence(ev), ev).body).toContain("retried once with a stronger model");
});
```
(`e`, `proven`, `fromEvents`, `fromEvidence` are the helpers/fixtures those test files already have; adapt names to the ones present.)

- [ ] **Step 2:** `npx vitest run` → FAIL.
- [ ] **Step 3: Implement**
  - `receipt.ts`: add `writerRetry: boolean` to `Receipt` (default `false` in `empty()`), and `else if (type === "writer_retry") r.writerRetry = true;` in `fromEvents`.
  - `progress.ts`: writer step detail becomes `wrote ? ... : r.writerRetry ? "retrying with a stronger model" : r.writerCommands ? ... : undefined`.
  - `explain.ts`: rename the current function to `explainBase`; export
    ```ts
    export function explainVerdict(r: Receipt, ev: Evidence): Explanation {
      const e = explainBase(r, ev);
      return ev.writer_first
        ? { ...e, body: `${e.body} The first test writer couldn't write a valid test, so Receipts retried once with a stronger model.` }
        : e;
    }
    ```
  - `EvidenceDetails.tsx`: replace the documentation-search count with two disclosures:
    - "Docs consulted" (when `ev.research?.sources.length`): a list of `<a href={s.url} target="_blank" rel="noreferrer">{s.title}</a>`, with the meta "searched before the test was written, never the pull request".
    - "Attempts" (when `w?.submissions?.length`): for each submission, "Attempt N: accepted | rejected" plus the reason, and a nested `<details>` with the file in `<pre className="code">`; include `ev.writer_first?.submissions` first under the heading "First writer".
- [ ] **Step 4:** `npx vitest run` and `npm run build` → pass. Check `/app/runs/<a recovery run>` in the browser at 1440 and 390 widths.
- [ ] **Step 5:** Commit `feat: receipts show the docs consulted, every attempt and an automatic retry`.

---

## Self-review notes

- Spec coverage: cost guard (Task 1), research brief (2), A/B gate (3), self-recovery (4), check-run text (5), frontend (6). Non-goals are untouched (no permissions, comments, default auto-check, dependency checks, verdict rule changes).
- Types: `write_test(..., brief, history, role)` is defined in Task 2 and consumed in Task 4; `Brief.for_writer()` in Task 2 and Task 4; `check_output` in Task 5 only.
- Review Focus: items 1-2 in Task 2 tests, 3 in Task 4 tests, 4 in Task 1 tests, 5 in Task 5 tests.
