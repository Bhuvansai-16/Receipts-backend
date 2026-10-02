"""Run the evaluation dataset as a LangSmith experiment: the Receipts pipeline, or a reader that only reads.

    python scripts/eval_run.py --mode receipts --limit 1          # pilot: the first issue's five cases
    python scripts/eval_run.py --mode receipts                    # the whole dataset
    python scripts/eval_run.py --mode reader                      # Nemotron Ultra reads issue + diff, no running
    python scripts/eval_run.py --mode receipts --resume <name>    # finish a stopped experiment

Receipts are saved under runs/eval/<experiment>/ (never public until `python -m receipts import-eval`).
"""
import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langsmith import Client  # noqa: E402

from receipts import config, evaluation, swebench  # noqa: E402

DATASET = "receipts-swebench-verified"
CACHE = config.ROOT / ".cache" / "eval-patches"
RECEIPT_KEYS = ["caught", "false_proven", "proven_fix", "false_refuted", "unproven"]
READER_KEYS = ["accepted_fix", "false_reject", "rejected_wrong", "false_accept", "unsure"]


def load_patch(inputs: dict) -> str:
    """The cached agent patch, checked against the dataset's sha256."""
    path = CACHE / inputs["agent"] / f"{inputs['instance_id']}.diff"
    text = path.read_text(encoding="utf-8", errors="replace")
    if hashlib.sha256(text.encode()).hexdigest() != inputs["patch_sha256"]:
        raise ValueError(f"cached patch for {inputs['agent']}/{inputs['instance_id']} doesn't match the dataset")
    return text


def diff_of(inputs: dict) -> str:
    if inputs["kind"] == "none":
        return "(an empty diff: the pull request changes nothing)"
    if inputs["kind"] == "gold":
        return swebench.load_instance(inputs["instance_id"]).gold_patch
    return load_patch(inputs)


async def reader(inputs: dict) -> dict:
    inst = swebench.load_instance(inputs["instance_id"])
    llm = config.llm("judge").with_structured_output(evaluation.ReaderAnswer, method="function_calling")
    try:
        answer = await llm.ainvoke(evaluation.reader_prompt(inst.problem_statement, diff_of(inputs)))
        return answer.model_dump()
    except Exception as e:  # counted as unsure
        return {"answer": "unsure", "reason": f"{type(e).__name__}: {e}"[:300]}


def _results(scores: dict) -> dict:
    return {"results": [{"key": k, "score": v} for k, v in scores.items()]}


def receipts_eval(outputs: dict, reference_outputs: dict) -> dict:
    scores = evaluation.row_scores(reference_outputs["fixed"], outputs.get("verdict"))
    scores.update(cost_usd=outputs.get("cost_usd") or 0.0, seconds=outputs.get("seconds") or 0.0,
                  reused=int(bool(outputs.get("reused"))))
    return _results(scores)


def reader_eval(outputs: dict, reference_outputs: dict) -> dict:
    return _results(evaluation.reader_scores(reference_outputs["fixed"], outputs.get("answer", "unsure")))


def rates(keys, scorer, field):
    def summary_eval(outputs: list[dict], reference_outputs: list[dict]) -> dict:
        rows = [scorer(r["fixed"], o.get(field)) for o, r in zip(outputs, reference_outputs)]
        return _results({f"{k}_rate": v for k, v in evaluation.summary(rows, keys).items()})
    return summary_eval


def commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=config.ROOT).stdout.strip()
    except OSError:
        return ""


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["receipts", "reader"], required=True)
    ap.add_argument("--limit", type=int, help="only the first N issues")
    ap.add_argument("--resume", help="an experiment name to finish")
    ap.add_argument("--only", nargs="+", help="only these cases, by run id (a fix measured where it applies)")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    client = Client()
    # the committed order is interleaved by issue, so rows running at the same time are different issues
    rank = {c["instance_id"] + c["kind"] + c["agent"]: i for i, c in
            enumerate(json.loads((config.ROOT / "eval" / "dataset.json").read_text(encoding="utf-8")))}
    examples = sorted(client.list_examples(dataset_name=DATASET),
                      key=lambda e: rank.get(e.inputs["instance_id"] + e.inputs["kind"] + e.inputs["agent"], 10**6))
    order = {}
    for e in examples:
        order.setdefault(e.inputs["instance_id"], len(order))
    if args.limit:
        keep = sorted(order, key=order.get)[:args.limit]
        examples = [e for e in examples if e.inputs["instance_id"] in keep]
    if args.only:
        keep = set(args.only)
        examples = [e for e in examples if evaluation.case_of(e.inputs).run_id in keep]
    if args.resume:
        done = {r.reference_example_id for r in client.list_runs(project_name=args.resume, is_root=True)}
        examples = [e for e in examples if e.id not in done]
    if args.mode == "receipts":
        prefix = "receipts"
        out = config.ROOT / "runs" / "eval" / (args.resume or f"{prefix}-{commit()}")
        harness = evaluation.Harness(load_patch, out)

        async def target(inputs: dict) -> dict:  # aevaluate needs a function, not a callable object
            return await harness(inputs)

        evaluators = [receipts_eval]
        summaries = [rates(RECEIPT_KEYS, evaluation.row_scores, "verdict")]
    else:
        prefix, target, evaluators = "reader-ultra", reader, [reader_eval]
        summaries = [rates(READER_KEYS, evaluation.reader_scores, "answer")]
    print(f"{len(examples)} rows, mode {args.mode}", flush=True)
    results = await client.aevaluate(
        target, data=examples, evaluators=evaluators, summary_evaluators=summaries,
        max_concurrency=args.concurrency, experiment_prefix=None if args.resume else prefix,
        experiment=args.resume, metadata={"models": config.MODELS, "commit": commit()}, error_handling="log")
    print("experiment:", results.experiment_name, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
