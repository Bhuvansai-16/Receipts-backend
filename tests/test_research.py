import asyncio

from receipts import research

ISSUE_2 = """TypeError: Invalid NaN comparison when printing an expression containing f(nan)
```python
>>> f = Function('f')
>>> str(f(nan) + f(1))
TypeError: Invalid NaN comparison
>>> sorted([f(nan), f(-1), f(1)], key=default_sort_key)
```"""


class FakeSearch:
    def __init__(self, fail=False, results=None, delay=0):
        self.queries, self.fail, self.results, self.delay = [], fail, results, delay

    async def ainvoke(self, args):
        self.queries.append(args["query"])
        n = len(self.queries)  # numbered before awaiting: the searches run at the same time
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("tavily down")
        return {"results": self.results if self.results is not None else [
            {"title": "Sorting", "url": f"https://docs.sympy.org/{n}",
             "content": "default_sort_key(item, order=None) returns a key ..."}]}


def test_library_and_api_names_come_from_the_issue():
    assert research.library_of("LaZy-Wolf/receipts-demo-sympy", ISSUE_2) == "sympy"
    assert research.library_of("psf/requests", "requests.get(url)") == "requests"
    assert research.api_names(ISSUE_2, "sympy") == ["default_sort_key", "Function"]  # no builtins, no `f`


def test_brief_searches_the_library_docs_and_keeps_sources():
    search = FakeSearch()
    brief = asyncio.run(research.research("LaZy-Wolf/receipts-demo-sympy", ISSUE_2, search))
    assert search.queries == ["sympy default_sort_key", "sympy Function"]
    assert [s["url"] for s in brief.sources] == ["https://docs.sympy.org/1", "https://docs.sympy.org/2"]
    assert "default_sort_key(item" in brief.for_writer() and len(brief.notes) <= 1200


def test_research_never_fails_a_check():
    assert asyncio.run(research.research("a/sympy", ISSUE_2, FakeSearch(fail=True))).sources == []
    quiet = FakeSearch()
    assert asyncio.run(research.research("a/b", "Something is slow sometimes.", quiet)).for_writer() == ""
    assert quiet.queries == []  # no names in the issue: no search


def test_code_hosts_and_source_views_never_reach_the_writer():
    # Blindness: a newer docs build's "view source" page can show the fixed code; a code host can show the PR.
    leaky = [{"title": "PR", "url": "https://github.com/sympy/sympy/pull/1", "content": "the fix"},
             {"title": "Source", "url": "https://docs.sympy.org/latest/_modules/sympy/core/sorting.html",
              "content": "def default_sort_key(...): fixed"},
             {"title": "Source", "url": "https://docs.sympy.org/latest/_sources/modules/core.rst.txt", "content": "x"},
             {"title": "Index", "url": "https://docs.sympy.org/latest/genindex.html", "content": "A B C"},
             {"title": "Docs", "url": "https://docs.sympy.org/latest/modules/core.html", "content": "usage"}]
    brief = asyncio.run(research.research("a/sympy", ISSUE_2, FakeSearch(results=leaky)))
    assert [s["url"] for s in brief.sources] == ["https://docs.sympy.org/latest/modules/core.html"]
    assert "fixed" not in brief.notes and "the fix" not in brief.notes


def test_a_slow_search_cannot_stall_a_check(monkeypatch):
    monkeypatch.setattr(research, "SEARCH_TIMEOUT_S", 0.05)
    brief = asyncio.run(asyncio.wait_for(research.research("a/sympy", ISSUE_2, FakeSearch(delay=5)), 2))
    assert brief.sources == [] and brief.for_writer() == ""


def test_only_web_links_become_sources():
    # A receipt is a public page: a javascript: or data: "link" from a search result must never reach it.
    hostile = [{"title": "x", "url": "javascript://docs.sympy.org/%0Aalert(1)", "content": "a"},
               {"title": "y", "url": "data://docs.sympy.org/text", "content": "b"},
               {"title": "ok", "url": "https://docs.sympy.org/latest/modules/core.html", "content": "c"}]
    brief = asyncio.run(research.research("a/sympy", ISSUE_2, FakeSearch(results=hostile)))
    assert [s["url"] for s in brief.sources] == ["https://docs.sympy.org/latest/modules/core.html"]


def test_the_repos_own_library_comes_before_the_issues_imports():
    # pydata/xarray 4629's issue imports numpy first; its brief searched numpy's docs.
    issue = "```python\nimport numpy as np\nimport xarray as xr\nxr.merge([a, b], combine_attrs='override')\n```"
    assert research.library_of("pydata/xarray", issue) == "xarray"
    assert research.library_of("scikit-learn/scikit-learn", "import numpy as np\nfrom sklearn import svm") == "sklearn"
    assert research.library_of("someone/app", ">>> from sympy import sqrt") == "sympy"  # unknown repo: the import


def test_api_names_come_from_the_issues_code_not_its_prose():
    # sympy #26 searched "sympy returned" ("... returned (x)") and a traceback gives "Traceback (".
    issue = ("sqrt(x**2) returned (x) instead of Abs(x), see `refine(sqrt(x**2), Q.real(x))`.\n\n"
             "```\nTraceback (most recent call last):\n  File \"t.py\", line 3, in <module>\n"
             "    print(simplify(e))\n```")
    assert research.api_names(issue, "sympy") == ["simplify", "refine"]


def test_one_failed_search_keeps_the_others_and_is_recorded():
    # langchain_tavily raises when a query finds nothing, which cancelled the other query; and a failing
    # search (an expired key) must show up in the evidence, not quietly turn research off.
    class FirstFails(FakeSearch):
        async def ainvoke(self, args):
            if not self.queries:
                self.queries.append(args["query"])
                raise RuntimeError("No search results found")
            return await super().ainvoke(args)

    brief = asyncio.run(research.research("a/sympy", ISSUE_2, FirstFails()))
    assert [s["url"] for s in brief.sources] == ["https://docs.sympy.org/2"]
    assert brief.errors == ["sympy default_sort_key: RuntimeError: No search results found"]


def test_the_writer_gets_the_docs_text_without_links():
    # A docs page links to its source on a code host; the writer gets the text, the receipt lists the links.
    brief = asyncio.run(research.research("a/sympy", ISSUE_2, FakeSearch()))
    assert "default_sort_key(item" in brief.for_writer() and "http" not in brief.for_writer()


def test_the_two_searches_run_at_the_same_time():
    started, both = [], asyncio.Event()

    class Search:
        async def ainvoke(self, args):
            started.append(args["query"])
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)  # one at a time would time out here
            return {"results": []}

    issue = "```python\nsimplify(nsimplify(x))\n```"
    brief = asyncio.run(research.research("sympy/sympy", issue, search=Search()))
    assert len(started) == 2 and brief.errors == []
