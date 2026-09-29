# GitHub App, real PR checks, Google sign-in and new UI: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Check real GitHub pull requests end to end (GitHub App, button and Auto-check, check runs), add Google sign-in and "Connect GitHub", and rebuild the public site and the app UI.

**Architecture:** One GitHub App is both Neon's GitHub sign-in provider and the repo integration. The backend mints short-lived, repo-scoped installation tokens, builds a `RepoTarget` (claim from the linked issue, tarball at the merge base) that the unchanged engine runs in a Nebius sandbox, and mirrors the verdict into a GitHub check run. The frontend splits into `src/site/` (public pages) and `src/app/` (signed-in shell).

**Tech Stack:** FastAPI, psycopg 3, httpx, PyJWT (RS256) + cryptography; React 19, react-router 7, `@neondatabase/auth`.

**Spec:** `docs/superpowers/specs/2026-09-29-github-app-and-ui-design.md`

## Global Constraints

- No venv; global Python 3.12; config from `.env`. Never print secrets; tokens never reach the browser or a sandbox.
- Webhook signature: `X-Hub-Signature-256` = `sha256=` + HMAC-SHA256(secret, raw body), `hmac.compare_digest`.
- Installation tokens: scoped to one repository id and the permissions the call needs; cached until 5 minutes before `expires_at`.
- Real repos: Python + pytest only; `SANDBOX_PROVIDER=contree` required; environment failures give UNPROVEN.
- Check run conclusions: PROVEN=success, REFUTED/REGRESSION=failure, UNPROVEN/NO_CHECKABLE_CLAIM=neutral; check-run API errors never fail a run.
- Per-user limits (2 active, 20/day) apply to button and Auto-check runs; one run per (repo, PR, head SHA) from webhooks.
- UI: white/cream fields, black type, honey accents, no blue; illustrations are inline SVG; WCAG 2.2 AA; no em dashes in copy.
- Repo structure: backend keeps `receipts/` + `migrations/` + `tests/`; frontend `src/site/`, `src/app/`, `src/components/`.
- Files written by scripts use explicit UTF-8; use the Write/Edit tools for any text containing backslashes.

## Review Focus

1. Forged webhook (no or wrong signature): 401, nothing parsed or started. Test: `test_webhook_rejects_bad_signatures` (Task 4).
2. A user passes someone else's `installation_id` to the setup URL: nothing stored unless GitHub's `/user/installations` for that user lists it. Test: `test_sync_stores_only_what_github_confirms` (Task 4).
3. A user calls PR endpoints for a repo outside their installations: 404. Test: `test_pr_endpoints_require_the_users_installation` (Task 4).
4. GitHub redelivers the same `synchronize` event: one run only. Test: `test_webhook_runs_once_per_head_sha` (Task 4).
5. A repo that won't install: the run ends UNPROVEN with the setup error, not stuck. Test: `test_env_setup_failure_is_unproven` (Task 3).

## Rulings

- The UI tasks (6, 7) specify structure, copy, states and tests rather than full JSX: the pages are presentational and the owner asked to build UI directly; logic that can break (checklist state, labels) is unit-tested.

---

### Task 1: Google sign-in and "Connect GitHub" (frontend)

**Files:** `receipts-frontend/src/pages/AuthPage.tsx`, `src/auth.ts` (unchanged), `src/app/AccountPage.tsx` (Task 7 uses `linkGitHub`), `src/authFlow.ts`, `src/authFlow.test.ts`

**Interfaces:** Produces `socialCallbacks(next, origin, apiUrl) -> {callbackURL, errorCallbackURL}` in `authFlow.ts` (used by Google, GitHub sign-in and GitHub linking).

- [ ] **Step 1: Failing test** (`src/authFlow.test.ts`)

```ts
describe("socialCallbacks", () => {
  it("sends the browser back through the API's completion route to the requested page", () => {
    expect(socialCallbacks("/app/repos", "http://localhost:5173", "http://localhost:8000")).toEqual({
      callbackURL: "http://localhost:8000/api/auth/complete?next=http%3A%2F%2Flocalhost%3A5173%2Fapp%2Frepos",
      errorCallbackURL: "http://localhost:5173/signin?error=oauth",
    });
  });
});
```

- [ ] **Step 2: Run, expect FAIL** (`npx vitest run`: `socialCallbacks` is not exported).
- [ ] **Step 3: Implement** in `authFlow.ts`:

```ts
/** Neon returns the browser to the API, which finishes sign-in and forwards to `next` (receipts/auth.py). */
export function socialCallbacks(next: string, origin: string, apiUrl: string) {
  return {
    callbackURL: `${apiUrl}/api/auth/complete?next=${encodeURIComponent(origin + next)}`,
    errorCallbackURL: `${origin}/signin?error=oauth`,
  };
}
```
AuthPage: `social(provider: "github" | "google")` replaces `github()`; buttons "Continue with Google" (Google "G" mark SVG) above "Continue with GitHub".

- [ ] **Step 4: PASS**, build passes. **Step 5: Commit** `feat: Google sign-in and a shared social callback helper`.

---

### Task 2: Data for installations, repo settings and PR runs

**Files:** `migrations/002_github.sql`, `receipts/db.py`, `tests/test_runs_store.py`

**Interfaces (both `MemoryRuns` and `PgRuns`):**
- `create(run_id, user_id, instance_id, pr, started_at=None, source: dict | None = None)`; `source = {"repo", "pr_number", "head_sha"}`; `SUMMARY_KEYS` gains `repo`, `pr_number`, `head_sha`
- `set_check_run(run_id, check_run_id)`, `has_run_for_head(repo, pr_number, head_sha) -> bool`
- `latest_for_prs(repo, numbers) -> dict[int, dict]` (id, status, verdict of the newest run per PR)
- `sync_installations(user_id, installations: list[dict])` (each `{id, account_login, account_type}`; replaces this user's links, keeps others')
- `user_installations(user_id) -> list[dict]`, `installation_owner(installation_id) -> str | None`, `delete_installation(installation_id)`
- `set_auto_check(repo_id, installation_id, full_name, enabled)`, `auto_checks(repo_ids) -> set[int]`

- [ ] **Step 1: Migration** `migrations/002_github.sql` exactly as in the spec's Data section.
- [ ] **Step 2: Failing tests** (parametrized over memory/Neon like the existing ones):

```python
def test_pr_runs_keep_their_source_and_latest_per_pr(store, run):
    async def go():
        await store.create("a", "u1", "octo/hello#1", "github", T0, {"repo": "octo/hello", "pr_number": 1, "head_sha": "s1"})
        await store.create("b", "u1", "octo/hello#1", "github", T0 + timedelta(minutes=1), {"repo": "octo/hello", "pr_number": 1, "head_sha": "s2"})
        await store.finish("b", "done", EVIDENCE)
        return (await store.latest_for_prs("octo/hello", [1, 2]), await store.has_run_for_head("octo/hello", 1, "s1"),
                await store.has_run_for_head("octo/hello", 1, "s3"))
    latest, seen, unseen = run(go())
    assert latest[1]["id"] == "b" and latest[1]["verdict"] == "PROVEN" and 2 not in latest and seen and not unseen


def test_installations_sync_per_user(store, run):
    async def go():
        await store.sync_installations("u1", [{"id": 7, "account_login": "octo", "account_type": "User"}])
        await store.sync_installations("u2", [{"id": 7, "account_login": "octo", "account_type": "User"}])
        await store.sync_installations("u1", [])
        return await store.user_installations("u1"), await store.user_installations("u2"), await store.installation_owner(7)
    mine, theirs, owner = run(go())
    assert mine == [] and [i["id"] for i in theirs] == [7] and owner == "u2"


def test_auto_check_switch(store, run):
    async def go():
        await store.sync_installations("u1", [{"id": 7, "account_login": "octo", "account_type": "User"}])
        await store.set_auto_check(42, 7, "octo/hello", True)
        on = await store.auto_checks([42, 43])
        await store.set_auto_check(42, 7, "octo/hello", False)
        return on, await store.auto_checks([42])
    assert run(go()) == ({42}, set())
```

- [ ] **Step 3: FAIL. Step 4: Implement** (memory: dicts `installations`, `installation_users` (list of (id, user, created)), `repo_settings`; Postgres: plain SQL; `sync_installations` upserts `github_installations` then `DELETE FROM github_installation_users WHERE user_id=%s` and inserts the confirmed ids; owner = earliest `created`/insertion). **Step 5: PASS; migrate Neon** (`python -m receipts migrate` → `applied: 002_github`). **Step 6: Commit** `feat: store GitHub installations, repo settings and PR runs`.

---

### Task 3: GitHub App client and real-repo targets

**Files:** `receipts/config.py`, `receipts/github_app.py`, `receipts/targets.py`, `receipts/engine.py`, `receipts/swebench.py`, `tests/test_github_app.py`, `tests/test_targets.py`

**Interfaces:**
- `config.API_URL`, `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_WEBHOOK_SECRET`, `github_private_key() -> str`
- `github_app.configured() -> bool`, `app_jwt(now=None) -> str`, `verify_signature(secret, body, header) -> bool`, `install_url() -> str`
- `await installation_token(installation_id, repo_id=None, permissions=None) -> str`
- `await api(token, method, path, **kw) -> httpx.Response` (raises `GitHubError` on >= 400 unless `ok=(...)`)
- `CONCLUSION: dict[str, str]`, `await start_check(installation_id, repo_id, full_name, head_sha, details_url) -> int | None`, `await finish_check(installation_id, repo_id, full_name, check_run_id, verdict, reason, details_url)`
- `targets.RepoTarget(instance_id, repo, problem_statement, tarball, base_sha, suite, pass_to_pass=[])` with `async base_image()`
- `targets.linked_issue(body) -> int | None`, `claim_text(pr, issue) -> str`, `suite_for(changed, base_files) -> list[str]`, `tar_files(tarball) -> set[str]`
- `await targets.pr_target(installation_id, repo_id, full_name, number) -> (RepoTarget, diff, head_sha)`
- Engine: `check(target, patch, emit)` accepts `RepoTarget`; `swebench.Instance.base_image()`.

- [ ] **Step 1: Failing tests** `tests/test_github_app.py`:

```python
def test_verify_signature():
    body = b'{"zen":"hi"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert github_app.verify_signature("s3cret", body, good)
    assert not github_app.verify_signature("s3cret", body, good.replace("a", "b", 1))
    assert not github_app.verify_signature("s3cret", body, None) and not github_app.verify_signature("", body, good)

def test_app_jwt_claims(monkeypatch, rsa_key):  # fixture generates a throwaway RSA key
    monkeypatch.setattr(config, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(config, "github_private_key", lambda: rsa_key.private_pem)
    claims = jwt.decode(github_app.app_jwt(now=1_000_000), rsa_key.public_pem, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}

def test_installation_token_is_scoped_and_cached(fake_github):
    t1 = run(github_app.installation_token(7, repo_id=42, permissions={"contents": "read"}))
    t2 = run(github_app.installation_token(7, repo_id=42, permissions={"contents": "read"}))
    [call] = [c for c in fake_github.calls if c.url.path == "/app/installations/7/access_tokens"]
    assert t1 == t2 and json.loads(call.content) == {"repository_ids": [42], "permissions": {"contents": "read"}}
    assert call.headers["authorization"].startswith("Bearer ey")

def test_check_run_conclusions():
    assert github_app.CONCLUSION == {"PROVEN": "success", "REFUTED": "failure", "REGRESSION": "failure",
                                     "UNPROVEN": "neutral", "NO_CHECKABLE_CLAIM": "neutral"}
```
`tests/test_targets.py`:

```python
def test_linked_issue():
    assert targets.linked_issue("Fixes #12 and more") == 12
    assert targets.linked_issue("closes: #7") == 7
    assert targets.linked_issue("see #3") is None and targets.linked_issue(None) is None

def test_claim_prefers_the_issue():
    pr = {"title": "Fix crash", "body": "Fixes #3"}
    assert targets.claim_text(pr, {"title": "Crash on empty list", "body": "Steps..."}) == "Crash on empty list\n\nSteps..."
    assert targets.claim_text(pr, None) == "Fix crash\n\nFixes #3"

def test_suite_for_picks_related_existing_tests():
    base = {"pkg/core.py", "tests/test_core.py", "tests/test_other.py", "tests/test_new.py"}
    assert targets.suite_for(["pkg/core.py", "tests/test_other.py", "tests/test_added.py"], base) == \
        ["tests/test_core.py", "tests/test_other.py"]

def test_tar_files_strips_the_top_folder():
    assert targets.tar_files(make_tarball({"octo-hello-abc/pkg/a.py": b"", "octo-hello-abc/README.md": b""})) == {"pkg/a.py", "README.md"}

def test_env_setup_failure_is_unproven(monkeypatch):
    target = targets.RepoTarget("octo/hello#1", "octo/hello", "Crash on empty list", b"", "sha", [])
    async def broken(): raise targets.EnvironmentSetupError("pip install failed: no setup.py")
    monkeypatch.setattr(target, "base_image", broken)
    monkeypatch.setattr(engine, "classify", fake_fix_claim)  # claim kind "fix" without a model call
    ev = run(engine.check(target, "diff --git a/x b/x\n"))
    assert ev["verdict"] == "UNPROVEN" and "pip install failed" in ev["reason"]
```

- [ ] **Step 2: FAIL. Step 3: Implement.**

`config.py`:
```python
API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def github_private_key() -> str:
    """PEM from GITHUB_APP_PRIVATE_KEY (\\n-escaped) or the file at GITHUB_APP_PRIVATE_KEY_PATH."""
    if key := os.environ.get("GITHUB_APP_PRIVATE_KEY"):
        return key.replace("\\n", "\n")
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    return Path(path).read_text(encoding="utf-8") if path else ""
```

`github_app.py`:
```python
"""GitHub App plumbing: app JWT, scoped installation tokens, REST calls, webhook signatures, check runs."""
import hashlib
import hmac
import logging
import time
from datetime import datetime

import httpx
import jwt

from . import auth, config

API = "https://api.github.com"
HEADERS = {"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28", "user-agent": "receipts"}
CONCLUSION = {"PROVEN": "success", "REFUTED": "failure", "REGRESSION": "failure", "UNPROVEN": "neutral",
              "NO_CHECKABLE_CLAIM": "neutral"}
log = logging.getLogger("uvicorn.error")
_tokens: dict[tuple, tuple[float, str]] = {}


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def configured() -> bool:
    return bool(config.GITHUB_APP_ID and config.GITHUB_APP_SLUG and config.github_private_key())


def install_url() -> str:
    return f"https://github.com/apps/{config.GITHUB_APP_SLUG}/installations/new"


def app_jwt(now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    # iat 60 s in the past absorbs clock drift; GitHub rejects exp more than 10 minutes out
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": config.GITHUB_APP_ID},
                      config.github_private_key(), algorithm="RS256")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def api(token: str, method: str, path: str, *, ok=(200, 201), accept: str | None = None,
              **kw) -> httpx.Response:
    headers = {**HEADERS, "authorization": f"Bearer {token}", **({"accept": accept} if accept else {})}
    try:
        r = await auth.http().request(method, f"{API}{path}", headers=headers, **kw)
    except httpx.HTTPError as e:
        raise GitHubError(502, f"unreachable ({type(e).__name__})") from e
    if r.status_code not in ok:
        raise GitHubError(r.status_code, r.text[:200])
    return r


async def installation_token(installation_id: int, repo_id: int | None = None,
                             permissions: dict | None = None) -> str:
    """Token for one installation, narrowed to one repository and the permissions a call needs."""
    key = (installation_id, repo_id, tuple(sorted((permissions or {}).items())))
    if (hit := _tokens.get(key)) and hit[0] > time.time():
        return hit[1]
    body = {**({"repository_ids": [repo_id]} if repo_id else {}), **({"permissions": permissions} if permissions else {})}
    r = await api(app_jwt(), "POST", f"/app/installations/{installation_id}/access_tokens", ok=(201,), json=body)
    data = r.json()
    expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp() - 300
    _tokens[key] = (expires, data["token"])
    return data["token"]


async def start_check(installation_id, repo_id, full_name, head_sha, details_url) -> int | None:
    try:
        token = await installation_token(installation_id, repo_id, {"checks": "write"})
        r = await api(token, "POST", f"/repos/{full_name}/check-runs", json={
            "name": "Receipts", "head_sha": head_sha, "status": "in_progress", "details_url": details_url})
        return r.json()["id"]
    except GitHubError as e:  # a missing check run must never stop the check itself
        log.warning("check run not created for %s: %s", full_name, e)
        return None


async def finish_check(installation_id, repo_id, full_name, check_run_id, verdict, reason, details_url) -> None:
    if not check_run_id:
        return
    try:
        token = await installation_token(installation_id, repo_id, {"checks": "write"})
        await api(token, "PATCH", f"/repos/{full_name}/check-runs/{check_run_id}", json={
            "status": "completed", "conclusion": CONCLUSION.get(verdict, "neutral"), "details_url": details_url,
            "output": {"title": f"Receipts: {verdict}", "summary": f"{reason}\n\nFull receipt: {details_url}"}})
    except GitHubError as e:
        log.warning("check run %s not completed: %s", check_run_id, e)
```

`targets.py`:
```python
"""Real GitHub repositories as check targets (the counterpart of swebench.Instance)."""
import io
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import config, github_app
from .engine import changed_files

CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.I)
MAX_TARBALL_BYTES = 50_000_000
READ = {"contents": "read", "pull_requests": "read", "issues": "read", "metadata": "read"}
# The engine's sandbox commands activate SWE-bench's conda env; here the image's python *is* the env.
SETUP = """set -e
mkdir -p /testbed /opt/miniconda3/etc/profile.d
printf 'conda() { :; }\\n' > /opt/miniconda3/etc/profile.d/conda.sh
tar -xzf /tmp/src.tar.gz -C /testbed --strip-components=1
cd /testbed
pip install -q --disable-pip-version-check -e '.[test]' || pip install -q -e '.[tests]' \\
  || pip install -q -e '.[dev]' || pip install -q -e . || true
for f in requirements*.txt requirements/*.txt; do [ -f "$f" ] && pip install -q -r "$f" || true; done
pip install -q pytest"""
_envs: dict[tuple[str, str], object] = {}


class EnvironmentSetupError(RuntimeError):
    pass


def linked_issue(body: str | None) -> int | None:
    m = CLOSING.search(body or "")
    return int(m.group(1)) if m else None


def claim_text(pr: dict, issue: dict | None) -> str:
    src = issue or pr
    return f"{src.get('title') or ''}\n\n{src.get('body') or ''}".strip()


def tar_files(tarball: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        return {m.name.split("/", 1)[1] for m in tf.getmembers() if m.isfile() and "/" in m.name}


def suite_for(changed: list[str], base_files: set[str]) -> list[str]:
    """Existing tests to guard: test files the PR touches, plus test_<module>.py for each changed module."""
    tests = {p for p in base_files if PurePosixPath(p).name.startswith("test_") and p.endswith(".py")}
    picked = {p for p in changed if p in tests}
    stems = {PurePosixPath(p).stem for p in changed if p.endswith(".py") and p not in tests}
    picked |= {p for p in tests if PurePosixPath(p).stem.removeprefix("test_") in stems}
    return sorted(picked)


@dataclass
class RepoTarget:
    instance_id: str
    repo: str
    problem_statement: str
    tarball: bytes = field(repr=False)
    base_sha: str
    suite: list[str]
    pass_to_pass: list[str] = field(default_factory=list)

    async def base_image(self):
        if config.SANDBOX_PROVIDER != "contree":
            raise EnvironmentSetupError("real repositories need SANDBOX_PROVIDER=contree (Nebius sandboxes)")
        key = (self.repo, self.base_sha)
        if key not in _envs:  # ponytail: per-process cache; rebuilt after a restart
            image = await config.contree().images.oci("docker://docker.io/library/python:3.11")
            env = await image.run(shell=SETUP, files={"/tmp/src.tar.gz": self.tarball}, disposable=False,
                                  timeout=config.SANDBOX_TIMEOUT_S)
            if env.exit_code != 0:
                from .sandbox import text
                raise EnvironmentSetupError(f"setting up {self.repo} failed: {text(env.stderr)[-300:]}")
            _envs[key] = env
        return _envs[key]


async def pr_target(installation_id: int, repo_id: int, full_name: str, number: int):
    """(target, diff, head_sha) for a PR. The token only reads this one repository."""
    token = await github_app.installation_token(installation_id, repo_id, READ)
    pr = (await github_app.api(token, "GET", f"/repos/{full_name}/pulls/{number}")).json()
    head = pr["head"]["sha"]
    compare = (await github_app.api(token, "GET", f"/repos/{full_name}/compare/{pr['base']['sha']}...{head}")).json()
    base_sha = compare["merge_base_commit"]["sha"]
    diff = (await github_app.api(token, "GET", f"/repos/{full_name}/pulls/{number}",
                                 accept="application/vnd.github.diff")).text
    issue = None
    if n := linked_issue(pr.get("body")):
        r = await github_app.api(token, "GET", f"/repos/{full_name}/issues/{n}", ok=(200, 404, 410))
        issue = r.json() if r.status_code == 200 and "pull_request" not in r.json() else None
    tar = await github_app.api(token, "GET", f"/repos/{full_name}/tarball/{base_sha}", follow_redirects=True)
    if len(tar.content) > MAX_TARBALL_BYTES:
        raise EnvironmentSetupError(f"{full_name} is larger than {MAX_TARBALL_BYTES // 1_000_000} MB")
    suite = suite_for(changed_files(diff), tar_files(tar.content))
    return RepoTarget(f"{full_name}#{number}", full_name, claim_text(pr, issue), tar.content, base_sha, suite), diff, head
```

Engine edits (`engine.py`): `base = await inst.base_image()`; `files = getattr(inst, "suite", None) or suite_files(p2p)`; `base_suites = ([restrict(res[2], p2p) if p2p else res[2]], [res[3]])`. `swebench.Instance` gains `async def base_image(self): return await base_image(self.instance_id)`.

- [ ] **Step 4: PASS** (`python -m pytest tests/test_github_app.py tests/test_targets.py -q`), whole suite green. **Step 5: Commit** `feat: GitHub App client and real-repository check targets`.

---

### Task 4: GitHub endpoints, webhook, check runs, usage

**Files:** `receipts/github.py`, `receipts/server.py`, `tests/test_github.py`, `tests/test_server.py`

**Interfaces:** endpoints per the spec's API table. `server.start_check_run(runs, user_id, run_label_id, instance_id, pr_kind, prepare, source=None, on_start=None, on_finish=None) -> run_id` where `prepare()` returns `(target, patch)` inside the task (so downloads and setup show as progress); SWE-bench `/api/runs` uses it too. `server.enforce_limits(runs, user_id)` raises 429.

Behavior:
- `status`: `{app_configured, github_linked (provider_token found), install_url, installations}`.
- `setup` (GET, cookie): sync, then 302 to `{FRONTEND_URL}/app/repos?installed=1` (or `?error=github` on failure).
- `sync`: `GET /user/installations` with the user's GitHub token (`auth.provider_token`); store exactly the listed ids.
- `repos`: for each of the user's installations, installation token → `GET /installation/repositories?per_page=100`; each repo `{id, full_name, private, language, pushed_at, url, installation_id, auto_check}`.
- `pulls`: resolve `owner/repo` inside the user's installations or 404; `GET /repos/{full}/pulls?state=open&per_page=30`; each `{number, title, author, url, draft, updated_at, head_sha, linked_issue, latest}` with `latest` from `runs.latest_for_prs`.
- `check`: resolve repo (404 if not the user's), `enforce_limits`, start run with `prepare = targets.pr_target(...)`, `on_start` creates the check run, `on_finish` completes it.
- `webhook`: 503 if no secret; 401 on bad signature; dedupe `X-GitHub-Delivery`; `installation.deleted` → delete; `pull_request` (opened, synchronize, reopened, ready_for_review; not draft) on an Auto-check repo → owner = `installation_owner`, skip if `has_run_for_head` or limits exceeded, else start; always 202.
- `/api/me` gains `usage`.

Tests (fake GitHub + fake Neon on one `httpx.MockTransport`, fake `start` recorder instead of real engine runs):

```python
def test_webhook_rejects_bad_signatures(client): ...          # missing/wrong -> 401, nothing started
def test_sync_stores_only_what_github_confirms(client): ...   # setup with installation_id=99 not in /user/installations -> not stored
def test_pr_endpoints_require_the_users_installation(client): ...  # other repo -> 404
def test_check_button_starts_a_run_and_a_check_run(client): ...    # POST check -> run_id; check-runs POST seen
def test_webhook_runs_once_per_head_sha(client): ...          # same delivery twice / same sha -> one run
def test_webhook_ignores_repos_without_auto_check(client): ...
def test_repos_and_pulls_shapes(client): ...
def test_me_reports_usage(api): ...
```

- [ ] Steps: failing tests → implement → PASS → full suite → commit `feat: GitHub repositories, pull requests, checks and webhook`.

---

### Task 5: Receipt display for PR runs (frontend model)

**Files:** `src/api.ts`, `src/receipt.ts`, `src/receipt.test.ts`, `src/components/ReceiptCard.tsx`

- Evidence gains `source?: {repo, pr_number, head_sha, url}`; the card's meta line reads `octo/hello · PR #12` for PR runs.
- Test: `receiptTitle(evidence)` returns `{repo: "octo/hello", label: "PR #12"}` for a PR run and `{repo: "psf/requests", label: "issue #1142"}` for SWE-bench.

---

### Task 6: Public site (layout, pages, illustrations)

**Files:** `src/site/SiteLayout.tsx` (navbar + footer + `<Outlet/>`), `src/site/Navbar.tsx`, `src/site/Footer.tsx`, `src/site/illustrations.tsx`, `src/site/HomePage.tsx`, `src/site/HowItWorksPage.tsx`, `src/site/SecurityPage.tsx`, `src/site/DocsPage.tsx`, `src/site/site.css`; routes in `src/App.tsx`.

- Navbar: logo; links How it works, Security, Docs; right side Sign in + Get started (signed out) or Open app (signed in); sticky, hairline border; mobile: menu button (aria-expanded) opening a sheet.
- Footer: 4 columns (Product: How it works, Security, Docs; Get started: Sign up, Sign in, Example receipt; Built with: Nebius Token Factory, Neon, LangSmith, Tavily; Source: backend and frontend repos); bottom line "Receipts. Evidence, not opinions."
- Illustrations (inline SVG, soft clay style: rounded shapes, radial-gradient shading, one soft shadow, cream `#F6EFE3`/honey/black): `HeroPrinter` (receipt printing from a PR card with a magnifier), `StepIssue`, `StepSandbox`, `StepReceipt`, `ShieldLock`.
- Home sections: hero (pill "Proof for pull requests", headline "Every pull request makes a claim. Get the receipt.", lead, CTAs Get started / See an example receipt, `HeroPrinter`), strip "Built on Nebius Token Factory · Neon · LangSmith · Tavily", How it works (3 cards with Step illustrations), Verdicts (5 chips with one sentence each), Why trust it (blind test, 3 + 3 runs, isolated sandboxes, second opinion; `ShieldLock`), example receipt, CTA band ("Check your next pull request"), footer.
- How it works: the pipeline as 6 numbered stages with the live-receipt lines they produce.
- Security: blindness guarantees, sandboxes, GitHub permissions table, data kept, token handling.
- Docs: Getting started, Connect GitHub, Auto-check, Verdicts, Limits, FAQ (in-page anchors).
- Browser check: desktop + 375 px, no horizontal scroll, keyboard focus visible, all links resolve.

---

### Task 7: App shell and pages

**Files:** `src/app/AppLayout.tsx` (sidebar/tab bar + `<Outlet/>`), `src/app/Overview.tsx`, `src/app/Repositories.tsx`, `src/app/PullRequests.tsx`, `src/app/ReceiptsPage.tsx`, `src/app/AccountPage.tsx`, `src/app/checklist.ts` + test; `NewCheck.tsx` becomes Try a demo at `/app/demo`; `api.ts` gains `githubStatus`, `githubRepos`, `setAutoCheck`, `pulls`, `checkPull`, `syncInstallations`, `me` (usage).

- `checklist(status, repos, runs)` → `[{id:"connect", done}, {id:"repos", done}, {id:"check", done}]`; test covers the 4 states (nothing, linked, installed, checked).
- Overview: greeting, checklist cards with the one action each (Connect GitHub → `linkSocial`, Add repositories → install URL, Check a PR → first repo's PRs), recent receipts (5), "N of 20 checks left today".
- Repositories: empty state explains install; list with Auto-check switch (role="switch"), "Add repositories", "Refresh" (sync).
- Pull requests: list with title, #, author, linked issue, latest verdict chip, "Check this PR" (disabled while one is running), link to receipt.
- Receipts: paged history (load more by cursor).
- Account: email, connected accounts (from `list-accounts`), Connect GitHub button when missing, sign out.
- `/app` routes guarded; sidebar collapses to a bottom tab bar under 900 px.

---

### Task 8: Docs, env, live verification

- Backend `.env.example` + README: GitHub App registration steps (permissions, URLs, events), smee forwarding, new env vars.
- Frontend README: routes and structure.
- Live: register-app instructions to the owner; once set, sign in with GitHub, install on a small public Python repo, check one PR (button), then Auto-check with a push.
