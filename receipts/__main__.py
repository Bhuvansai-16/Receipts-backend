"""CLI.  python -m receipts smoke [--instance ID]
         python -m receipts run INSTANCE_ID [--patch gold|none|FILE]"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config


async def smoke(instance_id: str | None) -> None:
    for role, model in config.MODELS.items():
        r = await config.llm(role).ainvoke("Reply with the single word: ok")
        print(f"model {role:<10} {model}: {str(r.content).strip()[:40]!r}")

    from .sandbox import ACTIVATE, TEST_ARGS, TEST_PATH, run_pytest, text

    sdk = config.contree()
    r = await (await sdk.images.use("busybox:latest")).run(shell="echo sandbox-ok")
    print(f"sandbox: exit={r.exit_code} {text(r.stdout).strip()}")
    if not instance_id:
        return

    from .swebench import swe_image

    img = await swe_image(sdk, instance_id)
    r = await img.run(shell=f"{ACTIVATE} && python --version && python -m pytest --version; git log -1 --oneline",
                      timeout=config.SANDBOX_TIMEOUT_S)
    print(f"swe image {instance_id}: exit={r.exit_code}\n{text(r.stdout)}{text(r.stderr)}")
    run = await run_pytest(img, TEST_ARGS, {TEST_PATH: b"def test_probe():\n    assert 1 == 2\n"})
    print(f"probe (expect failed/AssertionError): {run.results or run.output}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252; model output isn't
    ap = argparse.ArgumentParser(prog="receipts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke", help="check models, sandbox, and optionally one SWE-bench image")
    s.add_argument("--instance")
    a = ap.parse_args()
    if a.cmd == "smoke":
        asyncio.run(smoke(a.instance))


if __name__ == "__main__":
    main()
