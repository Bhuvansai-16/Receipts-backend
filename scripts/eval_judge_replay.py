"""Replay the second opinion (current prompt) on every eval receipt that asked it before a REFUTED.

    python scripts/eval_judge_replay.py receipts-b30c80b0

Only the judge runs again, on the stored test and base output, so a prompt change is measured without
the test writer's variance. Prints old vs new verdict counts against the SWE-bench labels.
"""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from receipts import config, engine, evaluation, swebench  # noqa: E402


async def main(exp: str) -> None:
    cases = [evaluation.Case(**c) for c in json.loads((config.ROOT / "eval" / "dataset.json").read_text(encoding="utf-8"))]
    rows = []
    for c in cases:
        p = config.ROOT / "runs" / "eval" / exp / f"{c.run_id}.json"
        ev = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        op = ev.get("second_opinion") or {}
        if op and op.get("about") != "mixed":
            rows.append((c, ev))
    sem = asyncio.Semaphore(4)

    async def one(c, ev):
        async with sem:
            j = await engine.judge(swebench.load_instance(c.instance_id).problem_statement, ev["writer"]["test_code"],
                                   ev["forks"]["base_with_test"][0]["output_tail"])
        return c, ev, j

    tally = Counter()
    for c, ev, j in await asyncio.gather(*(one(c, ev) for c, ev in rows)):
        new = "REFUTED" if j and j.faithful else "UNPROVEN"  # no answer: the engine's error path, UNPROVEN
        tally[("fixed" if c.fixed else "wrong", ev["verdict"], new)] += 1
        if new != ev["verdict"]:
            print(f"{c.run_id}: {ev['verdict']} -> {new} | {j.reason[:160] if j else '(no answer)'}")
    for k, n in sorted(tally.items()):
        print(n, "x", *k)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
