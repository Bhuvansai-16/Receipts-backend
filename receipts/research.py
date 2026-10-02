"""Research brief: the library's own docs for the APIs an issue names, fetched before the test writer starts.

The writer had Tavily as an optional tool and called it 0 times in 40 runs; this step always runs. Blind like the
writer: queries come from the issue text alone, code hosts are excluded (by Tavily and again here), and "view
source" pages are dropped, since a newer docs build can show the fixed code; the writer gets the text without
links. A failed search is logged and recorded in the brief; research never fails or stalls a check.
"""
import asyncio
import builtins
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from langsmith import traceable

CODE_HOSTS = ["github.com", "gitlab.com", "bitbucket.org", "githubusercontent.com", "sourcegraph.com",
              "gitee.com", "codeberg.org", "huggingface.co", "swebench.com"]
# ponytail: docs sites for the repos we check; add one when a new library shows up.
DOCS = {"sympy": "docs.sympy.org", "requests": "requests.readthedocs.io", "xarray": "docs.xarray.dev",
        "sklearn": "scikit-learn.org", "pylint": "pylint.readthedocs.io", "seaborn": "seaborn.pydata.org",
        "matplotlib": "matplotlib.org", "numpy": "numpy.org", "pandas": "pandas.pydata.org",
        "django": "docs.djangoproject.com", "flask": "flask.palletsprojects.com", "pytest": "docs.pytest.org",
        "astropy": "docs.astropy.org", "sphinx": "www.sphinx-doc.org"}
DOC_DOMAINS = sorted(set(DOCS.values()) | {"readthedocs.io", "docs.python.org"})
SOURCE_VIEWS = ("/_modules/", "/_sources/")  # Sphinx pages that show code, possibly newer than the base
INDEX_PAGES = ("genindex", "py-modindex", "search.html")  # lists of names, nothing to read
NOTES_LIMIT = 1200
SEARCH_TIMEOUT_S = 20
_NOT_APIS = set(dir(builtins)) | {"Traceback"}  # "Traceback (most recent call last)" reads like a call
log = logging.getLogger("uvicorn.error")


@dataclass
class Brief:
    queries: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)  # {"title", "url"}
    notes: str = ""
    errors: list[str] = field(default_factory=list)  # failed searches, so a dead key shows up

    def for_writer(self) -> str:
        if not self.notes:
            return ""
        return "\n\nDocs for the APIs in the issue (usage only; the issue decides what is correct):\n" + self.notes


def library_of(repo: str, issue: str) -> str:
    """The repo's own library first (an xarray issue imports numpy too), else the issue's first import."""
    name = repo.split("/")[-1].lower()
    for lib, site in DOCS.items():
        if lib in name or site.split(".")[0] == name:  # scikit-learn's library is sklearn
            return lib
    for imported in re.findall(r"(?:^|>>>\s*)\s*(?:from|import)\s+([A-Za-z_]\w*)", issue, re.M):
        return imported
    return name


def _code(issue: str) -> str:
    """The issue's code: fenced blocks, >>> lines and `inline` spans. A prose word before "(" is not an API."""
    return "\n".join(re.findall(r"```[^\n]*\n(.*?)```", issue, re.S) + re.findall(r"^\s*>>>(.*)$", issue, re.M)
                     + re.findall(r"`([^`\n]+)`", issue))


def api_names(issue: str, library: str) -> list[str]:
    """Up to two names the issue's code calls or passes, longest first: the likeliest real APIs."""
    code = _code(issue)
    found = re.findall(r"\b([A-Za-z_]\w*)\s*\(", code) + re.findall(r"=\s*([A-Za-z_]\w*)\b", code)
    names = list(dict.fromkeys(n for n in found if len(n) >= 4 and n not in _NOT_APIS and n != library))
    return sorted(names, key=len, reverse=True)[:2]


def _allowed(url: str) -> bool:
    parts = urlparse(url)
    host = parts.netloc.lower()
    on_code_host = any(host == h or host.endswith("." + h) for h in CODE_HOSTS)
    # web links only: sources are shown as links on public receipts (javascript://host/... has a host too)
    return (parts.scheme in ("http", "https") and bool(host) and not on_code_host
            and not any(v in url for v in SOURCE_VIEWS + INDEX_PAGES))


@traceable(name="research_brief")
async def research(repo: str, issue: str, search=None) -> Brief:
    library = library_of(repo, issue)
    brief = Brief(queries=[f"{library} {name}" for name in api_names(issue, library)])
    if not brief.queries:
        return brief
    try:
        if search is None:
            from langchain_tavily import TavilySearch

            search = TavilySearch(max_results=3, exclude_domains=CODE_HOSTS,
                                  include_domains=[DOCS[library]] if library in DOCS else DOC_DOMAINS)
    except Exception as e:  # no key: the writer works without docs, as it always did (one error per query)
        brief.errors += [f"{q}: {type(e).__name__}: {e}"[:300] for q in brief.queries]
        log.warning("research search unavailable: %s", e)
        return brief

    async def one(query: str) -> dict:
        try:  # no results, quota, timeout: the writer works without this part
            found = await asyncio.wait_for(search.ainvoke({"query": query}), SEARCH_TIMEOUT_S)
            return found if isinstance(found, dict) else {}
        except Exception as e:
            brief.errors.append(f"{query}: {type(e).__name__}: {e}"[:300])
            log.warning("research search failed: %s", brief.errors[-1])
            return {}

    notes = []
    for found in await asyncio.gather(*(one(q) for q in brief.queries)):  # results stay in query order
        for item in found.get("results", []):
            url = item.get("url") or ""
            if _allowed(url) and url not in {s["url"] for s in brief.sources}:
                brief.sources.append({"title": item.get("title") or url, "url": url})
                text = " ".join(str(item.get("content", "")).split())[:350]
                notes.append(f"- {item.get('title') or 'Docs'}: {text}")  # no link: a docs page can lead to source
    brief.notes = "\n".join(notes)[:NOTES_LIMIT]
    return brief
