"""GitHub App plumbing: app JWT, scoped installation tokens, REST calls, webhook signatures, check runs.

Docs: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app
"""
import hashlib
import hmac
import logging
import re
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
    # iat 60 s in the past absorbs clock drift; GitHub rejects an exp more than 10 minutes out
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": config.GITHUB_APP_ID},
                      config.github_private_key(), algorithm="RS256")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is sha256=HMAC-SHA256(secret, raw body); compared in constant time."""
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
    """A token for one installation, narrowed to one repository and the permissions a call needs."""
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
        r = await _check_run_call(token, "POST", f"/repos/{full_name}/check-runs", {
            "name": "Receipts", "head_sha": head_sha, "status": "in_progress", "details_url": details_url})
        return r.json()["id"]
    except GitHubError as e:  # a missing check run must never stop the check itself
        log.warning("check run not created for %s: %s", full_name, e)
        return None


HEADLINE = {
    "PROVEN": "Proven: the pull request does what it claims",
    "REFUTED": "Refuted: the test still fails the same way with this change",
    "REGRESSION": "Regression: the fix breaks tests that passed before",
    "UNPROVEN": "Unproven: not enough evidence either way (this says nothing against the PR)",
    "NO_CHECKABLE_CLAIM": "No checkable claim: the PR doesn't claim to fix a bug",
}
LIMIT = 60_000  # GitHub allows 65,535 characters per output field


def _fails(runs) -> int:
    return sum(1 for r in runs if r.get("not_passed")) if isinstance(runs, list) else 0


def _fenced(code: str) -> str:
    """A code block whose fence is longer than any backtick run in the code, so the code can't close it."""
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}python\n{code}\n{fence}"


def check_output(ev: dict, details_url: str) -> dict:
    """The check run's title, summary and text: what the verdict means and the evidence behind it."""
    verdict = ev.get("verdict") or "UNPROVEN"
    headline = HEADLINE.get(verdict, verdict)
    lines = [f"**{headline}**", ""]
    if claim := (ev.get("claim") or {}).get("claim"):
        lines.append(f"Claim: {claim[:1000]}")
    forks = ev.get("forks") or {}
    base, pr = forks.get("base_with_test"), forks.get("pr_with_test")
    if isinstance(base, list) and base:
        lines.append(f"- Blind test fails on the original code in {_fails(base)} of {len(base)} runs")
    if isinstance(pr, list) and pr:
        lines.append(f"- Blind test passes with this pull request in {len(pr) - _fails(pr)} of {len(pr)} runs")
        still = next((f.get("msg") or "" for r in pr for f in r.get("not_passed", {}).values()), None)
        if still:
            lines.append(f"- Still failing with the change: {still.splitlines()[0][:500]}")
    elif isinstance(pr, str):
        lines.append("- The pull request's patch did not apply at its base, so it was never run")
    if (opinion := ev.get("second_opinion")) and opinion.get("about") == "mixed":
        view = "the failing check matches the issue" if opinion.get("faithful") else "the test may be wrong here"
        lines.append(f"- Second opinion: {view}: {(opinion.get('reason') or '')[:1000]}")
    lines += ["", f"Reason: {(ev.get('reason') or '')[:2000]}", "", f"Full receipt: {details_url}"]
    code = "\n".join(((ev.get("writer") or {}).get("test_code") or "").splitlines()[:60])
    text = f"### Blind test (written from the issue alone)\n{_fenced(code[:LIMIT])}" if code else ""
    return {"title": headline.split(":")[0], "summary": "\n".join(lines)[:LIMIT], "text": text[:LIMIT]}


async def finish_check(installation_id, repo_id, full_name, check_run_id, evidence: dict, details_url) -> None:
    if not check_run_id:
        return
    try:
        token = await installation_token(installation_id, repo_id, {"checks": "write"})
        await _check_run_call(token, "PATCH", f"/repos/{full_name}/check-runs/{check_run_id}", {
            "status": "completed", "conclusion": CONCLUSION.get(evidence.get("verdict") or "UNPROVEN", "neutral"),
            "details_url": details_url, "output": check_output(evidence, details_url)})
    except GitHubError as e:
        log.warning("check run %s not completed: %s", check_run_id, e)


async def _check_run_call(token: str, method: str, path: str, body: dict) -> httpx.Response:
    try:
        return await api(token, method, path, json=body)
    except GitHubError as e:
        if e.status != 422 or "details_url" not in body:
            raise
        # GitHub can refuse a link it won't show (e.g. http://localhost in development); the summary still has it
        return await api(token, method, path, json={k: v for k, v in body.items() if k != "details_url"})
