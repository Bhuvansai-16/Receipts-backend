"""Build the evaluation dataset from SWE-bench's public leaderboard submissions, optionally upload it to LangSmith.

    python scripts/eval_dataset.py            # writes eval/dataset.json (patches cached in .cache/eval-patches/)
    python scripts/eval_dataset.py --upload   # also replaces the LangSmith dataset receipts-swebench-verified

AGENTS: eight SWE-bench Verified submissions whose per-issue patches are public (a `logs` asset), spread over
resolve rates (read 2 October 2026): appmap-navie gpt-4o 26%, SWE-agent Claude 3.5 Sonnet 34%, Agentless 1.5
gpt-4o 39%, Nebius search with open-weight models 41%, AutoCodeRover 2.0 Claude 3.5 Sonnet 46%, OpenHands
CodeAct 2.1 53%, CortexA 58%, SWE-agent Claude 3.7 Sonnet 62%.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from receipts import config, evaluation, swebench  # noqa: E402

AGENTS = [
    "20240615_appmap-navie_gpt4o",
    "20240620_sweagent_claude3.5sonnet",
    "20241028_agentless-1.5_gpt4o",
    "20241113_nebius-search-open-weight-models-11-24",
    "20241108_autocoderover-v2.0-claude-3-5-sonnet-20241022",
    "20241029_OpenHands-CodeAct-2.1-sonnet-20241022",
    "20250410_cortexa",
    "20250225_sweagent_claude-3-7-sonnet",
]
RAW = "https://raw.githubusercontent.com/SWE-bench/experiments/main/evaluation/verified/"
CACHE = config.ROOT / ".cache" / "eval-patches"
OUT = config.ROOT / "eval" / "dataset.json"
DATASET = "receipts-swebench-verified"


def _get(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def resolved_and_generated(agent: str, all_ids: set[str]) -> tuple[set[str], set[str]]:
    """Which issues the agent resolved, and which it produced a patch for (per SWE-bench's own results)."""
    if raw := _get(RAW + agent + "/results/results.json"):
        res = json.loads(raw)
        missing = set(res.get("no_generation", [])) | set(res.get("no_logs", []))
        return set(res.get("resolved", [])), all_ids - missing
    details = json.loads(_get(RAW + agent + "/per_instance_details.json") or b"{}")
    return {i for i, d in details.items() if d.get("resolved")}, set(details)


def fetch(agent: str, iid: str) -> str | None:
    """The agent's patch for an issue, cached on disk; a missing patch is cached as missing."""
    path = CACHE / agent / f"{iid}.diff"
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
        return text or None
    raw = _get(evaluation.patch_url(agent, iid))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = raw.decode("utf-8", errors="replace") if raw else ""
    path.write_text(text, encoding="utf-8", newline="")
    return text or None


def upload(cases: list[evaluation.Case]) -> str:
    from langsmith import Client

    client = Client()
    if client.has_dataset(dataset_name=DATASET):
        client.delete_dataset(dataset_name=DATASET)
    ds = client.create_dataset(DATASET, description=(
        "Receipts against SWE-bench Verified's answer key: per issue the real fix, an empty patch, two wrong and "
        "one right agent patch from the public leaderboard. Label: fixed, from SWE-bench's hidden tests."))
    examples = [{"inputs": {k: getattr(c, k) for k in ("instance_id", "kind", "agent", "patch_url", "patch_sha256")},
                 "outputs": {"fixed": c.fixed}, "metadata": {"repo": c.repo, "run_id": c.run_id}} for c in cases]
    for i in range(0, len(examples), 50):
        client.create_examples(dataset_id=ds.id, examples=examples[i:i + 50])
    return str(ds.id)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--issues", type=int, default=40)
    ap.add_argument("--per-repo", type=int, default=6, help="spreads issues over more repositories")
    args = ap.parse_args()
    rows = {i: r for i, r in swebench._dataset().items() if r["repo"] in swebench.PYTEST_REPOS}
    all_ids = set(swebench._dataset())
    resolved, generated = {}, {}
    for agent in AGENTS:
        resolved[agent], generated[agent] = resolved_and_generated(agent, all_ids)
        print(f"{agent}: resolved {len(resolved[agent])}, patches {len(generated[agent])}", flush=True)
    cases = evaluation.interleave(evaluation.select_cases(rows, resolved, generated, fetch, issues=args.issues,
                                                               per_repo=args.per_repo))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps([asdict(c) for c in cases], indent=1), encoding="utf-8")
    repos = {}
    for c in cases:
        repos.setdefault(c.repo, set()).add(c.instance_id)
    print(json.dumps({"issues": len({c.instance_id for c in cases}), "cases": len(cases),
                      "per_repo": {r: len(v) for r, v in sorted(repos.items())}}))
    if args.upload:
        print("uploaded to LangSmith dataset", upload(cases))


if __name__ == "__main__":
    main()
