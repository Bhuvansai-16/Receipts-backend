"""GitHub App plumbing: app JWT, scoped installation tokens, REST calls, webhook signatures, check runs.

Docs: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app
"""
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
