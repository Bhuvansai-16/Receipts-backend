"""Write eval/RESULTS.md and eval/results.json from a Receipts experiment and a reader experiment.

    python scripts/eval_report.py --receipts receipts-b30c80b0 --reader reader-ultra-a72a5bc1
    python scripts/eval_report.py --receipts ... --reader ... --share   # scan every run for secrets, then make
                                                                         # the LangSmith dataset public

Labels come from the committed eval/dataset.json, verdicts from runs/eval/<receipts experiment>/, the reader's
answers from its LangSmith experiment.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
from langsmith import Client  # noqa: E402

from receipts import config, evaluation  # noqa: E402

DATASET = "receipts-swebench-verified"
SITE = "https://receipts-frontend-six.vercel.app"
# What the baseline's misses changed, each measured where it applies (see the ledger for the runs)
CHANGES = [
    {"change": "Apply a PR's text changes when it also adds binary files without data (images)", "commit": "c04adda",
     "measured": "the 7 patches with image files, re-run (experiment receipts-545c8189)",
     "before": "7 Unproven: patch didn't apply",
     "after": "2 real fixes Proven, 2 wrong patches caught, 2 Unproven, 1 wrong patch passed"},
    {"change": "Second opinion before Refuted: asks whether the failure is the bug (not an environment error) and "
               "whether the test checks observable behaviour; asked 3 times, any doubt keeps it Unproven",
     "commit": "aa610a8", "measured": "replayed on every baseline check that reached it (scripts/eval_judge_replay.py)",
     "before": "7 real fixes refuted, 58 wrong patches refuted",
     "after": "2 real fixes refuted, 51 wrong patches refuted (5 of the 7 lost were tests crashing on an "
              "environment error that refuted the real fixes too)"},
]
NOTE = ("One baseline check (psf__requests-1724 with an agent patch) hung for over 20 minutes and was stopped; "
        "the tables cover the other 199.")


def changes_md() -> str:
    rows = [f"| {c['change']} ({c['commit']}) | {c['measured']} | {c['before']} | {c['after']} |" for c in CHANGES]
    return "\n".join(["", "## What the eval changed", "", "The tables above are the baseline. Each fix below came "
                      "from its misses and was measured where it applies:", "",
                      "| Change | Measured on | Before | After |", "|---|---|---|---|", *rows, "", NOTE, ""])


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


def scan(client: Client, receipts_dir: Path) -> list[str]:
    """Every run of every experiment on the dataset (sharing it shares them all, traces included) and every
    receipt to be imported, checked for secrets; returns what was found, never values."""
    values = [v for k, v in dotenv_values(config.ROOT / ".env").items()
              if v and not k.upper().endswith("_PATH")
              and re.search(r"KEY|TOKEN|SECRET|PASSWORD|PRIVATE|DATABASE_URL|DSN", k.upper())]
    found = []
    for project in client.list_projects(reference_dataset_name=DATASET):
        n = 0
        for n, run in enumerate(client.list_runs(project_name=project.name), 1):
            text = json.dumps([run.inputs, run.outputs, run.error], default=str)
            found += [f"{project.name}: {kind}" for kind in evaluation.find_secrets(text, values)]
        print(f"scanned {project.name}: {n} runs", flush=True)
    for path in receipts_dir.glob("*.json"):
        found += [f"{path.name}: {kind}" for kind in evaluation.find_secrets(path.read_text(encoding="utf-8"), values)]
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
    out = config.ROOT / "eval"
    old = json.loads((out / "results.json").read_text(encoding="utf-8")) if (out / "results.json").exists() else {}
    links = {"site": SITE, **{k: v for k, v in old.get("links", {}).items() if k == "dataset"}}  # stays public
    if args.share:
        findings = scan(client, config.ROOT / "runs" / "eval" / args.receipts)
        if findings:
            sys.exit("not shared, secrets found: " + "; ".join(findings))
        share = client.share_dataset(dataset_name=DATASET)
        links["dataset"] = f"https://smith.langchain.com/public/{share['share_token']}/d"
    (out / "results.json").write_text(json.dumps({**rep, "links": links, "changes": CHANGES, "note": NOTE,
                                                  "experiments": {"receipts": args.receipts, "reader": args.reader}},
                                                 indent=1, default=str), encoding="utf-8")
    (out / "RESULTS.md").write_text(evaluation.markdown(rep, links) + changes_md(), encoding="utf-8")
    print(json.dumps({"receipts": rep["receipts"], "reader": rep["reader"], "cost_usd": rep["cost_usd"],
                      "seconds_median": rep["seconds_median"], "misses": len(rep["misses"]),
                      "unproven_reasons": rep["unproven_reasons"], "links": links}, indent=1))


if __name__ == "__main__":
    main()
