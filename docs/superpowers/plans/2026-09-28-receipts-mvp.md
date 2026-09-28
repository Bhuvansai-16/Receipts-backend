# Receipts MVP (Fix Engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI that takes a SWE-bench Verified instance + a patch and returns PROVEN / REFUTED / REGRESSION / UNPROVEN with evidence, by writing a blind reproducing test and running it in forked Token Factory sandboxes.

**Architecture:** Deterministic Python orchestrator (`engine.py`). One Deepagents agent (Nemotron Lightning) writes the test inside a Contree sandbox using the SDK's ready-made `ContreeSandbox` backend. Every pytest run happens in a throwaway fork of a clean image, with a tiny pytest plugin that records each test's exact exception type. Verdict rules are pure functions (`verdict.py`). Nano classifies the claim; Ultra is consulted only before a REFUTED verdict and can only downgrade it.

**Tech Stack:** Python 3.12 (global, no venv), deepagents 0.7.15, langchain-openai (Nebius Token Factory, OpenAI-compatible), contree-sdk 0.3.6 + contree-client[httpx] 0.4.0, langchain-tavily, langsmith, datasets 3.6, pytest 9.

**Spec:** `docs/superpowers/specs/2026-09-28-receipts-mvp-design.md`

## Global Constraints

- No venv. Run with global `python`. All config from `receipts/.env` (loaded by `receipts/config.py`).
- Models: classifier `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, writer `nvidia/Nemotron-3_5-Lightning`, judge `nvidia/Nemotron-3-Ultra-550b-a55b`; base URL `https://api.tokenfactory.nebius.com/v1/`; structured output uses `method="function_calling"`.
- Sandbox auth: `IAMAuth(token=<env var name>, project_id="CONTREE_PROJECT")`; token env var `CONTREE_TOKEN`, falling back to `NEBIUS_API_KEY`.
- The test writer never receives the patch, `test_patch`, `FAIL_TO_PASS`, or `hints_text`.
- A reproducing test must fail on base with `AssertionError`; any other failure type does not count.
- Uncertain → UNPROVEN. No code path may turn an exception or sandbox error into REFUTED or REGRESSION.
- Python + pytest only. Repos outside `PYTEST_REPOS` are rejected.
- Tavily excludes code hosts: `github.com, gitlab.com, bitbucket.org, githubusercontent.com, sourcegraph.com, gitee.com, codeberg.org, huggingface.co, swebench.com`.

## Review Focus

1. Existing tests that already fail on base (requests' suite calls httpbin; the sandbox may have no network) must not produce REGRESSION: the suite runs on base too, and only base-passing tests count. Covered by `test_suite_failing_on_base_is_not_regression` (Task 2).
2. Sandbox error/timeout in any run (empty results) → UNPROVEN, never REFUTED/REGRESSION. Covered by `test_empty_pr_run_is_unproven`, `test_suite_could_not_run_is_unproven` (Task 2).
3. Assertion messages containing memory addresses (`<Foo at 0x7f..>`) differ run to run. They must still compare equal, so REFUTED isn't lost to noise. Covered by `test_memory_addresses_normalised` (Task 2).
4. Empty `PASS_TO_PASS` must not call `pytest.main([])`, which would run the entire repo. Covered by `test_run_pytest_refuses_empty_targets` (Task 3).
5. Unknown instance id / non-pytest repo → clear CLI error, no verdict. Covered by `test_load_instance_rejects_*` (Task 4).

---

### Task 1: Scaffold, config, .env.example

**Files:**
- Create: `receipts/__init__.py` (empty), `receipts/config.py`, `.env.example`, `README.md`

**Interfaces:**
- Produces: `config.MODELS: dict[str,str]` (keys `classifier|writer|judge`), `config.llm(role) -> ChatOpenAI`, `config.contree() -> contree_sdk.Contree`, `config.SANDBOX_TIMEOUT_S`, `config.SANDBOX_MAX_CONCURRENCY`, `config.MAX_TEST_ATTEMPTS`, `config.VERDICT_RUNS`, `config.RUNS_DIR: Path`.

- [ ] **Step 1: Write `receipts/config.py`**

```python
"""Settings from .env and client factories. Runs on global Python; no venv."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# LangSmith tracing is env-driven; turn it on whenever a key is present.
os.environ.setdefault("LANGSMITH_PROJECT", "receipts")
if os.environ.get("LANGSMITH_API_KEY"):
    os.environ.setdefault("LANGSMITH_TRACING", "true")

NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
MODELS = {
    "classifier": os.environ.get("MODEL_CLASSIFIER", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
    "writer": os.environ.get("MODEL_TEST_WRITER", "nvidia/Nemotron-3_5-Lightning"),
    "judge": os.environ.get("MODEL_JUDGE", "nvidia/Nemotron-3-Ultra-550b-a55b"),
}
SANDBOX_TIMEOUT_S = int(os.environ.get("SANDBOX_TIMEOUT_S", "900"))
SANDBOX_MAX_CONCURRENCY = int(os.environ.get("SANDBOX_MAX_CONCURRENCY", "20"))
MAX_TEST_ATTEMPTS = int(os.environ.get("MAX_TEST_ATTEMPTS", "5"))
VERDICT_RUNS = int(os.environ.get("VERDICT_RUNS", "3"))
RUNS_DIR = ROOT / "runs"


def llm(role: str):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=MODELS[role],
        api_key=os.environ["NEBIUS_API_KEY"],
        base_url=NEBIUS_BASE_URL,
        temperature=0,
        max_retries=3,
        timeout=300,
    )


def contree():
    from contree_sdk import Contree
    from contree_sdk.auth import IAMAuth
    from contree_sdk.config import ContreeConfig

    # IAMAuth takes env var *names* and resolves them itself.
    kw = {"token": "CONTREE_TOKEN" if os.environ.get("CONTREE_TOKEN") else "NEBIUS_API_KEY", "project_id": "CONTREE_PROJECT"}
    if os.environ.get("CONTREE_BASE_URL"):
        kw["base_url"] = "CONTREE_BASE_URL"
    return Contree(ContreeConfig(auth=IAMAuth(**kw), operation_run_timeout=SANDBOX_TIMEOUT_S))
```

- [ ] **Step 2: Write `.env.example`**

```dotenv
# --- Nebius Token Factory inference (required) ---
NEBIUS_API_KEY=
# NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
# MODEL_CLASSIFIER=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
# MODEL_TEST_WRITER=nvidia/Nemotron-3_5-Lightning
# MODEL_JUDGE=nvidia/Nemotron-3-Ultra-550b-a55b

# --- Token Factory Sandboxes (required) ---
CONTREE_PROJECT=
# CONTREE_TOKEN=            # only if sandbox key differs from NEBIUS_API_KEY
# CONTREE_BASE_URL=https://api.tokenfactory.nebius.com/sandboxes
# SANDBOX_TIMEOUT_S=900
# SANDBOX_MAX_CONCURRENCY=20

# --- LangSmith (tracing turns on automatically when the key is set) ---
LANGSMITH_API_KEY=
# LANGSMITH_PROJECT=receipts

# --- Tavily (docs search for the test writer) ---
TAVILY_API_KEY=

# --- Verdict knobs ---
# MAX_TEST_ATTEMPTS=5
# VERDICT_RUNS=3
```

- [ ] **Step 3: Write `README.md`**

````markdown
# Receipts

Checks whether a pull request does what it claims: writes the missing test blind (from the issue only),
runs it in forked Nebius Token Factory sandboxes, and reports the evidence.

MVP: fix engine on SWE-bench Verified, CLI only.

## Setup (global Python, no venv)

```bash
pip install contree-sdk "contree-client[httpx]" deepagents langchain-openai langchain-tavily langsmith datasets python-dotenv
cp .env.example .env   # fill in keys
python -m receipts smoke --instance psf__requests-1142
```

## Run

```bash
python -m receipts run psf__requests-1142 --patch gold   # real fix, expect PROVEN
python -m receipts run psf__requests-1142 --patch none   # PR that changes nothing, expect REFUTED/UNPROVEN
python -m receipts run psf__requests-1142 --patch my.diff
```

Evidence JSON lands in `runs/`. Traces in LangSmith project `receipts`.

## Verdicts

| Verdict | Meaning |
|---|---|
| PROVEN | Blind test fails on base with AssertionError (3/3), passes on PR (3/3), existing tests that passed on base still pass |
| REGRESSION | Test passes on PR, but existing tests that passed on base now fail consistently |
| REFUTED | Test fails on PR with the same assertion as base (3/3) and Nemotron Ultra confirms the test matches the issue |
| UNPROVEN | Anything else. Explicitly not evidence against the PR |
| NO_CHECKABLE_CLAIM | Not a bug-fix claim |

## Models

| Step | Model |
|---|---|
| Claim classification | Nemotron 3 Nano |
| Blind test writing (Deepagents agent in sandbox) | Nemotron 3.5 Lightning |
| Second opinion before REFUTED | Nemotron 3 Ultra |
````

- [ ] **Step 4: Verify config imports and loads .env**

Run: `python -c "from receipts import config; print(config.MODELS['writer'], bool(config.os.environ.get('NEBIUS_API_KEY')))"` from `receipts/`
Expected: `nvidia/Nemotron-3_5-Lightning True`

- [ ] **Step 5: Commit**

```bash
git add receipts/__init__.py receipts/config.py .env.example README.md
git commit -m "feat: config from .env, model and sandbox factories"
```

---

### Task 2: Verdict rules (pure)

**Files:**
- Create: `receipts/verdict.py`
- Test: `tests/test_verdict.py`

**Interfaces:**
- Produces:
  - `TestResult(outcome: str, exc: str | None = None, msg: str = "")`, outcome ∈ `passed|failed|error|skipped`
  - `PytestRun(results: dict[str, TestResult] = {}, output: str = "")`
  - `Verdict` (str Enum): `PROVEN, REFUTED, REGRESSION, UNPROVEN, NO_CHECKABLE_CLAIM`
  - `repro_check(run: PytestRun) -> tuple[bool, str]`
  - `suite_candidates(base_suite: PytestRun, pr_suite: PytestRun) -> list[str]`
  - `fix_verdict(base_runs: list[PytestRun], pr_runs: list[PytestRun] | None, base_suite: PytestRun | None, pr_suites: list[PytestRun] | None) -> tuple[Verdict, str]`

- [ ] **Step 1: Write the failing tests `tests/test_verdict.py`**

```python
from receipts.verdict import PytestRun, TestResult, Verdict, fix_verdict, repro_check, suite_candidates

T = "receipts_test.py::test_bug"


def R(d):
    """R({nodeid: (outcome, exc, msg)}) -> PytestRun"""
    return PytestRun({k: TestResult(*v) for k, v in d.items()}, "out")


FAIL = {T: ("failed", "AssertionError", "assert 1 == 2")}
PASS = {T: ("passed",)}


def test_proven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.PROVEN


def test_refuted_same_assertion():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(FAIL)] * 3, None, None)
    assert v is Verdict.REFUTED


def test_regression_consistent_suite_failure():
    base_suite = R({"t.py::a": ("passed",), "t.py::b": ("passed",)})
    pr_full = R({"t.py::a": ("passed",), "t.py::b": ("failed", "AssertionError", "x")})
    rerun = R({"t.py::b": ("failed", "AssertionError", "x")})
    v, why = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, base_suite, [pr_full, rerun, rerun])
    assert v is Verdict.REGRESSION and "t.py::b" in why


def test_flaky_suite_failure_is_not_regression():
    base_suite = R({"t.py::b": ("passed",)})
    pr_full = R({"t.py::b": ("failed", "AssertionError", "x")})
    rerun_ok = R({"t.py::b": ("passed",)})
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, base_suite, [pr_full, rerun_ok, rerun_ok])
    assert v is Verdict.PROVEN


def test_suite_failing_on_base_is_not_regression():
    base_suite = R({"t.py::net": ("failed", "ConnectionError", "no network")})
    pr_full = R({"t.py::net": ("failed", "ConnectionError", "no network")})
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, base_suite, [pr_full])
    assert v is Verdict.PROVEN


def test_suite_could_not_run_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, R({"t.py::a": ("passed",)}), [PytestRun()])
    assert v is Verdict.UNPROVEN


def test_base_import_error_is_unproven():
    v, why = fix_verdict([R({T: ("error", "ImportError", "no module")})] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN and "AssertionError" in why


def test_base_other_exception_is_unproven():
    v, _ = fix_verdict([R({T: ("failed", "ValueError", "boom")})] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_base_all_pass_is_unproven():
    v, _ = fix_verdict([R(PASS)] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_base_empty_run_is_unproven():
    v, _ = fix_verdict([PytestRun()] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_base_flaky_is_unproven():
    other = R({T: ("failed", "AssertionError", "a"), "receipts_test.py::t2": ("failed", "AssertionError", "b")})
    v, _ = fix_verdict([R(FAIL), R(FAIL), other], [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_patch_not_applied_is_unproven():
    v, why = fix_verdict([R(FAIL)] * 3, None, None, None)
    assert v is Verdict.UNPROVEN and "apply" in why


def test_mixed_pr_runs_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS), R(FAIL), R(FAIL)], None, None)
    assert v is Verdict.UNPROVEN


def test_pr_fails_differently_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R({T: ("failed", "AssertionError", "assert 5 == 2")})] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_empty_pr_run_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [PytestRun()] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_memory_addresses_normalised():
    b = R({T: ("failed", "AssertionError", "assert <Obj at 0x7f00aa> == 1")})
    p = R({T: ("failed", "AssertionError", "assert <Obj at 0x7f99bb> == 1")})
    v, _ = fix_verdict([b] * 3, [p] * 3, None, None)
    assert v is Verdict.REFUTED


def test_repro_allows_extra_passing_tests():
    ok, _ = repro_check(R({**FAIL, "receipts_test.py::sanity": ("passed",)}))
    assert ok


def test_suite_candidates():
    base = R({"a": ("passed",), "b": ("passed",), "c": ("failed", "E", "")})
    pr = R({"a": ("passed",), "b": ("failed", "E", ""), "c": ("failed", "E", "")})
    assert suite_candidates(base, pr) == ["b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_verdict.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'receipts.verdict'`

- [ ] **Step 3: Write `receipts/verdict.py`**

```python
"""Verdict rules. Pure: no I/O, no models. These definitions are the product; don't loosen them."""
import re
from dataclasses import dataclass, field
from enum import Enum


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class
    outcome: str  # passed | failed | error | skipped
    exc: str | None = None
    msg: str = ""


@dataclass
class PytestRun:
    results: dict[str, TestResult] = field(default_factory=dict)
    output: str = ""  # tail of console output, for evidence


class Verdict(str, Enum):
    PROVEN = "PROVEN"
    REFUTED = "REFUTED"
    REGRESSION = "REGRESSION"
    UNPROVEN = "UNPROVEN"
    NO_CHECKABLE_CLAIM = "NO_CHECKABLE_CLAIM"


_ADDR = re.compile(r"0x[0-9a-fA-F]+")


def _signature(r: TestResult) -> str:
    first = (r.msg.strip().splitlines() or [""])[0]
    return f"{r.exc}:{_ADDR.sub('0x?', first)}"


def _failing(run: PytestRun) -> dict[str, str]:
    return {n: _signature(r) for n, r in run.results.items() if r.outcome != "passed"}


def _all_passed(run: PytestRun) -> bool:
    return bool(run.results) and all(r.outcome == "passed" for r in run.results.values())


def repro_check(run: PytestRun) -> tuple[bool, str]:
    """Reproduces the bug iff >=1 test fails with AssertionError and every other test passes."""
    if not run.results:
        return False, "no tests ran (syntax/import/collection error or sandbox error)"
    for n, r in run.results.items():
        if r.outcome != "passed" and not (r.outcome == "failed" and r.exc == "AssertionError"):
            return False, f"{n}: {r.outcome} with {r.exc}: {r.msg[:200]} (only AssertionError failures count)"
    if not _failing(run):
        return False, "every test passed on the unpatched code, so it does not reproduce the bug"
    return True, "fails on unpatched code with AssertionError"


def suite_candidates(base_suite: PytestRun, pr_suite: PytestRun) -> list[str]:
    """Existing tests that passed on base but not in this PR run."""
    return sorted(
        n for n, r in base_suite.results.items()
        if r.outcome == "passed" and pr_suite.results.get(n, TestResult("missing")).outcome != "passed"
    )


def fix_verdict(
    base_runs: list[PytestRun],
    pr_runs: list[PytestRun] | None,
    base_suite: PytestRun | None,
    pr_suites: list[PytestRun] | None,
) -> tuple[Verdict, str]:
    """pr_runs None = patch did not apply. base_suite None = no existing tests to check.

    pr_suites[0] is the full suite on the PR; later entries are reruns of suite_candidates().
    """
    for i, run in enumerate(base_runs, 1):
        ok, why = repro_check(run)
        if not ok:
            return Verdict.UNPROVEN, f"base run {i}/{len(base_runs)} does not reproduce the bug: {why}"
    if len({frozenset(_failing(r)) for r in base_runs}) != 1:
        return Verdict.UNPROVEN, "base runs disagree on which tests fail (flaky)"
    if pr_runs is None:
        return Verdict.UNPROVEN, "patch does not apply to the base commit"

    if all(_all_passed(r) for r in pr_runs):
        if base_suite is not None:
            if not base_suite.results or any(not s.results for s in pr_suites):
                return Verdict.UNPROVEN, "existing test suite could not run"
            broken = [n for n in suite_candidates(base_suite, pr_suites[0])
                      if all(s.results.get(n, TestResult("missing")).outcome != "passed" for s in pr_suites)]
            if broken:
                return Verdict.REGRESSION, f"fixes the claim but breaks {len(broken)} existing test(s): {', '.join(broken[:10])}"
        return Verdict.PROVEN, f"test fails on base and passes on the PR in {len(pr_runs)}/{len(pr_runs)} runs; existing tests hold"

    base_sig = _failing(base_runs[0])
    if all(_failing(r) == base_sig for r in pr_runs):
        return Verdict.REFUTED, "test still fails on the PR with the same assertion as on base"
    return Verdict.UNPROVEN, "PR runs are mixed or fail differently from base"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_verdict.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add receipts/verdict.py tests/test_verdict.py
git commit -m "feat: pure verdict rules with asymmetry (uncertain -> UNPROVEN)"
```

---

### Task 3: Pytest probe plugin + sandbox helpers

**Files:**
- Create: `receipts/pytest_probe.py`, `receipts/sandbox.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: `verdict.PytestRun`, `verdict.TestResult`, `config.SANDBOX_TIMEOUT_S`, `config.SANDBOX_MAX_CONCURRENCY`
- Produces:
  - `sandbox.TEST_PATH = "/testbed/receipts_test.py"`, `sandbox.TEST_ARGS = ["receipts_test.py"]`, `sandbox.ACTIVATE` (shell prefix)
  - `sandbox.text(x: bytes | str | None) -> str`
  - `sandbox.parse_probe_output(stdout: str, stderr: str = "") -> PytestRun`
  - `async sandbox.run_pytest(image, args: list[str], files: dict[str, bytes] | None = None) -> PytestRun` (raises `ValueError` on empty `args`)
  - `async sandbox.apply_patch(image, patch: str) -> image | None`

- [ ] **Step 1: Write the failing tests `tests/test_probe.py`**

```python
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from receipts.sandbox import MARKER, parse_probe_output, run_pytest

PROBE_DIR = Path(__file__).resolve().parent.parent / "receipts"


def test_probe_records_exact_exception_types(tmp_path):
    (tmp_path / "test_x.py").write_text(
        "import pytest\n"
        "def test_assert(): assert 1 == 2\n"
        "def test_value(): raise ValueError('boom')\n"
        "def test_ok(): pass\n"
        "@pytest.mark.skip\n"
        "def test_skip(): pass\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('fixture')\n"
        "def test_setup_err(broken): pass\n"
        "@pytest.mark.parametrize('v', ['a b'])\n"
        "def test_param(v): assert v == 'x'\n"
    )
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONPATH": str(PROBE_DIR), "RECEIPTS_OUT": str(out)}
    subprocess.run([sys.executable, "-m", "pytest", "test_x.py", "-p", "pytest_probe", "-p", "no:cacheprovider", "-q"],
                   cwd=tmp_path, env=env, capture_output=True)
    r = json.loads(out.read_text())
    assert r["test_x.py::test_assert"] == {"outcome": "failed", "exc": "AssertionError", "msg": "assert 1 == 2"}
    assert r["test_x.py::test_value"]["exc"] == "ValueError"
    assert r["test_x.py::test_ok"]["outcome"] == "passed"
    assert r["test_x.py::test_skip"]["outcome"] == "skipped"
    assert r["test_x.py::test_setup_err"] == {"outcome": "error", "exc": "RuntimeError", "msg": "fixture"}
    assert r["test_x.py::test_param[a b]"]["exc"] == "AssertionError"


def test_probe_records_collection_errors(tmp_path):
    (tmp_path / "test_bad.py").write_text("import nonexistent_module_xyz\n")
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONPATH": str(PROBE_DIR), "RECEIPTS_OUT": str(out)}
    subprocess.run([sys.executable, "-m", "pytest", "test_bad.py", "-p", "pytest_probe", "-p", "no:cacheprovider", "-q"],
                   cwd=tmp_path, env=env, capture_output=True)
    r = json.loads(out.read_text())
    assert r["test_bad.py"]["outcome"] == "error"


def test_parse_probe_output():
    stdout = "1 failed\n" + MARKER + '\n{"t.py::a": {"outcome": "failed", "exc": "AssertionError", "msg": "m"}}\n'
    run = parse_probe_output(stdout, "warn")
    assert run.results["t.py::a"].exc == "AssertionError"
    assert "1 failed" in run.output and "warn" in run.output


def test_parse_probe_output_without_marker_is_empty():
    assert parse_probe_output("Killed").results == {}


def test_run_pytest_refuses_empty_targets():
    with pytest.raises(ValueError):
        asyncio.run(run_pytest(None, []))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_probe.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'receipts.sandbox'`

- [ ] **Step 3: Write `receipts/pytest_probe.py`**

```python
"""Pytest plugin, uploaded into the sandbox and loaded with `-p pytest_probe`.

Records every test's outcome and exact exception type to $RECEIPTS_OUT as JSON.
Runs inside old SWE-bench envs: keep it Python 3.6 / old-pytest compatible.
"""
import json
import os

import pytest

_results = {}


def _record(nodeid, outcome, exc, msg):
    prev = _results.get(nodeid)
    if prev is None or prev["outcome"] == "passed":  # first non-pass wins (setup/call/teardown)
        _results[nodeid] = {"outcome": outcome, "exc": exc, "msg": msg[:500]}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    rep = (yield).get_result()
    exc = call.excinfo.typename if call.excinfo else None
    msg = str(call.excinfo.value) if call.excinfo else ""
    if rep.skipped:
        _record(rep.nodeid, "skipped", exc, msg)
    elif rep.failed:
        _record(rep.nodeid, "failed" if rep.when == "call" else "error", exc, msg)
    elif rep.when == "call":
        _record(rep.nodeid, "passed", None, "")


def pytest_collectreport(report):
    if report.failed:
        _record(report.nodeid or "<collection>", "error", "CollectionError", str(report.longrepr)[-500:])


def pytest_sessionfinish(session):
    with open(os.environ.get("RECEIPTS_OUT", "/tmp/receipts_out.json"), "w") as f:
        json.dump(_results, f)


if __name__ == "__main__":  # sandbox entrypoint: targets come from a JSON file to dodge shell quoting
    import sys

    targets = json.load(open("/tmp/receipts_args.json"))
    sys.exit(pytest.main(targets + ["-p", "pytest_probe", "-p", "no:cacheprovider", "-q"]))
```

- [ ] **Step 4: Write `receipts/sandbox.py`**

```python
"""Sandbox helpers: pytest in throwaway forks, patch application. All runs start from a given image."""
import asyncio
import json
from pathlib import Path

from . import config
from .verdict import PytestRun, TestResult

PROBE_SRC = (Path(__file__).parent / "pytest_probe.py").read_bytes()
MARKER = "__RECEIPTS_JSON__"
ACTIVATE = "cd /testbed && . /opt/miniconda3/bin/activate testbed"
TEST_PATH = "/testbed/receipts_test.py"
TEST_ARGS = ["receipts_test.py"]
_sem: asyncio.Semaphore | None = None


def _limit() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.SANDBOX_MAX_CONCURRENCY)
    return _sem


def text(x) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, (bytes, bytearray)) else (x or "")


def parse_probe_output(stdout: str, stderr: str = "") -> PytestRun:
    head, sep, tail = stdout.rpartition(MARKER)
    if not sep:
        return PytestRun(output=(stdout + stderr)[-4000:])
    try:
        raw = json.loads(tail.strip() or "{}")
    except ValueError:
        raw = {}
    return PytestRun({k: TestResult(**v) for k, v in raw.items()}, (head[-3000:] + stderr[-1000:]))


async def run_pytest(image, args: list[str], files: dict[str, bytes] | None = None) -> PytestRun:
    """Run pytest on `args` (nodeids/files, relative to /testbed) in a throwaway fork of `image`."""
    if not args:
        raise ValueError("refusing to run pytest with no targets (would run the whole repo)")
    files = {
        "/tmp/pytest_probe.py": PROBE_SRC,
        "/tmp/receipts_args.json": json.dumps(args).encode(),
        **(files or {}),
    }
    cmd = (
        f"{ACTIVATE} && PYTHONPATH=/tmp:$PYTHONPATH RECEIPTS_OUT=/tmp/receipts_out.json "
        f"python /tmp/pytest_probe.py; echo {MARKER}; cat /tmp/receipts_out.json 2>/dev/null"
    )
    async with _limit():
        try:
            r = await image.run(shell=cmd, files=files, timeout=config.SANDBOX_TIMEOUT_S,
                                truncate_output_at=10 * 1024 * 1024)
        except Exception as e:  # timeout / API error -> empty run -> rules yield UNPROVEN
            return PytestRun(output=f"sandbox error: {type(e).__name__}: {e}")
    return parse_probe_output(text(r.stdout), text(r.stderr))


async def apply_patch(image, patch: str):
    """New image with `patch` applied at /testbed, or None if it does not apply."""
    cmd = "cd /testbed && (git apply -v /tmp/pr.diff || patch --batch --fuzz=5 -p1 -i /tmp/pr.diff)"
    async with _limit():
        r = await image.run(shell=cmd, files={"/tmp/pr.diff": patch.encode()}, disposable=False,
                            timeout=config.SANDBOX_TIMEOUT_S)
    return r if r.exit_code == 0 else None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add receipts/pytest_probe.py receipts/sandbox.py tests/test_probe.py
git commit -m "feat: pytest probe plugin and sandbox run/patch helpers"
```

---

### Task 4: SWE-bench instance loading

**Files:**
- Create: `receipts/swebench.py`
- Test: `tests/test_swebench.py`

**Interfaces:**
- Produces: `Instance(instance_id, repo, problem_statement, gold_patch, pass_to_pass: list[str])`, `load_instance(instance_id) -> Instance` (raises `ValueError`), `async swe_image(sdk, instance_id) -> image`, `PYTEST_REPOS: set[str]`

- [ ] **Step 1: Write the failing tests `tests/test_swebench.py`**

```python
import pytest

from receipts import swebench

ROW = {"instance_id": "psf__requests-1", "repo": "psf/requests", "problem_statement": "bug", "patch": "diff",
       "PASS_TO_PASS": '["t.py::a"]', "test_patch": "HIDDEN", "FAIL_TO_PASS": '["HIDDEN"]', "hints_text": "HIDDEN"}


@pytest.fixture(autouse=True)
def fake_dataset(monkeypatch):
    rows = {"psf__requests-1": ROW, "django__django-1": {**ROW, "instance_id": "django__django-1", "repo": "django/django"}}
    monkeypatch.setattr(swebench, "_dataset", lambda: rows)


def test_load_instance_hides_ground_truth():
    inst = swebench.load_instance("psf__requests-1")
    assert inst.pass_to_pass == ["t.py::a"] and inst.gold_patch == "diff"
    assert "HIDDEN" not in repr(inst)


def test_load_instance_rejects_unknown_id():
    with pytest.raises(ValueError, match="not in SWE-bench Verified"):
        swebench.load_instance("nope__nope-1")


def test_load_instance_rejects_non_pytest_repo():
    with pytest.raises(ValueError, match="pytest"):
        swebench.load_instance("django__django-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_swebench.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `receipts/swebench.py`**

```python
"""SWE-bench Verified instances. Never exposes test_patch, FAIL_TO_PASS or hints (hidden ground truth)."""
import json
from dataclasses import dataclass
from functools import lru_cache

# Repos whose SWE-bench harness runs pytest. django (runtests.py) and sympy (bin/test) are out of MVP scope.
PYTEST_REPOS = {
    "astropy/astropy", "matplotlib/matplotlib", "mwaskom/seaborn", "pallets/flask", "psf/requests",
    "pydata/xarray", "pylint-dev/pylint", "pytest-dev/pytest", "scikit-learn/scikit-learn", "sphinx-doc/sphinx",
}


@dataclass
class Instance:
    instance_id: str
    repo: str
    problem_statement: str
    gold_patch: str
    pass_to_pass: list[str]


@lru_cache(maxsize=1)
def _dataset() -> dict:
    from datasets import load_dataset

    return {r["instance_id"]: r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")}


def load_instance(instance_id: str) -> Instance:
    row = _dataset().get(instance_id)
    if row is None:
        raise ValueError(f"{instance_id} is not in SWE-bench Verified")
    if row["repo"] not in PYTEST_REPOS:
        raise ValueError(f"{row['repo']} does not use pytest; the MVP is pytest-only")
    return Instance(row["instance_id"], row["repo"], row["problem_statement"], row["patch"],
                    json.loads(row["PASS_TO_PASS"]))


async def swe_image(sdk, instance_id: str):
    """Preloaded SWE-bench env image (repo at /testbed, conda env 'testbed'). oci() reuses it if already imported."""
    name = instance_id.replace("__", "_1776_").lower()
    return await sdk.images.oci(f"docker://docker.io/swebench/sweb.eval.x86_64.{name}:latest")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add receipts/swebench.py tests/test_swebench.py
git commit -m "feat: SWE-bench Verified loader (pytest repos only, ground truth hidden)"
```

---

### Task 5: CLI smoke (models + sandbox + SWE image)

**Files:**
- Create: `receipts/__main__.py`

**Interfaces:**
- Consumes: `config.llm`, `config.contree`, `swebench.swe_image`, `sandbox.run_pytest`, `sandbox.ACTIVATE`, `sandbox.TEST_PATH`, `sandbox.TEST_ARGS`, `sandbox.text`
- Produces: `python -m receipts smoke [--instance ID]`; `main()` with a `run` subcommand filled in Task 7.

- [ ] **Step 1: Write `receipts/__main__.py`**

```python
"""CLI.  python -m receipts smoke [--instance ID]
         python -m receipts run INSTANCE_ID [--patch gold|none|FILE]"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config


async def smoke(instance_id: str | None) -> None:
    for role, model in config.MODELS.items():
        r = await config.llm(role).ainvoke("Reply with the single word: ok")
        print(f"model {role:<10} {model}: {str(r.content).strip()[:40]!r}")

    from .sandbox import ACTIVATE, TEST_ARGS, TEST_PATH, run_pytest, text

    sdk = config.contree()
    r = await (await sdk.images.use("busybox:latest")).run(shell="echo sandbox-ok")
    print(f"sandbox: exit={r.exit_code} {text(r.stdout).strip()}")
    if not instance_id:
        return

    from .swebench import swe_image

    img = await swe_image(sdk, instance_id)
    r = await img.run(shell=f"{ACTIVATE} && python --version && python -m pytest --version; git log -1 --oneline",
                      timeout=config.SANDBOX_TIMEOUT_S)
    print(f"swe image {instance_id}: exit={r.exit_code}\n{text(r.stdout)}{text(r.stderr)}")
    run = await run_pytest(img, TEST_ARGS, {TEST_PATH: b"def test_probe():\n    assert 1 == 2\n"})
    print(f"probe (expect failed/AssertionError): {run.results or run.output}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252; model output isn't
    ap = argparse.ArgumentParser(prog="receipts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke", help="check models, sandbox, and optionally one SWE-bench image")
    s.add_argument("--instance")
    a = ap.parse_args()
    if a.cmd == "smoke":
        asyncio.run(smoke(a.instance))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run smoke without instance**

Run: `python -m receipts smoke`
Expected: three `model ...: 'ok'` lines, then `sandbox: exit=0 sandbox-ok`

- [ ] **Step 3: Run smoke with instance**

Run: `python -m receipts smoke --instance psf__requests-1142`
Expected: python + pytest versions and a git commit line; `probe (expect failed/AssertionError): {'receipts_test.py::test_probe': TestResult(outcome='failed', exc='AssertionError', ...)}`.
If the image path or conda path differs, fix `swe_image` / `ACTIVATE` here (the spec lists these as smoke-test unknowns) and re-run.

- [ ] **Step 4: Commit**

```bash
git add receipts/__main__.py
git commit -m "feat: smoke CLI for models, sandbox, SWE-bench image"
```

---

### Task 6: Blind test writer (Deepagents)

**Files:**
- Create: `receipts/writer.py`

**Interfaces:**
- Consumes: `contree_sdk.langchain.sandbox.ContreeSandbox`, `config.llm("writer")`, `config.MAX_TEST_ATTEMPTS`, `sandbox.run_pytest`, `sandbox.TEST_PATH`, `sandbox.TEST_ARGS`, `sandbox.text`, `verdict.repro_check`
- Produces: `WriterResult(test_code: str | None, base_run: PytestRun | None, attempts: int, reason: str, queries: list[str])`, `async write_test(issue: str, base_image) -> WriterResult`

- [ ] **Step 1: Write `receipts/writer.py`**

```python
"""Blind test writer: a Deepagents agent (Nemotron Lightning) working inside a Contree sandbox.

Integrity rule D2: the agent sees the issue and the unpatched repo only, never the PR's patch.
Acceptance is decided by code (repro_check on a clean fork), not by the agent.
"""
from dataclasses import dataclass, field

from contree_sdk.langchain.sandbox import ContreeSandbox
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langchain_tavily import TavilySearch

from . import config
from .sandbox import TEST_ARGS, TEST_PATH, run_pytest, text
from .verdict import PytestRun, repro_check

CODE_HOSTS = ["github.com", "gitlab.com", "bitbucket.org", "githubusercontent.com", "sourcegraph.com",
              "gitee.com", "codeberg.org", "huggingface.co", "swebench.com"]

PROMPT = f"""You write ONE pytest file that reproduces a reported bug in the repository at /testbed.

Rules:
- You get only the issue and the current (buggy) code. Never look for, write, or apply a fix.
- Do not edit repository files. Create only {TEST_PATH}.
- Tests must assert the behaviour the issue says is CORRECT, so they FAIL on the current code with an
  AssertionError (use plain `assert`). Import errors, other exceptions, or skips do not count.
- Keep it small: 1-3 focused test functions, no network access, no new dependencies.
- Run it with: cd /testbed && . /opt/miniconda3/bin/activate testbed && python -m pytest receipts_test.py -q
- When it fails for the right reason, call submit_test. If rejected, read the reason, fix the test, submit again.
- Stop as soon as submit_test answers ACCEPTED.
- docs_search is for library/API documentation only.
"""


@dataclass
class WriterResult:
    test_code: str | None = None
    base_run: PytestRun | None = None
    attempts: int = 0
    reason: str = "writer never submitted a test"
    queries: list[str] = field(default_factory=list)


async def write_test(issue: str, base_image) -> WriterResult:
    out = WriterResult()
    backend = ContreeSandbox(base_image.session())
    tavily = TavilySearch(max_results=5, exclude_domains=CODE_HOSTS)

    @tool
    async def docs_search(query: str) -> str:
        """Search library/API documentation on the web. Code hosting sites are excluded."""
        out.queries.append(query)
        return str(await tavily.ainvoke({"query": query}))[:6000]

    @tool
    async def submit_test() -> str:
        """Submit /testbed/receipts_test.py. It is re-run on a clean copy of the repo and checked."""
        if out.test_code is not None:
            return "ACCEPTED already. Stop."
        if out.attempts >= config.MAX_TEST_ATTEMPTS:
            return "REJECTED: attempt limit reached. Stop now."
        out.attempts += 1
        [dl] = await backend.adownload_files([TEST_PATH])
        if dl.error or not dl.content:
            out.reason = f"could not read {TEST_PATH}: {dl.error}"
            return f"REJECTED: {out.reason}"
        code = text(dl.content)
        run = await run_pytest(base_image, TEST_ARGS, {TEST_PATH: code.encode()})
        ok, out.reason = repro_check(run)
        if ok:
            out.test_code, out.base_run = code, run
            return "ACCEPTED. Stop now."
        return f"REJECTED: {out.reason}\n--- pytest output (tail) ---\n{run.output[-2500:]}"

    agent = create_deep_agent(model=config.llm("writer"), tools=[docs_search, submit_test],
                              system_prompt=PROMPT, backend=backend)
    try:
        await agent.ainvoke({"messages": [{"role": "user", "content": f"Issue:\n\n{issue}"}]},
                            config={"recursion_limit": 150, "run_name": "blind_test_writer"})
    except Exception as e:  # recursion limit / model error: keep whatever was accepted
        out.reason = f"{out.reason}; agent stopped: {type(e).__name__}: {e}"[:1000]
    return out
```

- [ ] **Step 2: Verify imports**

Run: `python -c "import receipts.writer"`
Expected: no output, exit 0. (Live behaviour is verified end to end in Task 7.)

- [ ] **Step 3: Commit**

```bash
git add receipts/writer.py
git commit -m "feat: blind Deepagents test writer on Contree sandbox"
```

---

### Task 7: Engine + `run` CLI + end-to-end check

**Files:**
- Create: `receipts/engine.py`
- Modify: `receipts/__main__.py` (add `run` subcommand)

**Interfaces:**
- Consumes: everything above.
- Produces: `async engine.check(inst: Instance, patch: str | None) -> dict` (evidence), `engine.changed_files(patch) -> list[str]`

- [ ] **Step 1: Write `receipts/engine.py`**

```python
"""Orchestrator: classify -> blind test -> forks -> verdict rules -> second opinion before REFUTED."""
import asyncio
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal

from langchain_core.callbacks import get_usage_metadata_callback
from langsmith import traceable
from pydantic import BaseModel

from . import config
from .sandbox import TEST_ARGS, TEST_PATH, apply_patch, run_pytest
from .swebench import Instance, swe_image
from .verdict import PytestRun, Verdict, fix_verdict, suite_candidates
from .writer import write_test


class Claim(BaseModel):
    kind: Literal["fix", "dependency", "none"]
    claim: str


class Judgement(BaseModel):
    faithful: bool
    reason: str


def changed_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+)", patch, flags=re.M)


@traceable(name="classify_claim")
async def classify(issue: str, patch: str) -> Claim:
    prompt = (
        "Classify this pull request's claim. fix = claims to fix a bug; dependency = only bumps a dependency "
        "version; none = feature, docs, style or refactor.\n\n"
        f"Linked issue:\n{issue[:6000]}\n\nFiles changed: {', '.join(changed_files(patch)) or '(none)'}"
    )
    return await config.llm("classifier").with_structured_output(Claim, method="function_calling").ainvoke(prompt)


@traceable(name="second_opinion")
async def judge(issue: str, test_code: str, base_output: str) -> Judgement:
    prompt = (
        "A pull request claims to fix the issue below. A test written from the issue alone still fails on the PR "
        "exactly as it fails on the unpatched code. Before we tell the contributor their PR does not fix the "
        "issue, check the test itself. Answer faithful=true ONLY if the test asserts exactly the behaviour the "
        "issue asks for, with no extra or stricter expectations and no mistakes of its own. If in doubt, false.\n\n"
        f"Issue:\n{issue[:8000]}\n\nTest:\n```python\n{test_code}\n```\n\n"
        f"Failure on unpatched code:\n{base_output[-3000:]}"
    )
    return await config.llm("judge").with_structured_output(Judgement, method="function_calling").ainvoke(prompt)


def _summary(run: PytestRun) -> dict:
    bad = {n: asdict(r) for n, r in run.results.items() if r.outcome != "passed"}
    return {"tests": len(run.results), "not_passed": bad, "output_tail": run.output[-1500:]}


async def _times(n: int, make) -> list[PytestRun]:
    return list(await asyncio.gather(*(make() for _ in range(n))))


@traceable(name="receipts_check")
async def check(inst: Instance, patch: str | None) -> dict:
    """patch=None means a PR that changes nothing (known-wrong control)."""
    t0 = time.monotonic()
    ev: dict = {"instance_id": inst.instance_id, "repo": inst.repo, "patch_files": changed_files(patch or ""),
                "started_at": datetime.now(timezone.utc).isoformat(), "models": config.MODELS}
    with get_usage_metadata_callback() as usage:
        try:
            v, reason = await _pipeline(inst, patch, ev)
        except Exception as e:  # asymmetry rule: anything unexpected is UNPROVEN, never REFUTED
            v, reason = Verdict.UNPROVEN, f"pipeline error: {type(e).__name__}: {e}"
    ev.update(verdict=v.value, reason=reason, seconds=round(time.monotonic() - t0, 1), tokens=usage.usage_metadata)
    return ev


async def _pipeline(inst: Instance, patch: str | None, ev: dict) -> tuple[Verdict, str]:
    claim = await classify(inst.problem_statement, patch or "")
    ev["claim"] = claim.model_dump()
    if claim.kind != "fix":
        return Verdict.NO_CHECKABLE_CLAIM, f"classified as '{claim.kind}': nothing to check"

    base = await swe_image(config.contree(), inst.instance_id)
    w = await write_test(inst.problem_statement, base)
    ev["writer"] = {"attempts": w.attempts, "reason": w.reason, "docs_queries": w.queries, "test_code": w.test_code}
    if w.test_code is None:
        return Verdict.UNPROVEN, f"no valid reproducing test after {w.attempts} attempt(s): {w.reason}"

    test = {TEST_PATH: w.test_code.encode()}
    pr = base if patch is None else await apply_patch(base, patch)
    n, p2p = config.VERDICT_RUNS, inst.pass_to_pass
    jobs = [_times(n, lambda: run_pytest(base, TEST_ARGS, test))]
    if pr is not None:
        jobs.append(_times(n, lambda: run_pytest(pr, TEST_ARGS, test)))
        if p2p:
            jobs += [run_pytest(base, p2p), run_pytest(pr, p2p)]
    res = await asyncio.gather(*jobs)
    base_runs = res[0]
    pr_runs = res[1] if pr is not None else None
    base_suite, pr_suites = (res[2], [res[3]]) if len(res) == 4 else (None, None)
    if base_suite is not None:
        cands = suite_candidates(base_suite, pr_suites[0])
        if cands:  # rerun only what looks broken, to rule out flakes
            pr_suites += await _times(n - 1, lambda: run_pytest(pr, cands))

    ev["forks"] = {
        "base_with_test": [_summary(r) for r in base_runs],
        "pr_with_test": [_summary(r) for r in pr_runs] if pr_runs else "patch did not apply",
        "base_suite": _summary(base_suite) if base_suite else None,
        "pr_suite": [_summary(r) for r in pr_suites] if pr_suites else None,
    }
    v, reason = fix_verdict(base_runs, pr_runs, base_suite, pr_suites)
    if v is Verdict.REFUTED:
        j = await judge(inst.problem_statement, w.test_code, base_runs[0].output)
        ev["second_opinion"] = j.model_dump()
        if not j.faithful:
            return Verdict.UNPROVEN, f"second opinion doubts the test: {j.reason}"
    return v, reason
```

- [ ] **Step 2: Add `run` subcommand to `receipts/__main__.py`**

In `main()`, after the `smoke` parser, add:

```python
    r = sub.add_parser("run", help="check one SWE-bench Verified instance")
    r.add_argument("instance_id")
    r.add_argument("--patch", default="gold", help="gold | none | path to a .diff file")
```

and replace the dispatch (`if a.cmd == "smoke": ...`) with:

```python
    if a.cmd == "smoke":
        return asyncio.run(smoke(a.instance))

    from .engine import check
    from .swebench import load_instance

    try:
        inst = load_instance(a.instance_id)
    except ValueError as e:
        sys.exit(f"error: {e}")
    patch = {"gold": inst.gold_patch, "none": None}[a.patch] if a.patch in ("gold", "none") else Path(a.patch).read_text()
    label = a.patch if a.patch in ("gold", "none") else Path(a.patch).stem
    ev = asyncio.run(check(inst, patch))
    config.RUNS_DIR.mkdir(exist_ok=True)
    out = config.RUNS_DIR / f"{inst.instance_id}-{label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")
    tokens = sum(u.get("total_tokens", 0) for u in (ev.get("tokens") or {}).values())
    print(f"\n{ev['verdict']}: {ev['reason']}\n  {ev['seconds']}s, {tokens} tokens\n  evidence: {out}")
```

- [ ] **Step 3: Run offline tests still pass**

Run: `python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 4: E2E, gold patch**

Run: `python -m receipts run psf__requests-1142 --patch gold`
Expected: `PROVEN: ...` and an evidence file in `runs/`. If UNPROVEN, read `writer.reason` and `forks` in the evidence JSON and fix the root cause (do not loosen rules).

- [ ] **Step 5: E2E, no-op patch (known wrong)**

Run: `python -m receipts run psf__requests-1142 --patch none`
Expected: `REFUTED` or `UNPROVEN`; never `PROVEN`.

- [ ] **Step 6: Commit**

```bash
git add receipts/engine.py receipts/__main__.py
git commit -m "feat: fix-engine orchestrator and run CLI"
```
