"""The signed-in user's GitHub repositories, read with the token from their GitHub sign-in.

The token stays on the server. A sign-in token only sees public repositories; private ones need the
GitHub App (next step), not a broader sign-in scope.
"""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth

router = APIRouter(prefix="/api/github")
GITHUB_API = "https://api.github.com"
NOT_CONNECTED = {"connected": False, "repos": []}
NO_STORE = {"Cache-Control": "private, no-store"}  # per user: never let a shared browser cache replay it


def repo_summary(repo: dict) -> dict:
    return {"full_name": repo["full_name"], "url": repo["html_url"], "description": repo.get("description"),
            "private": repo["private"], "language": repo.get("language"), "stars": repo.get("stargazers_count", 0),
            "pushed_at": repo.get("pushed_at") or repo.get("updated_at")}


@router.get("/repos")
async def my_repos(request: Request, _user: dict = Depends(auth.current_user)) -> JSONResponse:
    token = await auth.provider_token(request, "github")
    if not token:  # signed in with email, not GitHub
        return JSONResponse(NOT_CONNECTED, headers=NO_STORE)
    try:
        r = await auth.http().get(f"{GITHUB_API}/user/repos", params={"sort": "pushed", "per_page": 30}, headers={
            "authorization": f"Bearer {token}", "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28", "user-agent": "receipts"})
    except httpx.HTTPError as e:
        raise HTTPException(502, "Couldn't reach GitHub. Try again in a moment.") from e
    if r.status_code == 401:  # access revoked on GitHub
        return JSONResponse(NOT_CONNECTED, headers=NO_STORE)
    if r.status_code != 200:
        raise HTTPException(502, "GitHub didn't return your repositories. Try again in a moment.")
    return JSONResponse({"connected": True, "repos": [repo_summary(repo) for repo in r.json()]}, headers=NO_STORE)
