"""CLI.  python -m receipts smoke [--instance ID]  |  python -m receipts serve [--port 8000]
         python -m receipts run INSTANCE_ID [--patch gold|none|FILE]  |  python -m receipts migrate | import-runs"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import config


async def smoke(instance_id: str | None) -> None:
    for role, model in config.MODELS.items():
        r = await config.llm(role).ainvoke("Reply with the single word: ok")
        print(f"model {role:<10} {model}: {str(r.content).strip()[:40]!r}")

    from .sandbox import ACTIVATE, TEST_ARGS, TEST_PATH, run_pytest, text

    r = await (await config.contree().images.use("busybox:latest")).run(shell="echo sandbox-ok")
    print(f"sandbox: exit={r.exit_code} {text(r.stdout).strip()}")
    if not instance_id:
        return

    from .swebench import base_image

    img = await base_image(instance_id)
    r = await img.run(shell=f"{ACTIVATE} && python --version && python -m pytest --version; git log -1 --oneline",
                      timeout=config.SANDBOX_TIMEOUT_S)
    print(f"swe image {instance_id}: exit={r.exit_code}\n{text(r.stdout)}{text(r.stderr)}")
    run = await run_pytest(img, TEST_ARGS, {TEST_PATH: b"def test_probe():\n    assert 1 == 2\n"})
    print(f"probe (expect failed/AssertionError): {run.results or run.output}")


def _run_async(coro):
    """psycopg's async mode needs a selector event loop on Windows."""
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


async def _import_runs() -> int:
    from . import db

    pool = await db.open_pool(config.DATABASE_URL)
    try:
        return await db.import_runs(db.PgRuns(pool), config.RUNS_DIR)
    finally:
        await pool.close()


def load_patch(arg: str, gold: str) -> tuple[str | None, str]:
    """--patch value -> (patch text or None for a no-op PR, label for the evidence filename)."""
    if arg in ("gold", "none"):
        return (gold if arg == "gold" else None), arg
    return Path(arg).read_text(encoding="utf-8"), Path(arg).stem  # utf-8: Windows default is cp1252


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252; model output isn't
    ap = argparse.ArgumentParser(prog="receipts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke", help="check models, sandbox, and optionally one SWE-bench image")
    s.add_argument("--instance")
    r = sub.add_parser("run", help="check one SWE-bench Verified instance")
    r.add_argument("instance_id")
    r.add_argument("--patch", default="gold", help="gold | none | path to a .diff file")
    v = sub.add_parser("serve", help="API server (the UI is the separate receipts-frontend repo)")
    v.add_argument("--port", type=int, help="default: $PORT (Cloud Run), else 8000")
    sub.add_parser("migrate", help="apply database migrations (uses DATABASE_URL_UNPOOLED)")
    sub.add_parser("import-runs", help="load runs/*.json into the database as public example receipts")
    a = ap.parse_args()
    if a.cmd == "smoke":
        return asyncio.run(smoke(a.instance))
    if a.cmd in ("migrate", "import-runs"):
        if not config.DATABASE_URL:
            sys.exit("error: set DATABASE_URL (and DATABASE_URL_UNPOOLED) in .env first")
        if a.cmd == "migrate":
            from . import db

            applied = _run_async(db.migrate(config.DATABASE_URL_UNPOOLED))
            return print(f"applied: {', '.join(applied) or 'nothing new'}")
        return print(f"imported {_run_async(_import_runs())} runs")
    if a.cmd == "serve":
        import uvicorn

        # psycopg's async pool needs a selector event loop; uvicorn defaults to Proactor on Windows
        loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"
        cloud_port = os.environ.get("PORT")  # Cloud Run: traffic arrives from outside, so every interface
        return uvicorn.run("receipts.server:app", host="0.0.0.0" if cloud_port else "localhost",
                           port=a.port or int(cloud_port or 8000), loop=loop)

    from .engine import check, new_run_id, save_evidence
    from .swebench import load_instance

    try:
        inst = load_instance(a.instance_id)
    except ValueError as e:
        sys.exit(f"error: {e}")
    patch, label = load_patch(a.patch, inst.gold_patch)
    ev = asyncio.run(check(inst, patch))
    ev["pr"] = a.patch if a.patch in ("gold", "none") else "diff"
    out = save_evidence(ev, new_run_id(inst.instance_id, label))
    tokens = sum(u.get("total_tokens", 0) for u in (ev.get("tokens") or {}).values())
    print(f"\n{ev['verdict']}: {ev['reason']}\n  {ev['seconds']}s, {tokens} tokens\n  evidence: {out}")


if __name__ == "__main__":
    main()
