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
