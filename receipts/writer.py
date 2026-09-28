"""Blind test writer: a Deepagents agent (Nemotron Lightning) working inside a Contree sandbox.

Integrity rule D2: the agent sees the issue and the unpatched repo only, never the PR's patch.
Acceptance is decided by code (repro_check on a clean fork), not by the agent.
"""
from dataclasses import dataclass, field

from contree_sdk.langchain.sandbox import ContreeSandbox
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langchain_tavily import TavilySearch

from . import config
from .sandbox import TEST_ARGS, TEST_PATH, run_pytest, text
from .verdict import PytestRun, repro_check

CODE_HOSTS = ["github.com", "gitlab.com", "bitbucket.org", "githubusercontent.com", "sourcegraph.com",
              "gitee.com", "codeberg.org", "huggingface.co", "swebench.com"]

PROMPT = f"""You write ONE pytest file that reproduces a reported bug in the repository at /testbed.

Rules:
- You get only the issue and the current (buggy) code. Never look for, write, or apply a fix.
- Do not edit repository files. Create only {TEST_PATH}.
- Tests must assert the behaviour the issue says is CORRECT, so they FAIL on the current code with an
  AssertionError (use plain `assert`). Import errors, other exceptions, or skips do not count.
- Keep it small: 1-3 focused test functions, no network access, no new dependencies.
- Run it with: cd /testbed && . /opt/miniconda3/bin/activate testbed && python -m pytest receipts_test.py -q
- When it fails for the right reason, call submit_test. If rejected, read the reason, fix the test, submit again.
- Stop as soon as submit_test answers ACCEPTED.
- docs_search is for library/API documentation only.
"""


@dataclass
class WriterResult:
    test_code: str | None = None
    base_run: PytestRun | None = None
    attempts: int = 0
    reason: str = "writer never submitted a test"
    queries: list[str] = field(default_factory=list)


async def write_test(issue: str, base_image) -> WriterResult:
    out = WriterResult()
    backend = ContreeSandbox(base_image.session())
    tavily = TavilySearch(max_results=5, exclude_domains=CODE_HOSTS)

    @tool
    async def docs_search(query: str) -> str:
        """Search library/API documentation on the web. Code hosting sites are excluded."""
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
        if ok:
            out.test_code, out.base_run = code, run
            return "ACCEPTED. Stop now."
        return f"REJECTED: {out.reason}\n--- pytest output (tail) ---\n{run.output[-2500:]}"

    agent = create_deep_agent(model=config.llm("writer"), tools=[docs_search, submit_test],
                              system_prompt=PROMPT, backend=backend)
    try:
        await agent.ainvoke({"messages": [{"role": "user", "content": f"Issue:\n\n{issue}"}]},
                            config={"recursion_limit": 150, "run_name": "blind_test_writer"})
    except Exception as e:  # recursion limit / model error: keep whatever was accepted
        out.reason = f"{out.reason}; agent stopped: {type(e).__name__}: {e}"[:1000]
    return out
