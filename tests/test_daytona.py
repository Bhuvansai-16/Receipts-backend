import asyncio
from types import SimpleNamespace

import pytest

from receipts import daytona_backend as dt
from receipts import sandbox, swebench, writer
from receipts.sandbox import ENV


class FakeSandbox:
    """Stand-in for daytona.AsyncSandbox: process.exec, fs upload/download, delete."""

    def __init__(self, fail_on=None):
        self.cmds, self.uploads, self.files = [], [], {}
        self.deleted, self.fail_on, self.id = False, fail_on, f"sb{id(self)}"
        self.process = SimpleNamespace(exec=self._exec)
        self.fs = SimpleNamespace(upload_files=self._upload, download_file=self._download)

    async def _exec(self, command, cwd=None, env=None, timeout=None):
        self.cmds.append(command)
        if self.fail_on and self.fail_on in command:
            raise RuntimeError("daytona down")
        return SimpleNamespace(exit_code=0, result=f"ran:{command}")

    async def _upload(self, files, timeout=1800):
        self.uploads += [(f.destination, f.source) for f in files]

    async def _download(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def delete(self, timeout=60):
        self.deleted = True


@pytest.fixture
def started(monkeypatch):
    """Every sandbox started from a snapshot, in order."""

    class Boxes(list):
        pass

    boxes = Boxes()

    async def start(snapshot):
        boxes.append(FakeSandbox(fail_on=start.fail_on))
        boxes[-1].snapshot = snapshot
        return boxes[-1]

    start.fail_on = None
    monkeypatch.setattr(dt, "_start", start)
    boxes.start = start
    return boxes


def test_throwaway_run_starts_uploads_runs_and_deletes(started):
    r = asyncio.run(dt.DaytonaImage("snap").run(shell="pytest", files={"/testbed/t.py": b"x"}, timeout=5))
    [sb] = started
    assert r.stdout == "ran:pytest" and r.exit_code == 0
    assert sb.snapshot == "snap" and sb.uploads == [("/testbed/t.py", b"x")] and sb.deleted


def test_kept_run_is_replayed_before_later_runs(started):
    kept = asyncio.run(dt.DaytonaImage("snap").run(shell="git apply", files={"/tmp/pr.diff": b"d"}, disposable=False))
    assert isinstance(kept, dt.DaytonaImage) and kept.exit_code == 0
    asyncio.run(kept.run(shell="pytest"))
    later = started[-1]
    assert later.cmds == ["git apply", "pytest"] and ("/tmp/pr.diff", b"d") in later.uploads


def test_failed_run_still_deletes_the_sandbox(started):
    started.start.fail_on = "pytest"
    with pytest.raises(RuntimeError):
        asyncio.run(dt.DaytonaImage("snap").run(shell="pytest"))
    assert started[0].deleted


def test_agent_backend_logs_turns_errors_into_output_and_closes(started):
    work = dt.DaytonaImage("snap", steps=[("rm -rf /testbed/.git", None)])
    backend = asyncio.run(writer.agent_backend(work, log := []))
    sb = started[0]
    sb.files["/testbed/receipts_test.py"] = b"def test(): assert 0"
    ok = asyncio.run(backend.aexecute("ls"))
    [dl, missing] = asyncio.run(backend.adownload_files(["/testbed/receipts_test.py", "/nope"]))
    sb.fail_on = "pytest"
    bad = asyncio.run(backend.aexecute("pytest"))
    asyncio.run(backend.aclose())
    assert sb.cmds[0] == "rm -rf /testbed/.git"  # workspace state replayed first
    assert sb.cmds[1] == f"{ENV} && ls"  # agent commands run in the repo's env, not conda base
    assert ok.exit_code == 0 and dl.content == b"def test(): assert 0" and missing.error
    assert bad.exit_code == 1 and "sandbox error" in bad.output
    assert [e["cmd"] for e in log] == ["ls", "pytest"] and sb.deleted


def test_agent_backend_enforces_command_budget(started, monkeypatch):
    monkeypatch.setattr(sandbox, "AGENT_SOFT_BUDGET", 2)
    monkeypatch.setattr(sandbox, "AGENT_HARD_BUDGET", 3)
    backend = asyncio.run(writer.agent_backend(dt.DaytonaImage("snap"), []))
    outs = [asyncio.run(backend.aexecute(f"c{i}")) for i in range(4)]
    assert "submit_test" not in outs[0].output and "submit_test" in outs[1].output  # nudge from the soft limit
    assert outs[3].exit_code == 1 and "submit_test" in outs[3].output
    assert len(started[0].cmds) == 3  # the 4th command was refused, never executed


def test_docker_ref():
    assert swebench.docker_ref("psf__requests-1142") == "swebench/sweb.eval.x86_64.psf_1776_requests-1142:latest"


def test_base_image_uses_daytona_when_selected(monkeypatch):
    seen = []

    async def fake_base(ref, name):
        seen.append((ref, name))
        return "img"

    monkeypatch.setattr(swebench.config, "SANDBOX_PROVIDER", "daytona")
    monkeypatch.setattr(dt, "base_image", fake_base)
    assert asyncio.run(swebench.base_image("psf__requests-1142")) == "img"
    assert seen == [("swebench/sweb.eval.x86_64.psf_1776_requests-1142:latest", "receipts-psf-requests-1142")]
