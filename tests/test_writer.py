import asyncio

import pytest
from types import SimpleNamespace

from fakes import FakeImage
from receipts import sandbox, writer
from receipts.sandbox import ACTIVATE, ENV
from receipts.verdict import PytestRun, TestResult


def test_prompt_uses_portable_activation():
    assert ACTIVATE in writer.PROMPT


def test_writer_workspace_has_no_git_history():
    img = FakeImage()
    asyncio.run(writer.blind_workspace(img))
    call = img.calls[0]
    assert "rm -rf /testbed/.git" in call["shell"] and call["disposable"] is False


class FakeSession:
    shells = []

    def __init__(self, prev=None, fail=False):
        self.fail = fail

    async def run(self, **kw):
        FakeSession.shells.append(kw["shell"])
        if self.fail:
            raise TimeoutError("operation timed out")
        return SimpleNamespace(result=SimpleNamespace(stdout=b"caf\xe9 ok", stderr=b"", exit_code=0, truncated=False))


def test_safe_sandbox_refuses_commands_over_budget(monkeypatch):
    monkeypatch.setattr(sandbox, "AGENT_HARD_BUDGET", 1)
    sb = writer.SafeSandbox.__new__(writer.SafeSandbox)
    sb._session, sb._lock, sb.log = FakeSession(), asyncio.Lock(), []
    FakeSession.shells.clear()
    asyncio.run(sb.aexecute("ls"))
    with pytest.raises(sandbox.AgentBudgetExceeded):
        asyncio.run(sb.aexecute("ls again"))
    assert len(FakeSession.shells) == 1


def test_safe_sandbox_turns_errors_into_output_and_recovers():
    sb = writer.SafeSandbox.__new__(writer.SafeSandbox)
    sb._session, sb._lock, sb.log = FakeSession(fail=True), asyncio.Lock(), []
    r = asyncio.run(sb.aexecute("pytest"))
    assert r.exit_code == 1 and "sandbox error" in r.output
    r2 = asyncio.run(sb.aexecute("echo"))  # session reset to last good snapshot
    assert r2.exit_code == 0 and "caf" in r2.output
    assert [e["cmd"] for e in sb.log] == ["pytest", "echo"]
    assert FakeSession.shells[-1] == f"{ENV} && echo"  # agent commands run in the repo's env, not conda base


def test_writer_submits_leftover_test_file_when_agent_stops(monkeypatch):
    class Agent:
        async def ainvoke(self, *a, **k):
            raise sandbox.AgentBudgetExceeded("budget")

    class Backend:
        closed = False

        async def adownload_files(self, paths):
            return [SimpleNamespace(error=None, content=b"def test_bug(): assert 1 == 2")]

        async def aclose(self):
            Backend.closed = True

    async def backend(image, log):
        return Backend()

    async def blind(image):
        return image

    async def run_pytest(image, args, files):
        return PytestRun({"receipts_test.py::test_bug": TestResult("failed", "AssertionError", "assert 1 == 2")})

    monkeypatch.setattr(writer, "create_deep_agent", lambda **k: Agent())
    monkeypatch.setattr(writer.config, "llm", lambda role: None)
    monkeypatch.setattr(writer, "TavilySearch", lambda **k: None)

    async def scope_check(issue, test_code):
        return writer.Scope(faithful=True, reason="ok")

    for name, fn in [("agent_backend", backend), ("blind_workspace", blind), ("run_pytest", run_pytest),
                     ("scope_check", scope_check)]:
        monkeypatch.setattr(writer, name, fn)
    out = asyncio.run(writer.write_test("issue", object()))
    assert out.test_code == "def test_bug(): assert 1 == 2" and out.attempts == 1 and Backend.closed


def test_keep_tests_drops_tests_that_passed_on_base():
    code = ("import x\n\n\ndef helper():\n    return 1\n\n\ndef test_bug():\n    assert 0\n\n\n"
            "def test_guard():\n    assert 1\n\n\ndef test_param(v):\n    assert 0\n")
    kept = writer.keep_tests(code, ["receipts_test.py::test_bug", "receipts_test.py::test_param[a]"])
    assert "def test_bug" in kept and "def test_param" in kept and "def helper" in kept and "import x" in kept
    assert "test_guard" not in kept


def _fake_writer_run(monkeypatch, code, results, scope):
    class Agent:
        async def ainvoke(self, *a, **k):
            return None  # agent stops without submitting; the leftover file gets submitted

    class Backend:
        async def adownload_files(self, paths):
            return [SimpleNamespace(error=None, content=code.encode())]

        async def aclose(self):
            pass

    async def backend(image, log):
        return Backend()

    async def blind(image):
        return image

    async def run_pytest(image, args, files):
        return PytestRun({k: TestResult(*v) for k, v in results.items()})

    async def scope_check(issue, test_code):
        scope.seen = test_code
        return writer.Scope(faithful=scope.faithful, reason="adds POST expectations")

    monkeypatch.setattr(writer, "create_deep_agent", lambda **k: Agent())
    monkeypatch.setattr(writer.config, "llm", lambda role: None)
    monkeypatch.setattr(writer, "TavilySearch", lambda **k: None)
    for name, fn in [("agent_backend", backend), ("blind_workspace", blind), ("run_pytest", run_pytest),
                     ("scope_check", scope_check)]:
        monkeypatch.setattr(writer, name, fn)
    return asyncio.run(writer.write_test("issue", object()))


CODE = "def test_bug():\n    assert 1 == 2\n\n\ndef test_guard():\n    assert True\n"
RESULTS = {"receipts_test.py::test_bug": ("failed", "AssertionError", "assert 1 == 2"),
           "receipts_test.py::test_guard": ("passed",)}


def test_accepted_test_has_guards_dropped_and_passed_scope_check(monkeypatch):
    scope = SimpleNamespace(faithful=True)
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, scope)
    assert out.test_code is not None and "test_guard" not in out.test_code
    assert "test_guard" not in scope.seen  # the reviewer judges only the reproducing tests


def test_out_of_scope_test_is_rejected(monkeypatch):
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=False))
    assert out.test_code is None and "beyond the issue" in out.reason
