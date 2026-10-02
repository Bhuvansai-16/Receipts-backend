"""Write eval/RESULTS.md and eval/results.json from a Receipts experiment and a reader experiment.

    python scripts/eval_report.py --receipts receipts-b30c80b0 --reader reader-ultra-a72a5bc1
    python scripts/eval_report.py --receipts ... --reader ... --share   # scan every run for secrets, then make
                                                                         # the LangSmith dataset public

Labels come from the committed eval/dataset.json, verdicts from runs/eval/<receipts experiment>/, the reader's
answers from its LangSmith experiment.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
from langsmith import Client  # noqa: E402

from receipts import config, evaluation  # noqa: E402

DATASET = "receipts-swebench-verified"
SITE = "https://receipts-frontend-six.vercel.app"


def _key(inputs: dict) -> tuple:
    return inputs["instance_id"], inputs["kind"], inputs.get("agent", "")


def rows_for(receipts_exp: str, reader_exp: str | None, client: Client) -> list[dict]:
    cases = [evaluation.Case(**c) for c in json.loads((config.ROOT / "eval" / "dataset.json").read_text(encoding="utf-8"))]
    answers = {}
    if reader_exp:
        for run in client.list_runs(project_name=reader_exp, is_root=True):
            answers[_key(run.inputs.get("inputs", run.inputs))] = run.outputs or {}
    rows = []
    for c in cases:
        path = config.ROOT / "runs" / "eval" / receipts_exp / f"{c.run_id}.json"
        ev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        answer = answers.get((c.instance_id, c.kind, c.agent), {})
        rows.append({"run_id": c.run_id, "instance_id": c.instance_id, "repo": c.repo, "kind": c.kind, "agent": c.agent,
                     "fixed": c.fixed, "verdict": ev.get("verdict"), "reason": ev.get("reason"),
                     "cost_usd": evaluation.cost_usd(ev.get("tokens")), "seconds": ev.get("seconds"),
                     "reused": bool((ev.get("writer") or {}).get("reused_from")),
                     "reader_answer": answer.get("answer"), "reader_reason": answer.get("reason")})
    return rows


def scan(client: Client, experiments: list[str]) -> list[str]:
    """Every run of the experiments (traces included), checked for secrets; returns what was found, never values."""
    values = [v for v in dotenv_values(config.ROOT / ".env").values() if v]
    found = []
    for exp in experiments:
        for run in client.list_runs(project_name=exp):
            text = json.dumps([run.inputs, run.outputs, run.error], default=str)
            found += [f"{exp}: {kind}" for kind in evaluation.find_secrets(text, values)]
    return sorted(set(found))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--receipts", required=True)
    ap.add_argument("--reader")
    ap.add_argument("--share", action="store_true")
    args = ap.parse_args()
    client = Client()
    rows = rows_for(args.receipts, args.reader, client)
    rep = evaluation.report([r for r in rows if r["verdict"] is not None or r["reason"]])
    links = {"site": SITE}
    if args.share:
        findings = scan(client, [e for e in (args.receipts, args.reader) if e])
        if findings:
            sys.exit("not shared, secrets found: " + "; ".join(findings))
        share = client.share_dataset(dataset_name=DATASET)
        links["dataset"] = f"https://smith.langchain.com/public/{share['share_token']}/d"
    out = config.ROOT / "eval"
    (out / "results.json").write_text(json.dumps({**rep, "links": links, "experiments": {
        "receipts": args.receipts, "reader": args.reader}}, indent=1, default=str), encoding="utf-8")
    (out / "RESULTS.md").write_text(evaluation.markdown(rep, links), encoding="utf-8")
    print(json.dumps({"receipts": rep["receipts"], "reader": rep["reader"], "cost_usd": rep["cost_usd"],
                      "seconds_median": rep["seconds_median"], "misses": len(rep["misses"]),
                      "unproven_reasons": rep["unproven_reasons"], "links": links}, indent=1))


if __name__ == "__main__":
    main()
