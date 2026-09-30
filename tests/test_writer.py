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

    async def scope_check(issue, test_code, failing, cases):
        return writer.Scope(faithful_tests=["test_bug"], reason="ok")

    async def stated_cases(issue):
        return []

    async def after_fix_check(issue, test_code, tests):
        return []

    for name, fn in [("agent_backend", backend), ("blind_workspace", blind), ("run_pytest", run_pytest),
                     ("scope_check", scope_check), ("stated_cases", stated_cases),
                     ("after_fix_check", after_fix_check)]:
        monkeypatch.setattr(writer, name, fn)
    out = asyncio.run(writer.write_test("issue", object()))
    assert out.test_code == "def test_bug(): assert 1 == 2" and out.attempts == 1 and Backend.closed


def test_keep_tests_drops_tests_that_passed_on_base():
    code = ("import x\n\n\ndef helper():\n    return 1\n\n\ndef test_bug():\n    assert 0\n\n\n"
            "def test_guard():\n    assert 1\n\n\ndef test_param(v):\n    assert 0\n\n\n"
            "if __name__ == '__main__':\n    test_guard()\n")
    kept = writer.keep_tests(code, ["receipts_test.py::test_bug", "receipts_test.py::test_param[a]"])
    assert "def test_bug" in kept and "def test_param" in kept and "def helper" in kept and "import x" in kept
    assert "test_guard" not in kept and "__main__" not in kept  # runner block would call dropped tests


def _fake_writer_run(monkeypatch, code, results, scope, emit=None, make_agent=None, issue="issue"):
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

    async def scope_check(issue, test_code, failing, cases):
        scope.seen, scope.failing_seen, scope.cases_seen = test_code, failing, cases
        return writer.Scope(faithful_tests=scope.faithful, reason="test_extra adds POST expectations",
                            missing=getattr(scope, "missing", []))

    async def stated_cases(issue):
        return getattr(scope, "cases", [])

    async def after_fix_check(issue, test_code, tests):
        return [writer.AfterFix(test=t, after_fix="0.28867515045*I compared with 0.288675134594813*I",
                                passes_after_fix=False) for t in getattr(scope, "broken", [])]

    monkeypatch.setattr(writer, "create_deep_agent", lambda **k: make_agent(k) if make_agent else Agent())
    monkeypatch.setattr(writer.config, "llm", lambda role: None)
    for name, fn in [("agent_backend", backend), ("blind_workspace", blind), ("run_pytest", run_pytest),
                     ("scope_check", scope_check), ("stated_cases", stated_cases),
                     ("after_fix_check", after_fix_check)]:
        monkeypatch.setattr(writer, name, fn)
    return asyncio.run(writer.write_test(issue, object(), emit))


CODE = ("def test_bug():\n    assert 1 == 2\n\n\ndef test_extra():\n    assert 3 == 4\n\n\n"
        "def test_guard():\n    assert True\n")
RESULTS = {"receipts_test.py::test_bug": ("failed", "AssertionError", "assert 1 == 2"),
           "receipts_test.py::test_extra": ("failed", "AssertionError", "assert 3 == 4"),
           "receipts_test.py::test_guard": ("passed",)}


def test_scope_check_keeps_only_faithful_failing_tests(monkeypatch):
    scope = SimpleNamespace(faithful=["test_bug"])
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, scope)
    assert out.test_code is not None and "def test_bug" in out.test_code
    assert "test_extra" not in out.test_code and "test_guard" not in out.test_code
    # The reviewer sees the whole file, told which tests fail: a passing test still covers a case the issue
    # says already works (seen in #26: pruned before review, then reported missing, forever).
    assert "test_guard" in scope.seen
    assert scope.failing_seen == ["receipts_test.py::test_bug", "receipts_test.py::test_extra"]


def test_rejected_when_no_test_sticks_to_the_issue(monkeypatch):
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=[]))
    assert out.test_code is None and "sticks to the issue" in out.reason


def test_writer_emits_submit_events(monkeypatch):
    seen = []
    _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=[]), lambda t, d: seen.append((t, d)))
    _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=["test_bug"]),
                     lambda t, d: seen.append((t, d)))
    subs = [d for t, d in seen if t == "writer_submit"]
    assert [d["accepted"] for d in subs] == [False, True]
    assert subs[0]["attempt"] == 1 and "sticks to the issue" in subs[0]["reason"]


def test_writer_streams_its_command_count(monkeypatch):
    seen = []

    class Agent:
        async def ainvoke(self, *a, **k):
            return None

    class Backend:
        async def adownload_files(self, paths):
            return [SimpleNamespace(error="file_not_found", content=None)]

        async def aclose(self):
            pass

    async def backend(image, log):
        log.append({"cmd": "ls"})  # what the sandbox backends do for every shell command
        log.append({"cmd": "cat requests/models.py"})
        return Backend()

    async def blind(image):
        return image

    monkeypatch.setattr(writer, "create_deep_agent", lambda **k: Agent())
    monkeypatch.setattr(writer.config, "llm", lambda role: None)
    monkeypatch.setattr(writer, "agent_backend", backend)
    monkeypatch.setattr(writer, "blind_workspace", blind)
    monkeypatch.setattr(writer, "stated_cases", lambda issue: asyncio.sleep(0, []))
    out = asyncio.run(writer.write_test("issue", object(), lambda t, d: seen.append((t, d))))
    assert [d["commands"] for t, d in seen if t == "writer_progress"] == [1, 2]
    assert [e["cmd"] for e in out.log] == ["ls", "cat requests/models.py"]


def _through(mw, *names):
    """Send tool calls through the budget middleware; return the tools that really ran and the replies."""
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.messages import ToolMessage

    ran = []

    async def handler(req):
        ran.append(req.tool_call["name"])
        return ToolMessage("ok", tool_call_id=req.tool_call["id"])

    async def go():
        return [await mw.awrap_tool_call(ToolCallRequest({"name": n, "args": {}, "id": str(i)}, None, {}, None),
                                         handler) for i, n in enumerate(names)]

    return ran, asyncio.run(go())


def test_exploration_budget_submits_the_test_instead_of_looking_around(monkeypatch):
    # Seen: refused look-arounds were ignored ~60 times (772K tokens) and the test was never submitted.
    monkeypatch.setattr(writer, "EXPLORE_LIMIT", 2)
    monkeypatch.setattr(writer, "RETRY_ALLOWANCE", 1)

    class Submit:
        calls = 0

        async def ainvoke(self, args):
            Submit.calls += 1
            return "REJECTED: test_x: error with NameError"

    ran, replies = _through(writer.ExplorationBudget(Submit()), "grep", "read_file", "execute", "write_file",
                            "submit_test", "execute", "read_file", "edit_file")
    # past the limit a look-around call submits the file instead; writing always runs; a submission buys one more
    assert ran == ["grep", "read_file", "write_file", "submit_test", "execute", "edit_file"]
    assert Submit.calls == 2
    assert "did not run" in replies[2].content and "REJECTED: test_x" in replies[2].content
    assert replies[2].tool_call_id == "2" and replies[6].tool_call_id == "6"


def test_agent_run_ends_when_no_attempts_are_left(monkeypatch):
    monkeypatch.setattr(writer.config, "MAX_TEST_ATTEMPTS", 1)
    after = []

    class Agent:
        def __init__(self, tools):
            self.submit = next(t for t in tools if t.name == "submit_test")

        async def ainvoke(self, *a, **k):
            await self.submit.ainvoke({})
            after.append("kept going")

    out = _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=[]),
                           make_agent=lambda k: Agent(k["tools"]))
    assert after == [] and out.test_code is None and out.attempts == 1
    assert "sticks to the issue" in out.reason and "agent stopped" not in out.reason


def test_writer_agent_runs_with_the_exploration_budget(monkeypatch):
    seen = {}

    class Agent:
        async def ainvoke(self, *a, **k):
            return None

    class Backend:
        async def adownload_files(self, paths):
            return [SimpleNamespace(error="file_not_found", content=None)]

    async def backend(image, log):
        return Backend()

    async def blind(image):
        return image

    monkeypatch.setattr(writer, "create_deep_agent", lambda **k: seen.update(k) or Agent())
    monkeypatch.setattr(writer.config, "llm", lambda role: None)
    monkeypatch.setattr(writer, "agent_backend", backend)
    monkeypatch.setattr(writer, "blind_workspace", blind)
    monkeypatch.setattr(writer, "stated_cases", lambda issue: asyncio.sleep(0, []))
    asyncio.run(writer.write_test("issue", object()))
    assert any(isinstance(m, writer.ExplorationBudget) for m in seen["middleware"])


def test_agent_run_ends_as_soon_as_a_test_is_accepted(monkeypatch):
    # Seen: told "ACCEPTED. Stop now.", the writer kept probing for 77 s until its command budget ran out.
    after = []

    class Agent:
        def __init__(self, tools):
            self.submit = next(t for t in tools if t.name == "submit_test")

        async def ainvoke(self, *a, **k):
            await self.submit.ainvoke({})
            after.append("kept going")

    out = _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=["test_bug"]),
                           make_agent=lambda k: Agent(k["tools"]))
    assert after == [] and out.test_code is not None and out.attempts == 1
    assert "agent stopped" not in out.reason


def test_unchanged_resubmissions_are_not_attempts_and_end_the_run(monkeypatch):
    # Seen: the same broken file was auto-submitted four times and used up four of five attempts.
    replies, after = [], []

    class Agent:
        def __init__(self, tools):
            self.submit = next(t for t in tools if t.name == "submit_test")

        async def ainvoke(self, *a, **k):
            for _ in range(10):
                replies.append(await self.submit.ainvoke({}))
            after.append("kept going")

    broken = {"receipts_test.py": ("error", "CollectionError", "ImportError: no module x")}
    out = _fake_writer_run(monkeypatch, CODE, broken, SimpleNamespace(faithful=[]),
                           make_agent=lambda k: Agent(k["tools"]))
    assert out.attempts == 1 and after == [] and len(replies) == writer.UNCHANGED_LIMIT
    assert "unchanged" in replies[1] and "not counted" in replies[1]


def test_rejected_when_the_tests_leave_out_what_the_issue_states(monkeypatch):
    # Seen: the issue said Abs(z)**4 == z**4 too; a test of Abs(z)**2 alone let a half-right PR read PROVEN.
    scope = SimpleNamespace(faithful=["test_bug"], missing=["Abs(z)**4 == z**4 for imaginary z"])
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, scope)
    assert out.test_code is None and "Abs(z)**4 == z**4" in out.reason


def test_cases_come_from_the_issue_alone_and_reach_writer_and_reviewer(monkeypatch):
    # Shown only an Abs(z)**2 test, the reviewer listed only that case: cases must not come from the tests.
    first = {}

    class Agent:
        async def ainvoke(self, state, **k):
            first["message"] = state["messages"][0]["content"]

    cases = ["Abs(z)**2 == -z**2", "Abs(z)**4 == z**4"]
    scope = SimpleNamespace(faithful=["test_bug"], cases=cases)
    _fake_writer_run(monkeypatch, CODE, RESULTS, scope, make_agent=lambda k: Agent())
    assert all(c in first["message"] for c in cases) and scope.cases_seen == cases


def test_submitting_before_the_file_exists_costs_no_attempt(monkeypatch):
    # Seen in #14, #15, #16: the budget's auto-submit found no file yet and used up attempt 1 of 5.
    replies = []

    class Agent:
        def __init__(self, tools):
            self.submit = next(t for t in tools if t.name == "submit_test")

        async def ainvoke(self, *a, **k):
            replies.append(await self.submit.ainvoke({}))

    class Backend:
        async def adownload_files(self, paths):
            return [SimpleNamespace(error="file_not_found", content=None)]

    async def backend(image, log):
        return Backend()

    async def blind(image):
        return image

    monkeypatch.setattr(writer, "create_deep_agent", lambda **k: Agent(k["tools"]))
    monkeypatch.setattr(writer.config, "llm", lambda role: None)
    monkeypatch.setattr(writer, "agent_backend", backend)
    monkeypatch.setattr(writer, "blind_workspace", blind)
    monkeypatch.setattr(writer, "stated_cases", lambda issue: asyncio.sleep(0, []))
    out = asyncio.run(writer.write_test("issue", object()))
    assert out.attempts == 0 and "not counted" in replies[0] and "write_file" in replies[0]


def test_a_repeated_identical_call_returns_the_earlier_output_without_running(monkeypatch):
    # Seen in #15: the writer ran the same command 15 times in a row and got the same output each time.
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.messages import ToolMessage

    ran = []

    async def handler(req):
        ran.append(req.tool_call["name"])
        return ToolMessage(f"output {len(ran)}", tool_call_id=req.tool_call["id"])

    mw = writer.ExplorationBudget(None)
    calls = [("execute", {"command": "python -c 'print(1)'"}), ("execute", {"command": "python -c 'print(1)'"}),
             ("write_file", {"file_path": "/testbed/receipts_test.py"}), ("execute", {"command": "python -c 'print(1)'"})]

    async def go():
        return [await mw.awrap_tool_call(ToolCallRequest({"name": n, "args": a, "id": str(i)}, None, {}, None), handler)
                for i, (n, a) in enumerate(calls)]

    replies = asyncio.run(go())
    assert ran == ["execute", "write_file", "execute"]  # after a write the same command may show something new
    assert "already ran" in replies[1].content and "output 1" in replies[1].content


def test_rejected_when_a_test_would_still_fail_after_a_correct_fix(monkeypatch):
    # Seen in sympy #13: a float finite difference compared with an exact value fails on any code, so a PR
    # that fixed the bug read "mixed". The judge model spots it before acceptance (the scope model didn't).
    scope = SimpleNamespace(faithful=["test_bug", "test_extra"], broken=["test_extra()"])
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, scope)
    assert out.test_code is None and "test_extra would still fail after a correct fix" in out.reason


def test_every_counted_submission_is_kept_with_its_code_and_reason(monkeypatch):
    # #15 and #16 ended "passed on the unpatched code" with no way to see the file that did it.
    out = _fake_writer_run(monkeypatch, CODE, RESULTS, SimpleNamespace(faithful=[]))
    assert [(s["attempt"], s["accepted"], s["code"]) for s in out.submissions] == [(1, False, CODE)]
    assert "sticks to the issue" in out.submissions[0]["reason"]


def test_the_exception_the_issue_reports_gets_a_try_except_hint(monkeypatch):
    # Seen in sympy #14 (re-run): the bug is "TypeError: Invalid NaN comparison"; the test let it raise, the
    # generic "only AssertionError failures count" never got it fixed, and the writer drifted into fixing sympy.
    replies = []

    class Agent:
        def __init__(self, tools):
            self.submit = next(t for t in tools if t.name == "submit_test")

        async def ainvoke(self, *a, **k):
            replies.append(await self.submit.ainvoke({}))

    raised = {"receipts_test.py::test_bug": ("failed", "TypeError", "Invalid NaN comparison")}
    issue = "str(f(nan) + f(1)) raises\nTypeError: Invalid NaN comparison"
    _fake_writer_run(monkeypatch, CODE, raised, SimpleNamespace(faithful=["test_bug"]),
                     make_agent=lambda k: Agent(k["tools"]), issue=issue)
    assert "the bug the issue reports" in replies[0] and "except TypeError as e" in replies[0]
