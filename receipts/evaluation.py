"""Evaluation against SWE-bench's answer key: which patches to check, how to score them, the reader baseline.

Pure functions here; scripts/eval_*.py do the downloads and the LangSmith calls. SWE-bench's hidden tests only
label the patches (fixed or not); Receipts never sees them.
"""
import asyncio
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from . import db, engine, swebench

GOLD_URL = "swebench:gold"
NONE_URL = "none"
# tokenfactory.nebius.com/model-catalog.md, read 1 October 2026: USD per million tokens (input, output)
PRICES: dict[str, tuple[float, float]] = {
    "NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
    "nemotron-3-super-120b-a12b": (0.30, 0.90),
    "Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
}


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
        wrong = [a for a in sorted(generated) if iid in generated[a] and iid not in resolved.get(a, set())]
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
                  Case(iid, repo, "right", right_patch[0], True, patch_url(right_patch[0], iid),
                       sha256(right_patch[1]))]
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


CAUGHT = {"REFUTED", "REGRESSION"}
NOT_CHECKED = {"UNPROVEN", "NO_CHECKABLE_CLAIM", None}
DIFF_LIMIT = 30_000


def row_scores(fixed: bool, verdict: str | None) -> dict[str, int]:
    """Feedback for one checked patch; each truth class gets only its own keys, so averages are the rates."""
    unproven = int(verdict in NOT_CHECKED)
    if fixed:
        return {"proven_fix": int(verdict == "PROVEN"), "false_refuted": int(verdict == "REFUTED"), "unproven": unproven}
    return {"caught": int(verdict in CAUGHT), "false_proven": int(verdict == "PROVEN"), "unproven": unproven}


def reader_scores(fixed: bool, answer: str) -> dict[str, int]:
    """The same for a reviewer that only reads the issue and the diff."""
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


def cost_usd(tokens: dict | None) -> float:
    """Model cost of one check at list prices, from the evidence's usage by model."""
    total = 0.0
    for name, u in (tokens or {}).items():
        price_in, price_out = PRICES.get(name.split("/")[-1], (0.0, 0.0))
        total += (int(u.get("input_tokens") or 0) * price_in + int(u.get("output_tokens") or 0) * price_out) / 1e6
    return round(total, 5)


RECEIPT_KEYS = ["caught", "false_proven", "proven_fix", "false_refuted", "unproven"]
READER_KEYS = ["accepted_fix", "false_reject", "rejected_wrong", "false_accept", "unsure"]
_REASONS = [("no valid reproducing test", "no valid test"), ("does not reproduce", "test didn't reproduce on base"),
            ("flaky", "flaky base runs"), ("patch does not apply", "patch didn't apply"),
            ("mixed or fail differently", "mixed runs"), ("model was unavailable", "model outage"),
            ("second opinion doubts", "second opinion doubted the test"), ("did not run on the PR", "PR runs didn't run"),
            ("could not run", "existing tests couldn't run"), ("classified as", "no checkable claim"),
            ("pipeline error", "pipeline error (sandbox or API)")]


def reason_group(reason: str | None) -> str:
    text = reason or ""
    return next((name for needle, name in _REASONS if needle in text), "other: " + text[:40])


def _median(values: list[float]) -> float:
    v = sorted(values)
    return round((v[len(v) // 2] + v[(len(v) - 1) // 2]) / 2, 4) if v else 0.0


def report(rows: list[dict]) -> dict:
    """Headline rates for Receipts and the reader, per repository, cost and time, and every miss."""
    scores = [row_scores(r["fixed"], r["verdict"]) for r in rows]
    read = [(r, reader_scores(r["fixed"], r["reader_answer"])) for r in rows if r.get("reader_answer")]
    per_repo: dict[str, list[dict]] = {}
    for r, s in zip(rows, scores):
        per_repo.setdefault(r["repo"], []).append(s)
    unproven: dict[str, int] = {}
    for r, s in zip(rows, scores):
        if s["unproven"]:
            g = reason_group(r.get("reason"))
            unproven[g] = unproven.get(g, 0) + 1
    return {
        "issues": len({r["instance_id"] for r in rows}), "cases": len(rows), "fixed": sum(bool(r["fixed"]) for r in rows),
        "receipts": summary(scores, RECEIPT_KEYS), "reader": summary([s for _, s in read], READER_KEYS) if read else {},
        "cost_usd": {"total": round(sum(r.get("cost_usd") or 0 for r in rows), 3),
                     "median": _median([r.get("cost_usd") or 0 for r in rows])},
        "seconds_median": _median([r.get("seconds") or 0 for r in rows]),
        "reused_share": round(sum(bool(r.get("reused")) for r in rows) / len(rows), 4) if rows else 0.0,
        "per_repo": {repo: {"cases": len(v), **summary(v, RECEIPT_KEYS)} for repo, v in sorted(per_repo.items())},
        "unproven_reasons": dict(sorted(unproven.items(), key=lambda kv: -kv[1])),
        "misses": [r for r, s in zip(rows, scores) if s.get("false_proven") or s.get("false_refuted")],
        "reader_false_accepts": [r for r, s in read if s.get("false_accept")],
    }


def _pct(x: float) -> str:
    return f"{math.floor(x * 100 + 0.5)}%"  # half up, as the Results page rounds


def markdown(rep: dict, links: dict) -> str:
    """eval/RESULTS.md from a report; links: {"dataset": public LangSmith URL, "site": the app's origin}."""
    r, d, site = rep["receipts"], rep.get("reader") or {}, links.get("site", "")
    lines = [
        "# Receipts against SWE-bench Verified's answer key", "",
        f"{rep['issues']} issues, {rep['cases']} patches ({rep['fixed']} fix the issue, {rep['cases'] - rep['fixed']} "
        "don't, by SWE-bench's hidden tests, which Receipts never sees).", "",
        "| | Receipts (runs the blind test) | Nemotron Ultra reading the diff |", "|---|---|---|",
        f"| Catch rate: wrong patches flagged | {_pct(r['caught'])} | {_pct(d.get('rejected_wrong', 0))} |",
        f"| Wrong patches passed as fixes | {_pct(r['false_proven'])} | {_pct(d.get('false_accept', 0))} |",
        f"| Real fixes confirmed | {_pct(r['proven_fix'])} | {_pct(d.get('accepted_fix', 0))} |",
        f"| Real fixes rejected | {_pct(r['false_refuted'])} | {_pct(d.get('false_reject', 0))} |",
        f"| No answer (Unproven / unsure) | {_pct(r['unproven'])} | {_pct(d.get('unsure', 0))} |", "",
        f"Cost: ${rep['cost_usd']['total']} in all, ${rep['cost_usd']['median']} median per check at Token Factory list "
        f"prices; median {rep['seconds_median']} s per check; {_pct(rep['reused_share'])} of checks reused a blind test.",
        "", "## Per repository", "", "| Repository | Patches | Catch rate | Wrong passed | Fixes confirmed | Fixes rejected |",
        "|---|---|---|---|---|---|",
        *(f"| {repo} | {v['cases']} | {_pct(v['caught'])} | {_pct(v['false_proven'])} | {_pct(v['proven_fix'])} | "
          f"{_pct(v['false_refuted'])} |" for repo, v in rep["per_repo"].items()),
        "", "## Why some checks were Unproven", "", *(f"- {k}: {v}" for k, v in rep["unproven_reasons"].items()),
        "", "## Every miss", "",
        *(f"- {m['verdict']} on {m['instance_id']} ({m['kind']} {m['agent']}, "
          f"{'fixes' if m['fixed'] else 'does not fix'} the issue): [receipt]({site}/runs/{m['run_id']})"
          for m in rep["misses"]),
        *([] if rep["misses"] else ["None."]),
    ]
    if links.get("dataset"):
        lines += ["", f"Every row, score and trace: [the public LangSmith dataset]({links['dataset']})."]
    return "\n".join(lines) + "\n"


_TOKEN_SHAPES = [(r"ghp_[A-Za-z0-9]{30,}", "a GitHub token"), (r"github_pat_[A-Za-z0-9_]{30,}", "a GitHub token"),
                 (r"lsv2_[a-z]{2}_[A-Za-z0-9_]{20,}", "a LangSmith key"), (r"tvly-[A-Za-z0-9-]{20,}", "a Tavily key"),
                 (r"sk-[A-Za-z0-9_-]{20,}", "an API key"), (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key")]


def find_secrets(text: str, values) -> list[str]:
    """What kinds of secret appear in text (never the secret itself): a configured value, or a known token shape."""
    found = ["a configured secret value"] if any(v and len(v) >= 8 and v in text for v in values) else []
    found += [name for pattern, name in _TOKEN_SHAPES if re.search(pattern, text)]
    return list(dict.fromkeys(found))


def case_of(inputs: dict) -> Case:
    return Case(inputs["instance_id"], inputs.get("repo", ""), inputs["kind"], inputs.get("agent", ""), False,
                inputs.get("patch_url", ""), inputs.get("patch_sha256", ""))


class Harness:
    """The pipeline as a LangSmith target: one row is one patch. An issue's rows run one after another and share
    one store, so the first writes the blind test and the others reuse it (it never depends on the patch)."""

    def __init__(self, load_patch: Callable[[dict], str], out_dir: Path):
        self.load_patch, self.out_dir = load_patch, Path(out_dir)
        self.store = db.MemoryRuns()
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __call__(self, inputs: dict) -> dict:
        case = case_of(inputs)
        async with self.locks[case.instance_id]:
            try:
                inst = swebench.load_instance(case.instance_id)
                patch = (inst.gold_patch if case.kind == "gold" else None if case.kind == "none"
                         else self.load_patch(inputs))
                ev = await engine.check(inst, patch, tests=self.store, run_id=case.run_id)
            except Exception as e:  # the harness's own failure: scored as not checked
                ev = {"instance_id": case.instance_id, "verdict": None, "reason": f"{type(e).__name__}: {e}"}
        ev.update(run_id=case.run_id, pr={"gold": "gold", "none": "none"}.get(case.kind, "diff"))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"{case.run_id}.json").write_text(json.dumps(ev, default=str), encoding="utf-8")
        return {"run_id": case.run_id, "verdict": ev.get("verdict"), "reason": ev.get("reason"),
                "seconds": ev.get("seconds"), "cost_usd": cost_usd(ev.get("tokens")),
                "tokens": sum(int(u.get("total_tokens") or 0) for u in (ev.get("tokens") or {}).values()),
                "reused": bool((ev.get("writer") or {}).get("reused_from"))}
