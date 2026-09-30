"""Orchestrator: classify -> blind test -> forks -> verdict rules -> second opinion before REFUTED."""
import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from langchain_core.callbacks import get_usage_metadata_callback
from langsmith import traceable
from pydantic import BaseModel

from . import config, research
from .sandbox import TEST_ARGS, TEST_PATH, apply_patch, run_pytest, suite_files
from .swebench import Instance
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
        "Classify this pull request's claim.\n"
        "fix = the linked issue reports behaviour as wrong, broken or unwanted and the PR claims to change it "
        "(even if the issue also suggests an option or alternative);\n"
        "dependency = the PR only bumps a dependency version;\n"
        "none = new feature, docs, style or refactor, with nothing reported as wrong.\n\n"
        f"Linked issue:\n{issue[:6000]}" + (f"\n\nFiles changed: {', '.join(files)}" if files else "")
    )


def majority(votes: list[Claim]) -> Claim:
    """Most common kind wins; a three-way tie goes to the first vote."""
    kind = Counter(v.kind for v in votes).most_common(1)[0][0]
    return next(v for v in votes if v.kind == kind)


@traceable(name="classify_claim")
async def classify(issue: str, patch: str) -> Claim:
    # A single Nano answer once labelled a real bug fix "none", which silently skips the check: vote of 3.
    llm = config.llm("classifier").with_structured_output(Claim, method="function_calling")
    return majority(list(await asyncio.gather(*(llm.ainvoke(claim_prompt(issue, patch)) for _ in range(3)))))


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
    return list(await asyncio.gather(*(make(i) for i in range(n))))


def _passed(run: PytestRun) -> bool:
    return bool(run.results) and all(r.outcome == "passed" for r in run.results.values())


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def safe_run_id(run_id: str) -> bool:
    """Run ids become file names (and arrive over HTTP): letters, digits, _ . - only, no leading dot."""
    return bool(_SAFE_RUN_ID.match(run_id))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id(instance_id: str, label: str, taken=()) -> str:
    """<instance>-<label>-<YYYYmmdd-HHMMSS>, suffixed -2, -3... if that id is already used."""
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-") or "patch"
    base = f"{instance_id}-{label}-{datetime.now():%Y%m%d-%H%M%S}"
    run_id, n = base, 1
    while run_id in taken or (config.RUNS_DIR / f"{run_id}.json").exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def save_evidence(ev: dict, run_id: str) -> Path:
    """Write the evidence JSON to runs/<run_id>.json (shared by CLI and server)."""
    if not safe_run_id(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    config.RUNS_DIR.mkdir(exist_ok=True)
    path = config.RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")
    return path


@traceable(name="receipts_check")
async def check(inst: Instance, patch: str | None, emit=None) -> dict:
    """patch=None means a PR that changes nothing (known-wrong control).

    emit(type, data) is called as each stage lands (the web UI streams these); events are also
    stored in the evidence as ev["events"] so a finished run replays identically.
    """
    t0 = time.monotonic()
    ev: dict = {"instance_id": inst.instance_id, "repo": inst.repo, "patch_files": changed_files(patch or ""),
                "started_at": now_iso(), "models": config.MODELS, "events": []}

    def say(type_: str, data: dict | None = None) -> None:
        ev["events"].append({"type": type_, "t": round(time.monotonic() - t0, 1), "data": data or {}})
        if emit:
            emit(type_, data or {})

    with get_usage_metadata_callback() as usage:
        try:
            v, reason = await _pipeline(inst, patch, ev, say)
        except Exception as e:  # asymmetry rule: anything unexpected is UNPROVEN, never REFUTED
            v, reason = Verdict.UNPROVEN, f"pipeline error: {type(e).__name__}: {e}"
    ev.update(verdict=v.value, reason=reason, seconds=round(time.monotonic() - t0, 1), tokens=usage.usage_metadata)
    say("verdict", {"verdict": v.value, "reason": reason, "seconds": ev["seconds"],
                    "tokens": sum(u.get("total_tokens", 0) for u in (ev["tokens"] or {}).values())})
    say("done")
    return ev


async def _pipeline(inst: Instance, patch: str | None, ev: dict, say) -> tuple[Verdict, str]:
    # The environment doesn't depend on the claim, so it builds while the claim is classified (~10 s saved).
    # ponytail: a PR with nothing to check pays for a few seconds of an abandoned build.
    env = asyncio.ensure_future(inst.base_image())
    try:
        claim = await classify(inst.problem_statement, patch or "")
    except BaseException:
        env.cancel()
        raise
    ev["claim"] = claim.model_dump()
    say("claim", ev["claim"])
    if claim.kind != "fix":
        env.cancel()
        return Verdict.NO_CHECKABLE_CLAIM, f"classified as '{claim.kind}': nothing to check"

    brief_task = asyncio.ensure_future(research.research(inst.repo, inst.problem_statement))
    base = await env
    say("env_ready")
    brief = await brief_task
    ev["research"] = {"queries": brief.queries, "sources": brief.sources}
    say("research", {"sources": len(brief.sources)})
    w = await write_test(inst.problem_statement, base, say, brief=brief.for_writer())
    ev["writer"] = {"attempts": w.attempts, "reason": w.reason,
                    "test_code": w.test_code, "scope_check": getattr(w, "scope", ""), "tool_log": w.log,
                    "submissions": getattr(w, "submissions", [])}
    if w.test_code is None:
        return Verdict.UNPROVEN, f"no valid reproducing test after {w.attempts} attempt(s): {w.reason}"
    say("test_accepted", {"attempts": w.attempts})

    test = {TEST_PATH: w.test_code.encode()}
    pr = base if patch is None else await apply_patch(base, patch)
    n, p2p = config.VERDICT_RUNS, inst.pass_to_pass
    # by file: one PASS_TO_PASS id missing at base would abort a nodeid run. Real repos name files directly.
    files = getattr(inst, "suite", None) or suite_files(p2p)
    async def fork(side: str, image, i: int) -> PytestRun:
        run = await run_pytest(image, TEST_ARGS, test)
        failing = [r.msg for r in run.results.values() if r.outcome != "passed"]
        say("fork", {"side": side, "n": i + 1, "passed": _passed(run), "message": (failing or [""])[0][:300]})
        return run

    jobs = [_times(n, lambda i: fork("base", base, i))]
    if pr is not None:
        jobs.append(_times(n, lambda i: fork("pr", pr, i)))
        if files:
            jobs += [run_pytest(base, files), run_pytest(pr, files)]
    res = await asyncio.gather(*jobs)
    base_runs = res[0]
    pr_runs = res[1] if pr is not None else None
    base_suites, pr_suites = ([restrict(res[2], p2p) if p2p else res[2]], [res[3]]) if len(res) == 4 else (None, None)
    if base_suites is not None:
        cands = suite_candidates(base_suites[0], pr_suites[0])
        if cands:  # rerun what looks broken on both sides at once, to rule out flakes and outages
            b, p = await asyncio.gather(_times(n - 1, lambda i: run_pytest(base, cands)),
                                        _times(n - 1, lambda i: run_pytest(pr, cands)))
            base_suites += b
            pr_suites += p
        say("suite", {"base_passed": sum(r.outcome == "passed" for r in base_suites[0].results.values()),
                      "base_total": len(base_suites[0].results), "pr_failed": len(cands)})

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
        say("second_opinion", ev["second_opinion"])
        if not j.faithful:
            return Verdict.UNPROVEN, f"second opinion doubts the test: {j.reason}"
    return v, reason
