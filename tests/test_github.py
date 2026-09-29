import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from receipts import auth, config, github

AUTH = "https://ep-test.neonauth.aws.neon.tech/neondb/auth"
SESSION = "__Secure-neon-auth.session_token=tok123"
REPO = {"full_name": "octo/hello", "name": "hello", "html_url": "https://github.com/octo/hello",
        "description": "Hi", "private": False, "language": "Python", "stargazers_count": 3,
        "pushed_at": "2026-09-28T10:00:00Z", "updated_at": "2026-09-01T00:00:00Z", "owner": {"login": "octo"}}


@pytest.fixture
def fake(monkeypatch):
    """Neon Auth and GitHub behind one mock transport; replies can be swapped per test."""
    fake = SimpleNamespace(calls=[], token=lambda r: httpx.Response(200, json={"accessToken": "gho_secret"}),
                           repos=lambda r: httpx.Response(200, json=[REPO]))

    def handler(request: httpx.Request) -> httpx.Response:
        fake.calls.append(request)
        if request.url.host == "api.github.com":
            return fake.repos(request)
        if request.url.path.endswith("/get-session"):
            return httpx.Response(200, json={"user": {"id": "u1", "email": "a@b.c"}})
        return fake.token(request)

    monkeypatch.setattr(config, "NEON_AUTH_URL", AUTH)
    monkeypatch.setattr(config, "FRONTEND_URL", "http://localhost:5173")
    monkeypatch.setattr(auth, "_client", auth._new_client(httpx.MockTransport(handler)))
    auth.sessions.items.clear()
    return fake


@pytest.fixture
def client(fake):
    app = FastAPI()
    app.include_router(github.router)
    return TestClient(app)


def repos(client):
    return client.get("/api/github/repos", headers={"cookie": SESSION})


def test_lists_the_users_repos_without_exposing_the_token(client, fake):
    r = repos(client)
    assert r.status_code == 200 and r.json() == {"connected": True, "repos": [{
        "full_name": "octo/hello", "url": "https://github.com/octo/hello", "description": "Hi", "private": False,
        "language": "Python", "stars": 3, "pushed_at": "2026-09-28T10:00:00Z"}]}
    assert "gho_secret" not in r.text and r.headers["cache-control"] == "private, no-store"
    token_call = next(c for c in fake.calls if c.url.path.endswith("/get-access-token"))
    assert token_call.method == "POST" and json.loads(token_call.content) == {"providerId": "github"}
    assert token_call.headers["cookie"] == SESSION and token_call.headers["origin"] == "http://localhost:5173"
    gh = next(c for c in fake.calls if c.url.host == "api.github.com")
    assert gh.url.path == "/user/repos" and gh.headers["authorization"] == "Bearer gho_secret"
    assert gh.url.params["sort"] == "pushed"


def test_signed_in_without_github_is_not_connected(client, fake):
    fake.token = lambda r: httpx.Response(400, json={"message": "Account not found", "code": "ACCOUNT_NOT_FOUND"})
    assert repos(client).json() == {"connected": False, "repos": []}
    assert not any(c.url.host == "api.github.com" for c in fake.calls)


def test_revoked_github_token_is_not_connected(client, fake):
    fake.repos = lambda r: httpx.Response(401, json={"message": "Bad credentials"})
    assert repos(client).json() == {"connected": False, "repos": []}


def test_github_unreachable_is_502(client, fake):
    def down(request):
        raise httpx.ConnectError("down")

    fake.repos = down
    assert repos(client).status_code == 502


def test_needs_sign_in(client, fake):
    assert client.get("/api/github/repos").status_code == 401 and fake.calls == []
