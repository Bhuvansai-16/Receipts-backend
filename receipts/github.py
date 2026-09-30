"""GitHub for signed-in users: connection status, installations, repositories, pull requests, checks, webhook.

Access rule: a user reaches a repository only through an installation GitHub itself lists for them
(GET /user/installations with their own token); the setup redirect's installation_id is never trusted.
Tokens stay on the server; installation tokens are scoped per call (github_app.installation_token).
"""
import asyncio
import json
import logging
import re
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import auth, checks, config, engine, github_app, targets
from .checks import LIVE

router = APIRouter(prefix="/api/github")
log = logging.getLogger("uvicorn.error")
PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
_deliveries: OrderedDict[str, None] = OrderedDict()  # recent X-GitHub-Delivery ids (GitHub may redeliver)
# ponytail: per-process cache of each installation's repositories (GitHub's list call is ~1 s), cleared by
# Refresh and install webhooks; move it to the database if the API ever runs as several processes.
REPO_LIST_TTL_S = 60
_repo_lists: dict[int, tuple[float, list[dict]]] = {}


class AutoCheck(BaseModel):
    enabled: bool


def _runs(request: Request):
    return request.app.state.runs


def _require_app() -> None:
    if not github_app.configured():
        raise HTTPException(503, "The GitHub App isn't configured on this server yet.")


async def _gh(call):
    try:
        return await call
    except github_app.GitHubError as e:
        raise HTTPException(502, "GitHub didn't answer as expected. Try again in a moment.") from e


@router.get("/status")
async def status(request: Request, user: dict = Depends(auth.current_user), runs=Depends(_runs)) -> dict:
    return {"app_configured": github_app.configured(),
            "github_linked": bool(await auth.provider_token(request, "github")),
            "install_url": github_app.install_url() if github_app.configured() else None,
            "installations": await runs.user_installations(user["id"])}


async def _sync(request: Request, user: dict, runs) -> list[dict]:
    _require_app()
    token = await auth.provider_token(request, "github")
    if not token:
        raise HTTPException(409, "Connect your GitHub account first.")
    try:
        r = await github_app.api(token, "GET", "/user/installations", params={"per_page": 100})
    except github_app.GitHubError as e:
        # 403 when the GitHub sign-in is not this GitHub App (Neon's GitHub provider still points elsewhere)
        raise HTTPException(409, "Sign in with GitHub again so Receipts can see where its app is installed.") from e
    confirmed = [{"id": i["id"], "account_login": i["account"]["login"], "account_type": i["account"]["type"]}
                 for i in r.json().get("installations", []) if str(i.get("app_id")) == str(config.GITHUB_APP_ID)]
    await runs.sync_installations(user["id"], confirmed)
    for i in confirmed:
        _repo_lists.pop(i["id"], None)
    return confirmed


@router.post("/installations/sync")
async def sync(request: Request, user: dict = Depends(auth.current_user), runs=Depends(_runs)) -> dict:
    return {"installations": await _sync(request, user, runs)}


@router.get("/setup")
async def setup(request: Request, runs=Depends(_runs)) -> RedirectResponse:
    """GitHub's post-install redirect. installation_id in the URL is ignored: we ask GitHub instead."""
    try:
        user = await auth.current_user(request)
    except HTTPException:
        return RedirectResponse(f"{config.FRONTEND_URL}/signin?next=%2Fapp%2Frepos", status_code=302)
    try:
        await _sync(request, user, runs)
    except HTTPException:
        return RedirectResponse(f"{config.FRONTEND_URL}/app/repos?error=github", status_code=302)
    return RedirectResponse(f"{config.FRONTEND_URL}/app/repos?installed=1", status_code=302)


async def _installation_repos(installation_id: int) -> list[dict]:
    if (hit := _repo_lists.get(installation_id)) and hit[0] > time.monotonic():
        return hit[1]
    token = await _gh(github_app.installation_token(installation_id, None, {"metadata": "read"}))
    r = await _gh(github_app.api(token, "GET", "/installation/repositories", params={"per_page": 100}))
    repos = [{"id": x["id"], "full_name": x["full_name"], "private": x["private"], "language": x.get("language"),
              "description": x.get("description"), "pushed_at": x.get("pushed_at"), "url": x["html_url"],
              "installation_id": installation_id} for x in r.json().get("repositories", [])]
    _repo_lists[installation_id] = (time.monotonic() + REPO_LIST_TTL_S, repos)
    return repos


async def _user_repos(user: dict, runs) -> list[dict]:
    lists = await asyncio.gather(*(_installation_repos(i["id"]) for i in await runs.user_installations(user["id"])))
    repos = [r for rs in lists for r in rs]
    autos = await runs.auto_checks([r["id"] for r in repos])
    return [{**r, "auto_check": r["id"] in autos} for r in repos]


async def _find_repo(user: dict, runs, *, full_name: str | None = None, repo_id: int | None = None) -> dict:
    for repo in await _user_repos(user, runs):
        if repo["full_name"].lower() == (full_name or "").lower() or repo["id"] == repo_id:
            return repo
    raise HTTPException(404, "That repository isn't connected to your Receipts account.")


@router.get("/repos")
async def repos(user: dict = Depends(auth.current_user), runs=Depends(_runs)) -> dict:
    _require_app()
    return {"repos": await _user_repos(user, runs)}


@router.put("/repos/{repo_id}/auto-check")
async def auto_check(repo_id: int, body: AutoCheck, user: dict = Depends(auth.current_user),
                     runs=Depends(_runs)) -> dict:
    repo = await _find_repo(user, runs, repo_id=repo_id)
    await runs.set_auto_check(repo["id"], repo["installation_id"], repo["full_name"], body.enabled)
    return {"auto_check": body.enabled}


@router.get("/repos/{owner}/{name}/pulls")
async def pulls(owner: str, name: str, user: dict = Depends(auth.current_user), runs=Depends(_runs)) -> dict:
    repo = await _find_repo(user, runs, full_name=f"{owner}/{name}")
    token = await _gh(github_app.installation_token(repo["installation_id"], repo["id"],
                                                    {"pull_requests": "read", "metadata": "read"}))
    r = await _gh(github_app.api(token, "GET", f"/repos/{repo['full_name']}/pulls",
                                 params={"state": "open", "per_page": 30}))
    prs = r.json()
    latest = await runs.latest_for_prs(repo["full_name"], [p["number"] for p in prs])
    return {"repo": repo, "pulls": [{
        "number": p["number"], "title": p["title"], "author": (p.get("user") or {}).get("login"),
        "url": p["html_url"], "draft": p.get("draft", False), "updated_at": p.get("updated_at"),
        "head_sha": p["head"]["sha"], "linked_issue": targets.linked_issue(p.get("body")),
        "latest": latest.get(p["number"])} for p in prs]}


async def start_pr_check(runs, user_id: str, repo: dict, pr: dict) -> str:
    """Run a check on a PR (GitHub's pull request object) and mirror it into a 'Receipts' check run."""
    full, repo_id, inst = repo["full_name"], repo["id"], repo["installation_id"]
    number, head_sha = pr["number"], pr["head"]["sha"]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", full.replace("/", "__"))
    run_id = engine.new_run_id(f"{safe}-pr{number}", "github", taken=LIVE)
    details = f"{config.FRONTEND_URL}/runs/{run_id}"
    check = {}

    async def on_start(rid):
        check["id"] = await github_app.start_check(inst, repo_id, full, head_sha, details)
        if check["id"]:
            await runs.set_check_run(rid, check["id"])

    async def prepare():
        target, diff, _ = await targets.pr_target(inst, repo_id, full, number)
        return target, diff

    async def on_finish(status, evidence):
        await github_app.finish_check(inst, repo_id, full, check.get("id"),
                                      {**evidence, "reason": evidence.get("reason") or "the check failed to run"},
                                      details)

    source = {"repo": full, "pr_number": number, "head_sha": head_sha, "url": pr["html_url"],
              "title": pr.get("title"), "linked_issue": targets.linked_issue(pr.get("body"))}
    return await checks.launch(runs, user_id, run_id, f"{full}#{number}", "github", prepare, source=source,
                               on_start=on_start, on_finish=on_finish)


@router.post("/repos/{owner}/{name}/pulls/{number}/check", status_code=202)
async def check_pull(owner: str, name: str, number: int, user: dict = Depends(auth.current_user),
                     runs=Depends(_runs)) -> dict:
    repo = await _find_repo(user, runs, full_name=f"{owner}/{name}")
    await checks.enforce_limits(runs, user["id"])
    token = await _gh(github_app.installation_token(repo["installation_id"], repo["id"],
                                                    {"pull_requests": "read", "metadata": "read"}))
    try:
        pr = (await github_app.api(token, "GET", f"/repos/{repo['full_name']}/pulls/{number}")).json()
    except github_app.GitHubError as e:
        raise HTTPException(404 if e.status == 404 else 502, "Couldn't read that pull request.") from e
    if pr.get("state") != "open":
        raise HTTPException(409, "Only open pull requests can be checked.")
    return {"run_id": await start_pr_check(runs, user["id"], repo, pr)}


def _first_delivery(delivery: str) -> bool:
    if not delivery or delivery in _deliveries:
        return not delivery
    _deliveries[delivery] = None
    while len(_deliveries) > 1000:
        _deliveries.popitem(last=False)
    return True


@router.post("/webhook", status_code=202)
async def webhook(request: Request, runs=Depends(_runs)) -> dict:
    if not (config.GITHUB_WEBHOOK_SECRET and github_app.configured()):
        raise HTTPException(503, "The GitHub App isn't configured on this server yet.")
    body = await request.body()
    if not github_app.verify_signature(config.GITHUB_WEBHOOK_SECRET, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Signature doesn't match.")
    if not _first_delivery(request.headers.get("x-github-delivery", "")):
        return {"ok": True, "duplicate": True}
    event, payload = request.headers.get("x-github-event"), json.loads(body)
    if event in ("installation", "installation_repositories"):
        _repo_lists.pop(payload["installation"]["id"], None)
    if event == "installation" and payload.get("action") == "deleted":
        await runs.delete_installation(payload["installation"]["id"])
    elif event == "pull_request" and payload.get("action") in PR_ACTIONS and not payload["pull_request"].get("draft"):
        await _auto_check(runs, payload)
    return {"ok": True}


async def _auto_check(runs, payload: dict) -> None:
    repo, pr = payload["repository"], payload["pull_request"]
    if repo["id"] not in await runs.auto_checks([repo["id"]]):
        return
    login = (repo.get("owner") or {}).get("login", "").lower()
    if config.ALLOWED_GITHUB_ACCOUNTS and login not in config.ALLOWED_GITHUB_ACCOUNTS:
        log.info("auto-check skipped for %s: account not allowed", repo["full_name"])
        return
    owner = await runs.installation_owner(payload["installation"]["id"])
    head = pr["head"]["sha"]
    if not owner or await runs.has_run_for_head(repo["full_name"], pr["number"], head):
        return
    try:
        await checks.enforce_limits(runs, owner)
    except HTTPException as e:
        log.info("auto-check skipped for %s#%s: %s", repo["full_name"], pr["number"], e.detail)
        return
    await start_pr_check(runs, owner, {"id": repo["id"], "full_name": repo["full_name"],
                                       "installation_id": payload["installation"]["id"]}, pr)
