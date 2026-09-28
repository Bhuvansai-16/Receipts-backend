"""Sandbox helpers: pytest in throwaway forks, patch application. All runs start from a given image."""
import asyncio
import json
from pathlib import Path

from . import config
from .verdict import PytestRun, TestResult

PROBE_SRC = (Path(__file__).parent / "pytest_probe.py").read_bytes()
MARKER = "__RECEIPTS_JSON__"
# Works under dash (/bin/sh on Ubuntu): `. bin/activate testbed` would drop the argument there.
ACTIVATE = "cd /testbed && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed"
TEST_PATH = "/testbed/receipts_test.py"
TEST_ARGS = ["receipts_test.py"]
_sem: asyncio.Semaphore | None = None


def _limit() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.SANDBOX_MAX_CONCURRENCY)
    return _sem


def text(x) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, (bytes, bytearray)) else (x or "")


def suite_files(test_ids: list[str]) -> list[str]:
    """Test files behind nodeids. Running by file survives ids that don't exist at base."""
    return sorted({t.split("::")[0] for t in test_ids})


def parse_probe_output(stdout: str, stderr: str = "") -> PytestRun:
    head, sep, tail = stdout.rpartition(MARKER)
    if not sep:
        return PytestRun(output=(stdout + stderr)[-4000:])
    try:
        raw = json.loads(tail.strip() or "{}")
    except ValueError:
        raw = {}
    return PytestRun({k: TestResult(**v) for k, v in raw.items()}, (head[-3000:] + stderr[-1000:]))


async def run_pytest(image, args: list[str], files: dict[str, bytes] | None = None) -> PytestRun:
    """Run pytest on `args` (nodeids/files, relative to /testbed) in a throwaway fork of `image`."""
    if not args:
        raise ValueError("refusing to run pytest with no targets (would run the whole repo)")
    files = {
        "/tmp/pytest_probe.py": PROBE_SRC,
        "/tmp/receipts_args.json": json.dumps(args).encode(),
        **(files or {}),
    }
    cmd = (
        f"{ACTIVATE} && PYTHONPATH=/tmp:$PYTHONPATH RECEIPTS_OUT=/tmp/receipts_out.json "
        f"python /tmp/pytest_probe.py; echo {MARKER}; cat /tmp/receipts_out.json 2>/dev/null"
    )
    async with _limit():
        try:
            r = await image.run(shell=cmd, files=files, timeout=config.SANDBOX_TIMEOUT_S,
                                truncate_output_at=10 * 1024 * 1024)
        except Exception as e:  # timeout / API error -> empty run -> rules yield UNPROVEN
            return PytestRun(output=f"sandbox error: {type(e).__name__}: {e}")
    return parse_probe_output(text(r.stdout), text(r.stderr))


async def apply_patch(image, patch: str):
    """New image with `patch` applied at /testbed, or None if it does not apply.

    No fuzzy fallback: a misplaced hunk would put code the author never wrote under a verdict.
    """
    cmd = "cd /testbed && git apply -v /tmp/pr.diff"
    async with _limit():
        r = await image.run(shell=cmd, files={"/tmp/pr.diff": patch.encode()}, disposable=False,
                            timeout=config.SANDBOX_TIMEOUT_S)
    return r if r.exit_code == 0 else None
