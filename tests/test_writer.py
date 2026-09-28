import asyncio
from types import SimpleNamespace

from fakes import FakeImage
from receipts import writer
from receipts.sandbox import ACTIVATE


def test_prompt_uses_portable_activation():
    assert ACTIVATE in writer.PROMPT


def test_writer_workspace_has_no_git_history():
    img = FakeImage()
    asyncio.run(writer.blind_workspace(img))
    call = img.calls[0]
    assert "rm -rf /testbed/.git" in call["shell"] and call["disposable"] is False


class FakeSession:
    def __init__(self, prev=None, fail=False):
        self.fail = fail

    async def run(self, **kw):
        if self.fail:
            raise TimeoutError("operation timed out")
        return SimpleNamespace(result=SimpleNamespace(stdout=b"caf\xe9 ok", stderr=b"", exit_code=0, truncated=False))


def test_safe_sandbox_turns_errors_into_output_and_recovers():
    sb = writer.SafeSandbox.__new__(writer.SafeSandbox)
    sb._session, sb._lock, sb.log = FakeSession(fail=True), asyncio.Lock(), []
    r = asyncio.run(sb.aexecute("pytest"))
    assert r.exit_code == 1 and "sandbox error" in r.output
    r2 = asyncio.run(sb.aexecute("echo"))  # session reset to last good snapshot
    assert r2.exit_code == 0 and "caf" in r2.output
    assert [e["cmd"] for e in sb.log] == ["pytest", "echo"]
