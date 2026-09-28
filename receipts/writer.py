"""Blind test writer: a Deepagents agent (Nemotron Lightning) working inside a Contree sandbox.

Integrity rule D2: the agent sees the issue and the unpatched repo only, never the PR's patch.
Acceptance is decided by code (repro_check on a clean fork), not by the agent.
"""
import ast
from dataclasses import dataclass, field

from contree_sdk.langchain.sandbox import ContreeSandbox
from deepagents import create_deep_agent
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.tools import tool
from langchain_tavily import TavilySearch
from langsmith import traceable
from pydantic import BaseModel

from . import config, sandbox
from .sandbox import ACTIVATE, ENV, TEST_ARGS, TEST_PATH, run_pytest, text
from .verdict import PytestRun, repro_check

CODE_HOSTS = ["github.com", "gitlab.com", "bitbucket.org", "githubusercontent.com", "sourcegraph.com",
              "gitee.com", "codeberg.org", "huggingface.co", "swebench.com"]
# ponytail: allowlist of docs sites for the in-scope repos; readthedocs `_modules` pages can still show
# newer source, so blindness against the web is best-effort (see README).
DOC_DOMAINS = ["readthedocs.io", "docs.python.org", "pydata.org", "scikit-learn.org", "matplotlib.org",
               "sphinx-doc.org", "pytest.org", "palletsprojects.com", "astropy.org", "xarray.dev", "numpy.org",
               "scipy.org", "python-requests.org"]

PROMPT = f"""You write ONE pytest file that reproduces a reported bug in the repository at /testbed.

Work in this order and be quick (aim for submit_test within ~15 tool calls):
1. Find the code the issue is about: grep / read only the few relevant files.
2. Write {TEST_PATH} with write_file.
3. Call submit_test right away. It runs your file on a clean copy of the repo and returns the pytest output.
   (To run it yourself: {ACTIVATE} && python -m pytest receipts_test.py -q)
4. If REJECTED, read the reason and output, fix the file, and submit again, until ACCEPTED.

Rules:
- You get only the issue and the current (buggy) code. Never look for, write, or apply a fix.
- Don't install packages, change the environment, or make network requests; test the code directly.
- Do not edit repository files. Create only {TEST_PATH}.
- Tests must assert the behaviour the issue says is CORRECT, so they FAIL on the current code with an
  AssertionError (use plain `assert`). Import errors, other exceptions, or skips do not count.
- If the bug IS an exception (e.g. "fit raises TypeError"), call the code inside try/except and turn it into
  an assertion: `except TypeError as e: assert False, f"raised {{e!r}}"`. Don't use pytest.raises for this.
- Test functions must be named test_* at module level so pytest collects them.
- Test ONLY what the issue describes, the way it describes it: prefer the issue's own example, API and inputs.
  No extra cases, other methods/verbs, or stricter checks the issue doesn't ask for; a reviewer rejects
  tests that go beyond the issue.
- Keep it small: 1-2 focused test functions, no network access, no new dependencies.
- Stop as soon as submit_test answers ACCEPTED.
- docs_search is for library/API documentation only.
"""


class SafeSandbox(ContreeSandbox):
    """ContreeSandbox that survives sandbox errors and logs every command for the evidence file.

    Upstream lets SDK errors (timeouts, strict UTF-8 decoding) abort the whole agent, and the session is
    left FAILED with no way back.
    """

    def __init__(self, session, log: list):
        super().__init__(session)
        self.log = log

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        sandbox.check_budget(self.log)
        async with self._lock:
            try:
                r = (await self._session.run(shell=f"{ENV} && {command}", timeout=timeout, disposable=False, stdout=bytes,
                                             stderr=bytes, truncate_output_at=10 * 1024 * 1024)).result
                out = ExecuteResponse(output=text(r.stdout) + text(r.stderr), exit_code=r.exit_code,
                                      truncated=r.truncated)
            except Exception as e:
                self._session = type(self._session)(self._session)  # restart from the last good snapshot
                out = ExecuteResponse(output=f"sandbox error: {type(e).__name__}: {e}", exit_code=1)
        return sandbox.record(self.log, command, out)


class Scope(BaseModel):
    faithful_tests: list[str]
    reason: str


@traceable(name="scope_check")
async def scope_check(issue: str, test_code: str) -> Scope:
    """Blind review (issue + tests only): which tests assert just what the issue asks?"""
    prompt = (
        "The pytest tests below were written from the bug report below, and each fails on the current code. "
        "List in faithful_tests the names of the tests that assert only behaviour the report says is wrong, with "
        "expectations the report states or clearly implies, and that have no mistakes of their own (for example a "
        "name or docstring claiming something the test doesn't do). Leave out any test that adds other cases, "
        "methods, inputs or expectations the report doesn't ask for, and say in reason what you left out and why.\n\n"
        f"Bug report:\n{issue[:8000]}\n\nTests:\n```python\n{test_code}\n```"
    )
    return await config.llm("scope").with_structured_output(Scope, method="function_calling").ainvoke(prompt)


def _test_name(nodeid: str) -> str:
    return nodeid.split("::")[-1].split("[")[0]


def keep_tests(code: str, nodeids: list[str]) -> str:
    """Keep only the listed top-level tests (plus imports/helpers); drop the others and any __main__ runner."""
    keep = {_test_name(n) for n in nodeids}
    lines = code.splitlines(keepends=True)
    drop = [n for n in ast.parse(code).body
            if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
                and n.name not in keep)
            or (isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))]
    for node in sorted(drop, key=lambda n: n.lineno, reverse=True):
        decorators = getattr(node, "decorator_list", [])
        start = (decorators[0].lineno if decorators else node.lineno) - 1
        del lines[start:node.end_lineno]
    return "".join(lines)


async def agent_backend(image, log: list):
    """Deepagents backend on a live sandbox in `image`'s state (Daytona images bring their own)."""
    if hasattr(image, "agent_backend"):
        return await image.agent_backend(log)
    return SafeSandbox(image.session(), log)


async def blind_workspace(base_image):
    """The writer's own copy of base, without git history (tags/reflog may point past the fix).

    Verification never uses this image; it forks the untouched base.
    """
    return await base_image.run(shell="rm -rf /testbed/.git", disposable=False, timeout=config.SANDBOX_TIMEOUT_S)


@dataclass
class WriterResult:
    test_code: str | None = None
    base_run: PytestRun | None = None
    attempts: int = 0
    reason: str = "writer never submitted a test"
    queries: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    scope: str = ""  # what the scope check pruned and why


async def write_test(issue: str, base_image) -> WriterResult:
    out = WriterResult()
    backend = await agent_backend(await blind_workspace(base_image), out.log)
    tavily = TavilySearch(max_results=5, include_domains=DOC_DOMAINS, exclude_domains=CODE_HOSTS)

    @tool
    async def docs_search(query: str) -> str:
        """Search library/API documentation sites. Code hosting sites are excluded."""
        out.queries.append(query)
        return str(await tavily.ainvoke({"query": query}))[:6000]

    @tool
    async def submit_test() -> str:
        """Submit /testbed/receipts_test.py. It is re-run on a clean copy of the repo and checked."""
        if out.test_code is not None:
            return "ACCEPTED already. Stop."
        if out.attempts >= config.MAX_TEST_ATTEMPTS:
            return "REJECTED: attempt limit reached. Stop now."
        out.attempts += 1
        [dl] = await backend.adownload_files([TEST_PATH])
        if dl.error or not dl.content:
            out.reason = f"could not read {TEST_PATH}: {dl.error}"
            return f"REJECTED: {out.reason}"
        code = text(dl.content)
        run = await run_pytest(base_image, TEST_ARGS, {TEST_PATH: code.encode()})
        ok, out.reason = repro_check(run)
        if not ok:
            return f"REJECTED: {out.reason}\n--- pytest output (tail) ---\n{run.output[-2500:]}"
        # Keep only tests that reproduce the bug AND stick to the issue; prune the rest ourselves instead of
        # sending the agent round again (it tends to run out of budget before resubmitting).
        failing = [n for n, r in run.results.items() if r.outcome != "passed"]
        reproducing = keep_tests(code, failing)
        scope = await scope_check(issue, reproducing)
        faithful = [n for n in failing if _test_name(n) in set(scope.faithful_tests)]
        if not faithful:
            out.reason = f"no test sticks to the issue: {scope.reason}"
            return f"REJECTED: {out.reason}\nAssert only what the issue asks for, then submit again."
        trimmed = keep_tests(code, faithful)
        if trimmed != code:  # re-verify what remains reproduces on its own
            run = await run_pytest(base_image, TEST_ARGS, {TEST_PATH: trimmed.encode()})
            ok, out.reason = repro_check(run)
            if not ok:
                return f"REJECTED: after keeping only {', '.join(faithful)}: {out.reason}"
        out.test_code, out.base_run, out.scope = trimmed, run, scope.reason
        return "ACCEPTED. Stop now."

    agent = create_deep_agent(model=config.llm("writer"), tools=[docs_search, submit_test],
                              system_prompt=PROMPT, backend=backend)
    stopped = ""
    try:
        await agent.ainvoke({"messages": [{"role": "user", "content": f"Issue:\n\n{issue}"}]},
                            config={"recursion_limit": 150, "run_name": "blind_test_writer"})
    except Exception as e:  # command budget / recursion limit / model error
        stopped = f"agent stopped: {type(e).__name__}: {e}"[:500]
    try:
        if out.test_code is None and out.attempts < config.MAX_TEST_ATTEMPTS:
            await submit_test.ainvoke({})  # judge whatever test file the agent left behind
    finally:
        if hasattr(backend, "aclose"):
            await backend.aclose()
    if stopped and out.test_code is None:
        out.reason = f"{out.reason}; {stopped}"
    return out
