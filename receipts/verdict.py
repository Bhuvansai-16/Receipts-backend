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


_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")  # object addresses only; short hex like '0x1f' is real data


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
        if r.exc == "CollectionError" and "in <module>" in r.msg and "AssertionError" in r.msg:
            return False, (f"{n}: an assert at module level ran while pytest imported the file, so no test ran. "
                           "Put every assert inside a def test_...() function.")
        if r.outcome != "passed" and not (r.outcome == "failed" and r.exc == "AssertionError"):
            return False, f"{n}: {r.outcome} with {r.exc}: {r.msg[:200]} (only AssertionError failures count)"
    if not _failing(run):
        return False, "every test passed on the unpatched code, so it does not reproduce the bug"
    return True, "fails on unpatched code with AssertionError"


def _passed(run: PytestRun, nodeid: str) -> bool:
    return run.results.get(nodeid, TestResult("missing")).outcome == "passed"


def restrict(run: PytestRun, ids: list[str]) -> PytestRun:
    """Keep only the listed test ids (e.g. PASS_TO_PASS out of a file-level run)."""
    keep = set(ids)
    return PytestRun({n: r for n, r in run.results.items() if n in keep}, run.output)


def suite_candidates(base_suite: PytestRun, pr_suite: PytestRun) -> list[str]:
    """Existing tests that passed on base but not in this PR run."""
    return sorted(n for n in base_suite.results if _passed(base_suite, n) and not _passed(pr_suite, n))


def fix_verdict(
    base_runs: list[PytestRun],
    pr_runs: list[PytestRun] | None,
    base_suites: list[PytestRun] | None,
    pr_suites: list[PytestRun] | None,
) -> tuple[Verdict, str]:
    """pr_runs None = patch did not apply. base_suites None = no existing tests to check.

    base_suites / pr_suites: [full suite run, *reruns of suite_candidates()] on base / PR. Reruns happen on
    both sides at the same time, so an outage (network, flake) after the first base run can't look like a
    regression: a test counts as broken only if it passes in every base run and fails in every PR run.
    """
    for i, run in enumerate(base_runs, 1):
        ok, why = repro_check(run)
        if not ok:
            return Verdict.UNPROVEN, f"base run {i}/{len(base_runs)} does not reproduce the bug: {why}"
    if len({frozenset(_failing(r)) for r in base_runs}) != 1:
        return Verdict.UNPROVEN, "base runs disagree on which tests fail (flaky)"
    if pr_runs is None:
        return Verdict.UNPROVEN, "patch does not apply to the base commit"

    repro = _failing(base_runs[0]).keys()
    if all(_all_passed(r) for r in pr_runs):
        if not all(repro <= r.results.keys() for r in pr_runs):
            return Verdict.UNPROVEN, "the blind test did not run on the PR (deselected or not collected)"
        if base_suites is not None:
            if any(not s.results for s in base_suites + pr_suites):
                return Verdict.UNPROVEN, "existing test suite could not run"
            broken = [n for n in suite_candidates(base_suites[0], pr_suites[0])
                      if all(_passed(s, n) for s in base_suites) and not any(_passed(s, n) for s in pr_suites)]
            if broken:
                return Verdict.REGRESSION, f"fixes the claim but breaks {len(broken)} existing test(s): {', '.join(broken[:10])}"
        return Verdict.PROVEN, f"test fails on base and passes on the PR in {len(pr_runs)}/{len(pr_runs)} runs; existing tests hold"

    base_sig = _failing(base_runs[0])
    if all(_failing(r) == base_sig for r in pr_runs):
        return Verdict.REFUTED, "test still fails on the PR with the same assertion as on base"
    return Verdict.UNPROVEN, "PR runs are mixed or fail differently from base"
