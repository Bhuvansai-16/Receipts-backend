"""Check real pull requests end to end, outside the web app, and save each receipt for comparison.

    python scripts/eval_prs.py --repo LaZy-Wolf/receipts-demo-sympy --label super 13 14 15

The repository must have the Receipts GitHub App installed (its installation is read from the database).
Models come from the environment as usual, so an A/B is two runs with a different MODEL_TEST_WRITER.
Each receipt lands in runs/eval/eval-<label>-pr<N>.json (never imported as a public example); one JSON line per
pull request is printed.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg  # noqa: E402

from receipts import config, engine, github_app, targets  # noqa: E402


async def check_one(inst_id: int, repo_id: int, repo: str, number: int, label: str, slots) -> dict:
    async with slots:
        t0 = time.monotonic()
        target, diff, _ = await targets.pr_target(inst_id, repo_id, repo, number)
        ev = await engine.check(target, diff)
    ev["source"] = {"repo": repo, "pr_number": number}
    out = config.RUNS_DIR / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"eval-{label}-pr{number}.json").write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    writer = ev.get("writer") or {}
    return {"pr": number, "label": label, "verdict": ev["verdict"], "seconds": round(time.monotonic() - t0),
            "tokens": sum(u.get("total_tokens", 0) for u in (ev.get("tokens") or {}).values()),
            "attempts": writer.get("attempts"), "accepted": bool(writer.get("test_code")),
            "retried": "writer_first" in ev, "reason": (ev.get("reason") or "")[:160]}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name with the Receipts app installed")
    parser.add_argument("--label", required=True, help="names the saved receipts, e.g. the model")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("prs", nargs="+", type=int)
    args = parser.parse_args()
    owner = args.repo.split("/")[0]
    async with await psycopg.AsyncConnection.connect(config.DATABASE_URL) as conn:
        row = await (await conn.execute("SELECT id FROM github_installations WHERE lower(account_login) = lower(%s)",
                                        (owner,))).fetchone()
    if not row:
        sys.exit(f"The Receipts app isn't installed on {owner} (no row in github_installations).")
    token = await github_app.installation_token(row[0], None, {"metadata": "read"})
    repo_id = (await github_app.api(token, "GET", f"/repos/{args.repo}")).json()["id"]
    print(json.dumps({"writer": config.MODELS["writer"], "writer_strong": config.MODELS["writer_strong"]}), flush=True)
    slots = asyncio.Semaphore(args.parallel)
    for done in asyncio.as_completed([check_one(row[0], repo_id, args.repo, n, args.label, slots) for n in args.prs]):
        print(json.dumps(await done), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
