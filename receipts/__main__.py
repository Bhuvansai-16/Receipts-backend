"""CLI.  python -m receipts smoke [--instance ID]  |  python -m receipts serve [--port 8000]
         python -m receipts run INSTANCE_ID [--patch gold|none|FILE]"""
import argparse
import asyncio
import sys
from pathlib import Path

from . import config


async def smoke(instance_id: str | None) -> None:
    for role, model in config.MODELS.items():
        r = await config.llm(role).ainvoke("Reply with the single word: ok")
        print(f"model {role:<10} {model}: {str(r.content).strip()[:40]!r}")

    from .sandbox import ACTIVATE, TEST_ARGS, TEST_PATH, run_pytest, text

    print(f"sandbox provider: {config.SANDBOX_PROVIDER}")
    if config.SANDBOX_PROVIDER == "contree":
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


async def _closing(coro):
    """Run `coro`, then close the Daytona HTTP session (otherwise aiohttp warns at exit)."""
    try:
        return await coro
    finally:
        if config.SANDBOX_PROVIDER == "daytona":
            from .daytona_backend import client

            await client().close()


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
    v = sub.add_parser("serve", help="web UI + API (build the UI first: cd web && npm run build)")
    v.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if a.cmd == "smoke":
        return asyncio.run(_closing(smoke(a.instance)))
    if a.cmd == "serve":
        import uvicorn

        return uvicorn.run("receipts.server:app", host="127.0.0.1", port=a.port)

    from .engine import check, new_run_id, save_evidence
    from .swebench import load_instance

    try:
        inst = load_instance(a.instance_id)
    except ValueError as e:
        sys.exit(f"error: {e}")
    patch, label = load_patch(a.patch, inst.gold_patch)
    ev = asyncio.run(_closing(check(inst, patch)))
    ev["pr"] = a.patch if a.patch in ("gold", "none") else "diff"
    out = save_evidence(ev, new_run_id(inst.instance_id, label))
    tokens = sum(u.get("total_tokens", 0) for u in (ev.get("tokens") or {}).values())
    print(f"\n{ev['verdict']}: {ev['reason']}\n  {ev['seconds']}s, {tokens} tokens\n  evidence: {out}")


if __name__ == "__main__":
    main()
