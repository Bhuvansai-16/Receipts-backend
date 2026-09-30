"""Research brief: the library's own docs for the APIs an issue names, fetched before the test writer starts.

The writer had Tavily as an optional tool and called it 0 times in 40 runs; this step always runs. Blind like the
writer: queries come from the issue text alone, code hosts are excluded (by Tavily and again here), and "view
source" pages are dropped, since a newer docs build can show the fixed code. Any failure gives an empty brief;
research never fails or stalls a check.
"""
import asyncio
import builtins
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
_BUILTINS = set(dir(builtins))


@dataclass
class Brief:
    queries: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)  # {"title", "url"}
    notes: str = ""

    def for_writer(self) -> str:
        if not self.notes:
            return ""
        return "\n\nDocs for the APIs in the issue (usage only; the issue decides what is correct):\n" + self.notes


def library_of(repo: str, issue: str) -> str:
    for name in re.findall(r"(?:^|>>>\s*)\s*(?:from|import)\s+([A-Za-z_]\w*)", issue, re.M):
        return name
    name = repo.split("/")[-1].lower()
    return next((lib for lib in DOCS if lib in name), name)


def api_names(issue: str, library: str) -> list[str]:
    """Up to two names the issue calls or passes, longest first: the likeliest real APIs."""
    found = re.findall(r"\b([A-Za-z_]\w*)\s*\(", issue) + re.findall(r"=\s*([A-Za-z_]\w*)\b", issue)
    names = list(dict.fromkeys(n for n in found if len(n) >= 4 and n not in _BUILTINS and n != library))
    return sorted(names, key=len, reverse=True)[:2]


def _allowed(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    on_code_host = any(host == h or host.endswith("." + h) for h in CODE_HOSTS)
    return bool(host) and not on_code_host and not any(v in url for v in SOURCE_VIEWS + INDEX_PAGES)


@traceable(name="research_brief")
async def research(repo: str, issue: str, search=None) -> Brief:
    library = library_of(repo, issue)
    brief = Brief(queries=[f"{library} {name}" for name in api_names(issue, library)])
    if not brief.queries:
        return brief
    notes = []
    try:
        if search is None:
            from langchain_tavily import TavilySearch

            search = TavilySearch(max_results=3, include_domains=[DOCS[library]] if library in DOCS else DOC_DOMAINS,
                                  exclude_domains=CODE_HOSTS)
        for query in brief.queries:
            found = await asyncio.wait_for(search.ainvoke({"query": query}), SEARCH_TIMEOUT_S)
            for item in found.get("results", []) if isinstance(found, dict) else []:
                url = item.get("url") or ""
                if _allowed(url) and url not in {s["url"] for s in brief.sources}:
                    title = item.get("title") or url
                    brief.sources.append({"title": title, "url": url})
                    notes.append(f"- {title} ({url}): {' '.join(str(item.get('content', '')).split())[:350]}")
    except Exception:  # no key, network, quota, timeout: the writer works without a brief, as it always did
        pass
    brief.notes = "\n".join(notes)[:NOTES_LIMIT]
    return brief
