"""Tokens, time and estimated model spend per check, from stored receipts: compare pipeline versions.

    python scripts/usage_report.py runs/eval/eval-final-pr16.json ...
    python scripts/usage_report.py --api https://receipts-backend-wnjy.onrender.com RUN_ID ...

PRICES: Nebius Token Factory list prices, USD per million tokens (input, output). A model missing from the
table is reported in tokens only.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from receipts.evaluation import PRICES  # noqa: E402


def load(ref: str, api: str | None) -> dict:
    if api:
        with urllib.request.urlopen(f"{api.rstrip('/')}/api/runs/{ref}", timeout=30) as r:
            return json.load(r)["evidence"]
    return json.loads(Path(ref).read_text(encoding="utf-8"))


def usage(ev: dict) -> dict:
    models, usd, priced = {}, 0.0, True
    for name, u in (ev.get("tokens") or {}).items():
        short = name.split("/")[-1]
        i, o = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        models[short] = (i, o)
        if short in PRICES:
            usd += (i * PRICES[short][0] + o * PRICES[short][1]) / 1e6
        else:
            priced = False
    w = ev.get("writer") or {}
    return {"run": ev.get("run_id") or ev.get("instance_id"), "verdict": ev.get("verdict"),
            "seconds": ev.get("seconds"), "attempts": w.get("attempts"), "commands": len(w.get("tool_log") or []),
            "reused": bool(w.get("reused_from")), "models": models,
            "tokens": sum(i + o for i, o in models.values()), "usd": round(usd, 4) if priced else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", help="read stored runs from this Receipts API instead of files")
    parser.add_argument("refs", nargs="+", help="receipt files, or run ids with --api")
    args = parser.parse_args()
    rows = [usage(load(ref, args.api)) for ref in args.refs]
    for row in rows:
        print(json.dumps(row))
    total = {"checks": len(rows), "tokens": sum(r["tokens"] for r in rows),
             "seconds": round(sum(r["seconds"] or 0 for r in rows), 1)}
    if all(r["usd"] is not None for r in rows):
        total["usd"] = round(sum(r["usd"] for r in rows), 4)
    total["per_check"] = {k: round(v / len(rows), 3) for k, v in total.items() if k != "checks"}
    print(json.dumps(total))


if __name__ == "__main__":
    main()
