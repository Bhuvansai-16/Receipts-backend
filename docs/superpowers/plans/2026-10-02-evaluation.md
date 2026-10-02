# Evaluation in LangSmith: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure Receipts against SWE-bench's answer key as LangSmith experiments (plus an AI-reader
baseline), improve the agent from the misses, and publish the numbers, the traces and the receipts.

**Architecture:** Pure logic (dataset selection, scoring, the reader's prompt) lives in
`receipts/evaluation.py` and is unit-tested; three thin scripts do the network work: `scripts/eval_dataset.py`
(build `eval/dataset.json`, upload to LangSmith), `scripts/eval_run.py` (`Client.aevaluate` for the
pipeline or the reader), `scripts/eval_report.py` (report, secret scan, share). `python -m receipts
import-eval` publishes receipts; `/results` in the frontend renders `results.json`.

**Tech Stack:** Python 3.12, langsmith 0.13 (`Client.aevaluate`, `create_examples`, `share_dataset`), the
existing engine and stores, React 19 + Vitest.

**Spec:** `docs/superpowers/specs/2026-10-02-evaluation-design.md`

## Global Constraints

- Spend efficiently: a one-issue pilot (5 cases) before any full run; one full baseline; the reader; at most two
  improvement experiments, each only if the misses point at a fix. Never re-run finished rows.
- The writer never sees a patch; hidden tests are labels only; verdict rules are never changed for numbers.
- No secrets in files, logs, traces or output; never print `.env` values.
- Backend tests: `python -m pytest -q`; frontend: `npx vitest run`, `npm run build`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; push only in Task 8.

## Review Focus

1. Two cases of one issue picked up at the same time: the second waits for the first's test and reuses it
   (Task 3 lock test).
2. A patch whose download fails, is empty or over 200 KB: skipped, the issue still gets its cases from other
   agents or is left out (Task 1 tests).
3. A row whose check errors: scored as Unproven, never as caught (Task 2 tests).
4. A trace carrying a secret: the share is refused (Task 7 planted-key test).
5. Eval receipts showing up in a gallery: owner `eval` is never listed (Task 6 test).

---

### Task 1: Dataset selection (pure) and the builder script

**Files:** Create `receipts/evaluation.py`, `scripts/eval_dataset.py`, `tests/test_evaluation.py`;
modify `.gitignore` (add `.cache/eval-patches/` only if `.cache` isn't already ignored).

**Produces:** `Case` (dataclass: `instance_id, repo, kind, agent, fixed, patch_url, patch_sha256`, property
`run_id`), `select_cases(rows, resolved, generated, fetch, *, issues=40, per_repo=8, max_bytes=200_000) ->
list[Case]`, `interleave(cases) -> list[Case]` (round-robin by instance, so concurrent rows are different issues).

- [ ] **Step 1: Failing tests**

```python
# tests/test_evaluation.py
from receipts import evaluation as E

ROWS = {f"o__r-{n}": {"instance_id": f"o__r-{n}", "repo": "o/r", "patch": "GOLD"} for n in range(1, 5)}
RESOLVED = {"a1": {"o__r-1", "o__r-2"}, "a2": set(), "a3": {"o__r-3"}}
GENERATED = {"a1": set(ROWS), "a2": set(ROWS), "a3": set(ROWS)}


def fetch_ok(agent, iid):
    return f"diff --git a/x b/x\n# {agent} {iid}\n"


def test_an_issue_needs_one_right_and_two_wrong_agent_patches():
    cases = E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10)
    by_issue = {}
    for c in cases:
        by_issue.setdefault(c.instance_id, []).append(c)
    # o__r-1: right a1, wrong a2 + a3 -> kept; o__r-4: nobody resolved it -> left out
    assert sorted(by_issue) == ["o__r-1", "o__r-2", "o__r-3"] or "o__r-4" not in by_issue
    one = sorted((c.kind, c.agent, c.fixed) for c in by_issue["o__r-1"])
    assert one == [("gold", "", True), ("none", "", False), ("right", "a1", True),
                   ("wrong", "a2", False), ("wrong", "a3", False)]


def test_unusable_patches_are_skipped():
    def fetch(agent, iid):
        return None if agent == "a2" else ("x" * 300_000 if agent == "a3" else fetch_ok(agent, iid))
    assert E.select_cases(ROWS, RESOLVED, GENERATED, fetch, issues=10) == []  # no two usable wrong patches


def test_per_repo_cap_and_issue_count():
    cases = E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10, per_repo=1)
    assert len({c.instance_id for c in cases}) == 1


def test_interleave_spreads_issues():
    cases = E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10)
    order = [c.instance_id for c in E.interleave(cases)]
    assert order[0] != order[1] and len(order) == len(cases)


def test_run_ids_are_safe_and_unique():
    from receipts.engine import safe_run_id
    cases = E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10)
    ids = [c.run_id for c in cases]
    assert len(set(ids)) == len(ids) and all(safe_run_id(i) for i in ids)
```

- [ ] **Step 2: Run, see them fail** (`python -m pytest tests/test_evaluation.py -q`: ImportError)

- [ ] **Step 3: Implement `receipts/evaluation.py` (selection part)**

```python
"""Evaluation against SWE-bench's answer key: which patches to check, how to score them, the reader baseline.

Pure functions here; scripts/eval_*.py do the downloads and the LangSmith calls. SWE-bench's hidden tests only
label the patches (fixed or not); Receipts never sees them.
"""
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

GOLD_URL = "swebench:gold"
NONE_URL = "none"


@dataclass(frozen=True)
class Case:
    instance_id: str
    repo: str
    kind: str  # gold | none | wrong | right
    agent: str  # SWE-bench submission name for agent patches, "" otherwise
    fixed: bool
    patch_url: str
    patch_sha256: str

    @property
    def run_id(self) -> str:
        agent = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.agent)[:40]
        return f"eval-{self.instance_id}-{self.kind}" + (f"-{agent}" if agent else "")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def patch_url(agent: str, instance_id: str) -> str:
    return f"https://swe-bench-submissions.s3.amazonaws.com/verified/{agent}/logs/{instance_id}/patch.diff"


def select_cases(rows: dict, resolved: dict[str, set], generated: dict[str, set],
                 fetch: Callable[[str, str], str | None], *, issues: int = 40, per_repo: int = 8,
                 max_bytes: int = 200_000) -> list[Case]:
    """Issues some sampled agent fixed and at least two didn't, in instance-id order, capped per repository.
    Each gets the real fix, an empty patch, two wrong agent patches (different agents) and one right one."""
    def usable(agent: str, iid: str) -> str | None:
        text = fetch(agent, iid)
        return text if text and text.strip() and len(text.encode()) <= max_bytes else None

    cases: list[Case] = []
    taken: dict[str, int] = {}
    for iid in sorted(rows):
        if sum(taken.values()) >= issues:
            break
        repo = rows[iid]["repo"]
        if taken.get(repo, 0) >= per_repo:
            continue
        right = [a for a in sorted(resolved) if iid in resolved[a]]
        wrong = [a for a in sorted(generated) if iid in generated[a] and iid not in resolved[a]]
        if not right or len(wrong) < 2:
            continue
        right_patch = next(((a, t) for a in right if (t := usable(a, iid))), None)
        wrong_patches = []
        for a in wrong:
            if len(wrong_patches) == 2:
                break
            if t := usable(a, iid):
                wrong_patches.append((a, t))
        if right_patch is None or len(wrong_patches) < 2:
            continue
        gold = rows[iid]["patch"]
        cases += [Case(iid, repo, "gold", "", True, GOLD_URL, sha256(gold)),
                  Case(iid, repo, "none", "", False, NONE_URL, ""),
                  *(Case(iid, repo, "wrong", a, False, patch_url(a, iid), sha256(t)) for a, t in wrong_patches),
                  Case(iid, repo, "right", right_patch[0], True, patch_url(right_patch[0], iid), sha256(right_patch[1]))]
        taken[repo] = taken.get(repo, 0) + 1
    return cases


def interleave(cases: list[Case]) -> list[Case]:
    """Round-robin by issue, so rows run at the same time are different issues (each issue writes one test)."""
    by_issue: dict[str, list[Case]] = {}
    for c in cases:
        by_issue.setdefault(c.instance_id, []).append(c)
    queues = list(by_issue.values())
    out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out
```

- [ ] **Step 4: Run, see them pass.**

- [ ] **Step 5: `scripts/eval_dataset.py`**: picks the submissions (fixed list `AGENTS`, chosen in Step 6 for a
  spread of resolve rates and with `logs` assets), reads their resolved ids (`results/results.json` or
  `per_instance_details.json` from raw.githubusercontent.com), treats every other Verified id as generated
  unless listed in `no_generation`/`no_logs`, fetches patches through a cache in `.cache/eval-patches/`
  (`<agent>/<iid>.diff`; a 404 caches as missing), runs `select_cases` on the pytest rows, writes
  `eval/dataset.json` (`[asdict(case) ...]`, interleaved), and with `--upload` replaces the LangSmith dataset
  `receipts-swebench-verified`: inputs `{instance_id, kind, agent, patch_url, patch_sha256}`, outputs
  `{"fixed": ...}`, metadata `{"repo": ...}`.

- [ ] **Step 6: Choose `AGENTS`**: list `evaluation/verified/` (GitHub API), keep entries whose `metadata.yaml`
  has a `logs` asset and whose results give a resolve rate between 25 % and 65 %, take 8 spread across that
  range (different agent families). Record the list and the rates in the script's docstring.

- [ ] **Step 7: Build the dataset** (`python scripts/eval_dataset.py`; downloads resolved lists and about 150
  patches, about 10 to 30 MB, into the git-ignored cache). Expected: `eval/dataset.json` with 40 issues, 200
  cases, at most 8 per repository. Commit the script, the module, the tests and `eval/dataset.json`.

### Task 2: Scoring and the reader (pure)

**Files:** Modify `receipts/evaluation.py`, `tests/test_evaluation.py`.

**Produces:** `row_scores(fixed: bool, verdict: str | None) -> dict[str, int]`,
`reader_scores(fixed: bool, answer: str) -> dict[str, int]`,
`summary(rows: list[dict], keys) -> dict[str, float]`, `ReaderAnswer` (pydantic: `answer:
Literal["fixed","not_fixed","unsure"]`, `reason: str`), `reader_prompt(issue: str, diff: str) -> str`.

- [ ] **Step 1: Failing tests**

```python
def test_row_scores_follow_the_truth_class():
    assert E.row_scores(False, "REFUTED") == {"caught": 1, "false_proven": 0, "unproven": 0}
    assert E.row_scores(False, "REGRESSION")["caught"] == 1
    assert E.row_scores(False, "PROVEN") == {"caught": 0, "false_proven": 1, "unproven": 0}
    assert E.row_scores(True, "PROVEN") == {"proven_fix": 1, "false_refuted": 0, "unproven": 0}
    assert E.row_scores(True, "REFUTED")["false_refuted"] == 1
    assert E.row_scores(False, None) == {"caught": 0, "false_proven": 0, "unproven": 1}  # the check errored


def test_reader_scores():
    assert E.reader_scores(True, "fixed") == {"accepted_fix": 1, "false_reject": 0, "unsure": 0}
    assert E.reader_scores(False, "fixed") == {"rejected_wrong": 0, "false_accept": 1, "unsure": 0}
    assert E.reader_scores(False, "unsure")["unsure"] == 1


def test_summary_rates_are_means_over_rows_that_have_the_key():
    rows = [E.row_scores(False, "REFUTED"), E.row_scores(False, "PROVEN"), E.row_scores(True, "PROVEN")]
    s = E.summary(rows, ["caught", "false_proven", "proven_fix", "false_refuted"])
    assert s == {"caught": 0.5, "false_proven": 0.5, "proven_fix": 1.0, "false_refuted": 0.0}


def test_reader_prompt_truncates_the_diff():
    p = E.reader_prompt("issue text", "x" * 50_000)
    assert "issue text" in p and len(p) < 32_000
```

- [ ] **Step 2: Run, see them fail.**

- [ ] **Step 3: Implement**

```python
from typing import Literal
from pydantic import BaseModel

CAUGHT = {"REFUTED", "REGRESSION"}
NOT_CHECKED = {"UNPROVEN", "NO_CHECKABLE_CLAIM", None}
DIFF_LIMIT = 30_000


def row_scores(fixed: bool, verdict: str | None) -> dict[str, int]:
    unproven = int(verdict in NOT_CHECKED)
    if fixed:
        return {"proven_fix": int(verdict == "PROVEN"), "false_refuted": int(verdict == "REFUTED"), "unproven": unproven}
    return {"caught": int(verdict in CAUGHT), "false_proven": int(verdict == "PROVEN"), "unproven": unproven}


def reader_scores(fixed: bool, answer: str) -> dict[str, int]:
    unsure = int(answer not in ("fixed", "not_fixed"))
    if fixed:
        return {"accepted_fix": int(answer == "fixed"), "false_reject": int(answer == "not_fixed"), "unsure": unsure}
    return {"rejected_wrong": int(answer == "not_fixed"), "false_accept": int(answer == "fixed"), "unsure": unsure}


def summary(rows: list[dict], keys) -> dict[str, float]:
    out = {}
    for k in keys:
        vals = [r[k] for r in rows if k in r]
        out[k] = round(sum(vals) / len(vals), 4) if vals else 0.0
    return out


class ReaderAnswer(BaseModel):
    answer: Literal["fixed", "not_fixed", "unsure"]
    reason: str


def reader_prompt(issue: str, diff: str) -> str:
    return ("You review a pull request. Read the issue and the diff, and say whether the diff fixes the issue as "
            "described: fixed, not_fixed, or unsure. Give the reason in one or two sentences.\n\n"
            f"Issue:\n{issue[:8000]}\n\nDiff:\n{diff[:DIFF_LIMIT - 9000]}")
```

- [ ] **Step 4: Run, see them pass; commit.**

### Task 3: The experiment runner and the pilot

**Files:** Create `scripts/eval_run.py`; modify `receipts/evaluation.py` (`Harness`), `tests/test_evaluation.py`.

**Produces:** `Harness(cases_by_run_id, load_patch, out_dir)` with `async __call__(inputs) -> dict` returning
`{run_id, verdict, reason, seconds, tokens, cost_usd, reused}`; per-instance `asyncio.Lock`; one
`db.MemoryRuns` store.

- [ ] **Step 1: Failing test** (fake `engine.check` that records order and sleeps): two inputs of the same
  instance started together run one after the other, and the second sees the first's stored test
  (`tests=` is the same store). Also: `cost_usd` uses `scripts/usage_report.PRICES`-equivalent prices moved into
  `receipts/evaluation.py` as `PRICES` (the usage report imports them from there).

- [ ] **Step 2: Implement `Harness`**: `async with self.locks[iid]:` load the instance
  (`swebench.load_instance`), the patch (`None` for `none`, the instance's gold patch for `gold`, else the cached
  file checked against its sha256), call `engine.check(inst, patch, tests=self.store, run_id=case.run_id)`, write
  the evidence to `out_dir/<run_id>.json`, return the outputs. Errors become `verdict: None` with the reason.

- [ ] **Step 3: `scripts/eval_run.py`**: `--mode receipts|reader`, `--limit N` (first N issues), `--resume
  <experiment name>`; builds the evaluators (`row_scores` / `reader_scores` returning
  `{"results": [{"key": k, "score": v} ...]}` from `outputs` and `reference_outputs`), summary evaluators
  (`summary` over the rows), and calls `await Client().aevaluate(target, data=examples, evaluators=...,
  summary_evaluators=..., max_concurrency=4, experiment_prefix=..., metadata={"models": config.MODELS, "commit":
  <git sha>}, error_handling="log")`. With `--resume`, `data` is only the examples with no run in that
  experiment. The reader target: `config.llm("judge").with_structured_output(ReaderAnswer,
  method="function_calling")` on `reader_prompt(issue, diff)`.

- [ ] **Step 4: Run the tests; then the pilot** (one issue, five cases, about $0.05):
  `python scripts/eval_run.py --mode receipts --limit 1`. Expected: an experiment with 5 rows, feedback on each,
  the first case wrote the test and four reused it (`reused` true), traces open in LangSmith. Commit.

### Task 4: Baseline and reader experiments (live)

- [ ] **Step 1:** `python scripts/eval_run.py --mode receipts` (background; about 30 to 60 minutes). If it stops,
  `--resume <name>`. Expected: 200 rows with feedback.
- [ ] **Step 2:** `python scripts/eval_run.py --mode reader` (a few minutes). Expected: 200 rows.
- [ ] **Step 3:** Record both experiment names and their summary scores in the ledger.

### Task 5: Improvement loop (at most two experiments)

- [ ] **Step 1:** Pull the baseline's misses (`false_proven`, `false_refuted`, `unproven` rows) with their
  reasons; group them by cause (no valid test, gates, environment, apply failure, flaky base, library pitfall).
- [ ] **Step 2:** If a group is large and has a small, reviewable fix (a skill, a prompt line, a gate's wording,
  a budget), make it test-first and run `--mode receipts --limit <the affected issues>` as a new experiment; keep
  it only if the targeted rate improves and `false_refuted` doesn't rise. At most two such changes; skip the loop
  when no group is worth it, and say so in the results.

### Task 6: Report and publishing receipts

**Files:** Create `scripts/eval_report.py`; modify `receipts/db.py` (`import_run(..., user_id=None)`,
`_imported(..., user_id)`), `receipts/__main__.py` (`import-eval DIR`), tests in `tests/test_runs_store.py` and
`tests/test_evaluation.py`.

- [ ] **Step 1: Failing tests**: `import_run(run_id, ev, user_id="eval")` stores the owner on both stores;
  `list_for_user("demo", ...)` doesn't return it; `report(rows)` (pure, in `evaluation.py`) builds the headline
  table, the per-repository table and the misses list from fixture rows.
- [ ] **Step 2: Implement**; `eval_report.py` reads the saved receipts and reader outputs (from the experiments'
  feedback via `Client.list_runs` or the files in `runs/eval/`), writes `eval/RESULTS.md` and `eval/results.json`
  (each miss with its `/runs/<id>` link and its LangSmith trace URL).
- [ ] **Step 3:** `python -m receipts import-eval runs/eval/<baseline>` loads the receipts into Neon (owner
  `eval`). Commit.

### Task 7: Secret scan and the public dataset

- [ ] **Step 1: Failing test**: `find_secrets(text, values)` (pure) finds a planted value and common token
  shapes (`sk-`, `ghp_`, `-----BEGIN`), and nothing in clean text.
- [ ] **Step 2: Implement** and run it over every run of both experiments (inputs, outputs, error fields),
  comparing against the `.env` values (read, never printed). Expected: 0 findings; any finding stops the share.
- [ ] **Step 3:** `Client().share_dataset(dataset_name="receipts-swebench-verified")` and record the public URL
  in `eval/RESULTS.md`.

### Task 8: `/results` page, README links, push

**Files (frontend):** `src/eval-results.json` (copied from `eval/results.json`), `src/site/ResultsPage.tsx`,
`src/site/results.ts` (formatting helpers) + `src/site/results.test.ts`, `src/App.tsx` (route `/results`),
`src/site/SiteLayout.tsx` (nav "Results" before "Live demo"; footer link).

- [ ] **Step 1: Failing test** for the helpers (`percent(0.4567) === "46%"`, rows sorted by repository).
- [ ] **Step 2: Implement the page**: headline numbers (Receipts vs the reader), the per-repository table, the
  misses with links to `/runs/<id>`, the LangSmith public link, and how it was measured (the dataset, labels from
  SWE-bench's hidden tests).
- [ ] **Step 3:** Backend README: a "Measured against SWE-bench" block in Results with the headline numbers and
  links to `eval/RESULTS.md`, `/results` and the public dataset.
- [ ] **Step 4:** Both suites, then push both repositories; check `/results` and one eval receipt on the live
  site.
