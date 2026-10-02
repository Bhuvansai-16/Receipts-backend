"""Orchestrator: classify -> blind test -> forks -> verdict rules -> second opinion before REFUTED."""
import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import openai
from langchain_core.callbacks import get_usage_metadata_callback
from langsmith import traceable
from pydantic import BaseModel

from . import config, research
from .sandbox import TEST_ARGS, TEST_PATH, apply_patch, run_pytest, suite_files, text_part
from .swebench import Instance
from .verdict import (MIXED, PytestRun, Verdict, fix_verdict, partial_fix, repro_check, reproduces, restrict,
                      suite_candidates)
from .writer import WriterResult, blind_workspace, retry_history, stated_cases, write_test

log = logging.getLogger("uvicorn.error")


class Claim(BaseModel):
    kind: Literal["fix", "dependency", "none"]
    claim: str


class Judgement(BaseModel):
    faithful: bool
    reason: str


def changed_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+)", patch, flags=re.M)


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


def judge_prompt(issue: str, test_code: str, base_output: str) -> str:
    # Eval baseline: 7 of 79 real fixes were refuted by tests this check passed. Some failed on an unrelated
    # environment error (pylint #4970, #6386), others asserted an internal detail (matplotlib #23314:
    # ax.patch visibility, where the fix skips drawing) or a parameter name the issue never fixed.
    return (
        "A pull request claims to fix the issue below. A test written from the issue alone still fails on the PR "
        "exactly as it fails on the unpatched code. Before we tell the contributor their PR does not fix the "
        "issue, check the test itself. Answer faithful=true ONLY if all of these hold:\n"
        "1. The failure on unpatched code shows the bug the issue reports. Not an unrelated error: a broken "
        "environment, a missing dependency or plugin, a crash before the behaviour is checked.\n"
        "2. The test checks the behaviour the issue asks for through what a user can observe. Not an internal "
        "detail (a private attribute, a helper object's state) or a name or signature the issue does not fix, "
        "which a correct fix could do differently.\n"
        "3. No extra or stricter expectations than the issue, and no mistakes of its own.\n"
        "If in doubt, false.\n\n"
        f"Issue:\n{issue[:8000]}\n\nTest:\n```python\n{test_code}\n```\n\n"
        f"Failure on unpatched code:\n{base_output[-3000:]}"
    )


def unanimous(answers: list[Judgement | None]) -> Judgement:
    """The first doubt, or the trust all answers share. A split is doubt: UNPROVEN, never REFUTED."""
    for a in answers:
        if a is None or not a.faithful:
            return a or Judgement(faithful=False, reason="the second opinion gave no answer")
    return answers[0]


@traceable(name="second_opinion")
async def judge(issue: str, test_code: str, base_output: str) -> Judgement:
    # Asked 3 times: one answer flipped either way on replays of the same evidence (eval baseline)
    llm = config.llm("judge").with_structured_output(Judgement, method="function_calling")
    prompt = judge_prompt(issue, test_code, base_output)
    return unanimous(list(await asyncio.gather(*(llm.ainvoke(prompt) for _ in range(3)))))


@traceable(name="second_opinion_mixed")
async def judge_mixed(issue: str, test_code: str, pr_output: str) -> Judgement:
    """Explains a mixed result; never changes it. Is the check still failing with the PR one the issue asks for
    (the change misses part of it), or is the test wrong (sympy #16: it expected an evaluated -x - 2 where the
    issue asks for an unevaluated -(x + 2))? If in doubt the test is doubted: nothing is said against the PR."""
    prompt = (
        "A pull request claims to fix the issue below. A test written from the issue alone fails on the unpatched "
        "code. With the pull request applied, part of the test passes and the failure below remains exactly as "
        "on the unpatched code. Answer faithful=true ONLY if the failing assertion checks exactly what the issue "
        "asks for, so the change misses part of the issue. Answer faithful=false if the assertion itself is wrong: "
        "an expected value the issue doesn't ask for, built differently from the issue's expectation, or a mistake "
        "in the test. If in doubt, false. Say in reason which assertion and why, in one or two sentences.\n\n"
        f"Issue:\n{issue[:8000]}\n\nTest:\n```python\n{test_code}\n```\n\n"
        f"Failure with the pull request:\n{pr_output[-3000:]}"
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
async def check(inst: Instance, patch: str | None, emit=None, *, tests=None, run_id: str | None = None) -> dict:
    """patch=None means a PR that changes nothing (known-wrong control).

    tests: where reusable blind tests are kept (the run store); None, as in the CLI and evaluations, always
    writes a new one. run_id names this check when it keeps its test.

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
            v, reason = await _pipeline(inst, patch, ev, say, tests, run_id)
        except openai.APIError as e:  # a model provider outage (its client already retried): say so plainly
            v, reason = Verdict.UNPROVEN, f"a model was unavailable: {type(e).__name__}: {e}"
        except Exception as e:  # asymmetry rule: anything unexpected is UNPROVEN, never REFUTED
            v, reason = Verdict.UNPROVEN, f"pipeline error: {type(e).__name__}: {e}"
    ev.update(verdict=v.value, reason=reason, seconds=round(time.monotonic() - t0, 1), tokens=usage.usage_metadata)
    say("verdict", {"verdict": v.value, "reason": reason, "seconds": ev["seconds"],
                    "tokens": sum(u.get("total_tokens", 0) for u in (ev["tokens"] or {}).values())})
    say("done")
    return ev


def _drop(*tasks) -> None:
    """Stop work nobody will wait for; finished work's failure is already part of the result."""
    for t in tasks:
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


async def _pipeline(inst: Instance, patch: str | None, ev: dict, say, tests=None, run_id=None) -> tuple[Verdict, str]:
    # The environment doesn't depend on the claim, so it builds while the claim is classified (~10 s saved);
    # without a stored test, so does everything the writer needs.
    # ponytail: a PR with nothing to check pays for a few seconds of abandoned build and setup.
    env = asyncio.ensure_future(inst.base_image())
    key = blind_test_key(inst)
    setup: list = []  # grown in place, so the finally also drops setup started later (a stale stored test)
    try:
        stored = await _stored_test(tests, key)
        if not stored:
            setup += _writer_setup(inst, env)
        return await _checked(inst, patch, ev, say, tests, run_id, key, env, stored, setup)
    finally:
        _drop(env, *setup)


async def _checked(inst, patch, ev, say, tests, run_id, key, env, stored, setup) -> tuple[Verdict, str]:
    claim = await classify(inst.problem_statement, patch or "")
    ev["claim"] = claim.model_dump()
    say("claim", ev["claim"])
    if claim.kind != "fix":
        return Verdict.NO_CHECKABLE_CLAIM, f"classified as '{claim.kind}': nothing to check"

    base = await env
    say("env_ready")
    w = await _reuse(stored, base, tests, key, say) if stored else None
    if w is None:
        if not setup:  # a stale stored test: set up now
            setup += _writer_setup(inst, env)
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

    test = {TEST_PATH: w.test_code.encode()}
    if patch is not None:
        patch, skipped = text_part(patch)
        if skipped:
            ev["binary_files_skipped"] = skipped
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
