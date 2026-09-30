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
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("tavily down")
        return {"results": self.results if self.results is not None else [
            {"title": "Sorting", "url": f"https://docs.sympy.org/{len(self.queries)}",
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
