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
from .sandbox import TEST_ARGS, TEST_PATH, apply_patch, run_pytest, suite_files
from .swebench import Instance, base_image
from .verdict import PytestRun, Verdict, fix_verdict, restrict, suite_candidates
from .writer import write_test


class Claim(BaseModel):
    kind: Literal["fix", "dependency", "none"]
    claim: str


class Judgement(BaseModel):
    faithful: bool
    reason: str


def changed_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+)", patch, flags=re.M)


def claim_prompt(issue: str, patch: str) -> str:
    files = changed_files(patch)
    return (
        "Classify this pull request's claim. fix = claims to fix a bug; dependency = only bumps a dependency "
        "version; none = feature, docs, style or refactor.\n\n"
        f"Linked issue:\n{issue[:6000]}" + (f"\n\nFiles changed: {', '.join(files)}" if files else "")
    )


@traceable(name="classify_claim")
async def classify(issue: str, patch: str) -> Claim:
    llm = config.llm("classifier").with_structured_output(Claim, method="function_calling")
    return await llm.ainvoke(claim_prompt(issue, patch))


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

    base = await base_image(inst.instance_id)
    w = await write_test(inst.problem_statement, base)
    ev["writer"] = {"attempts": w.attempts, "reason": w.reason, "docs_queries": w.queries,
                    "test_code": w.test_code, "tool_log": w.log}
    if w.test_code is None:
        return Verdict.UNPROVEN, f"no valid reproducing test after {w.attempts} attempt(s): {w.reason}"

    test = {TEST_PATH: w.test_code.encode()}
    pr = base if patch is None else await apply_patch(base, patch)
    n, p2p = config.VERDICT_RUNS, inst.pass_to_pass
    files = suite_files(p2p)  # by file: one PASS_TO_PASS id missing at base would abort a nodeid run
    jobs = [_times(n, lambda: run_pytest(base, TEST_ARGS, test))]
    if pr is not None:
        jobs.append(_times(n, lambda: run_pytest(pr, TEST_ARGS, test)))
        if files:
            jobs += [run_pytest(base, files), run_pytest(pr, files)]
    res = await asyncio.gather(*jobs)
    base_runs = res[0]
    pr_runs = res[1] if pr is not None else None
    base_suites, pr_suites = ([restrict(res[2], p2p)], [res[3]]) if len(res) == 4 else (None, None)
    if base_suites is not None:
        cands = suite_candidates(base_suites[0], pr_suites[0])
        if cands:  # rerun what looks broken on both sides at once, to rule out flakes and outages
            b, p = await asyncio.gather(_times(n - 1, lambda: run_pytest(base, cands)),
                                        _times(n - 1, lambda: run_pytest(pr, cands)))
            base_suites += b
            pr_suites += p

    ev["forks"] = {
        "base_with_test": [_summary(r) for r in base_runs],
        "pr_with_test": [_summary(r) for r in pr_runs] if pr_runs else "patch did not apply",
        "base_suite": [_summary(r) for r in base_suites] if base_suites else None,
        "pr_suite": [_summary(r) for r in pr_suites] if pr_suites else None,
    }
    v, reason = fix_verdict(base_runs, pr_runs, base_suites, pr_suites)
    if v is Verdict.REFUTED:
        j = await judge(inst.problem_statement, w.test_code, base_runs[0].output)
        ev["second_opinion"] = j.model_dump()
        if not j.faithful:
            return Verdict.UNPROVEN, f"second opinion doubts the test: {j.reason}"
    return v, reason
