import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from receipts import auth, config

AUTH = "https://ep-test.neonauth.aws.neon.tech/neondb/auth"
SESSION = "__Secure-neon-auth.session_token=tok123"
CHALLENGE = "__Secure-neon-auth.session_challenge=chal456"
USER = {"id": "u1", "email": "a@b.c"}


@pytest.fixture
def upstream(monkeypatch):
    """Fake Neon Auth: records each request and answers with `upstream.reply(request)`."""
    fake = SimpleNamespace(calls=[], reply=lambda request: httpx.Response(200, json={"user": USER}))

    def handler(request: httpx.Request) -> httpx.Response:
        fake.calls.append(request)
        return fake.reply(request)

    monkeypatch.setattr(config, "NEON_AUTH_URL", AUTH)
    monkeypatch.setattr(config, "FRONTEND_URL", "http://localhost:5173")
    monkeypatch.setattr(config, "COOKIE_DOMAIN", None)
    monkeypatch.setattr(auth, "_client", auth._new_client(httpx.MockTransport(handler)))
    auth.sessions.items.clear()
    return fake


@pytest.fixture
def client(upstream):
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/me")
    async def me(user: dict = Depends(auth.current_user)):
        return user

    return TestClient(app, follow_redirects=False)


def test_neon_cookies_keeps_only_neon_auth_cookies():
    assert auth.neon_cookies(f"theme=dark; {SESSION}; other=1") == SESSION
    assert auth.neon_cookies("") == ""


def test_rewrite_set_cookie_makes_it_first_party():
    out = auth.rewrite_set_cookie(f"{SESSION}; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT; "
                                  "SameSite=None; Partitioned; Domain=neon.tech", ".example.com")
    parts = [p.strip() for p in out.split(";")]
    assert parts[0] == SESSION and "Partitioned" not in parts and "SameSite=None" not in parts
    assert "Domain=neon.tech" not in parts and "Expires=Wed, 21 Oct 2026 07:28:00 GMT" in parts
    assert {"Secure", "HttpOnly", "SameSite=Lax", "Domain=.example.com", "Path=/"} <= set(parts)
    assert not any(p.startswith("Domain=") for p in auth.rewrite_set_cookie(f"{SESSION}; Path=/").split("; "))


def test_safe_next_blocks_other_sites(monkeypatch):
    monkeypatch.setattr(config, "FRONTEND_URL", "http://localhost:5173")
    assert auth.safe_next("http://localhost:5173/runs/x") == "http://localhost:5173/runs/x"
    for bad in (None, "", "https://evil.com/app", "http://localhost:5173.evil.com/app",
                "http://localhost:5173@evil.com/", "//evil.com"):
        assert auth.safe_next(bad) == "http://localhost:5173/app"


def test_proxy_forwards_like_neon_sdk(client, upstream):
    upstream.reply = lambda r: httpx.Response(
        200, json={"ok": True},
        headers=[("set-cookie", f"{SESSION}; Path=/; SameSite=None; Partitioned"), ("set-auth-jwt", "jwt1"),
                 ("x-upstream-internal", "leak")])
    r = client.post("/api/auth/sign-in/email?x=1", json={"email": "a@b.c", "password": "pw123456"},
                    headers={"origin": "http://localhost:5173", "cookie": f"{SESSION}; theme=dark",
                             "user-agent": "ua", "x-other": "no"})
    sent = upstream.calls[0]
    assert str(sent.url) == f"{AUTH}/sign-in/email?x=1" and sent.method == "POST"
    assert sent.headers["origin"] == "http://localhost:5173" and sent.headers["cookie"] == SESSION
    assert sent.headers["x-neon-auth-middleware"] == "true" and sent.headers["user-agent"] == "ua"
    assert "x-other" not in sent.headers
    assert json.loads(sent.content) == {"email": "a@b.c", "password": "pw123456"}
    assert r.status_code == 200 and r.json() == {"ok": True} and r.headers["set-auth-jwt"] == "jwt1"
    assert "x-upstream-internal" not in r.headers
    cookie = r.headers["set-cookie"]
    assert "SameSite=Lax" in cookie and "Partitioned" not in cookie and "Secure" in cookie


def test_proxy_origin_falls_back_to_referer_then_own_origin(client, upstream):
    client.get("/api/auth/get-session", headers={"referer": "http://localhost:5173/signin?next=/app"})
    client.get("/api/auth/get-session")
    assert [c.headers["origin"] for c in upstream.calls] == ["http://localhost:5173", "http://testserver"]
    assert "cookie" not in upstream.calls[1].headers  # no Neon cookies, nothing to send


def test_proxy_rejects_paths_outside_the_auth_api(client, upstream):
    for path in ("..%2F..%2Frest", "a//b", "a/.hidden"):
        assert client.get(f"/api/auth/{path}").status_code == 404
    assert upstream.calls == []


def test_proxy_503_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(config, "NEON_AUTH_URL", "")
    r = client.post("/api/auth/sign-in/email", json={})
    assert r.status_code == 503 and "NEON_AUTH_URL" in r.json()["message"]


def test_proxy_502_when_upstream_unreachable(client, upstream):
    def down(request):
        raise httpx.ConnectError("down")

    upstream.reply = down
    r = client.get("/api/auth/get-session")
    assert r.status_code == 502 and r.json()["code"] == "AUTH_UNAVAILABLE" and r.json()["message"]


def test_oauth_complete_exchanges_verifier_and_redirects(client, upstream):
    upstream.reply = lambda r: httpx.Response(200, json={"user": USER}, headers=[("set-cookie", f"{SESSION}; Path=/")])
    r = client.get("/api/auth/complete?next=http://localhost:5173/runs/x&neon_auth_session_verifier=v1",
                   headers={"cookie": CHALLENGE})
    sent = upstream.calls[0]
    assert sent.url.path.endswith("/get-session") and sent.url.params["neon_auth_session_verifier"] == "v1"
    assert sent.headers["cookie"] == CHALLENGE
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/runs/x"
    assert "tok123" in r.headers["set-cookie"] and "SameSite=Lax" in r.headers["set-cookie"]


def test_upstream_cookies_never_leak_between_users(client, upstream):
    """The proxy's HTTP client is shared by everyone: it must not keep cookies from Neon's responses."""
    upstream.reply = lambda r: httpx.Response(200, json={"user": USER}, headers=[("set-cookie", f"{SESSION}; Path=/")])
    client.get("/api/auth/get-session", headers={"cookie": SESSION})  # user A signs in
    client.get("/api/auth/get-session")  # user B, no cookies
    assert "cookie" not in upstream.calls[1].headers


def test_oauth_complete_without_challenge_cookie_fails(client, upstream):
    r = client.get("/api/auth/complete?neon_auth_session_verifier=v1", headers={"cookie": "theme=dark"})
    assert r.headers["location"] == "http://localhost:5173/signin?error=oauth" and upstream.calls == []


def test_oauth_complete_without_verifier_just_redirects(client, upstream):
    r = client.get("/api/auth/complete?next=https://evil.com")
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/app" and upstream.calls == []


@pytest.mark.parametrize("reply", [
    lambda r: httpx.Response(401, json={"message": "invalid verifier"}),
    lambda r: httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),  # no session made
])
def test_oauth_complete_failure_goes_to_signin(client, upstream, reply):
    upstream.reply = reply
    r = client.get("/api/auth/complete?neon_auth_session_verifier=v1", headers={"cookie": CHALLENGE})
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/signin?error=oauth"


def test_current_user_401_without_cookie(client, upstream):
    assert client.get("/me").status_code == 401 and upstream.calls == []


def test_current_user_401_when_upstream_session_is_null(client, upstream):
    upstream.reply = lambda r: httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
    assert client.get("/me", headers={"cookie": SESSION}).status_code == 401


def test_current_user_is_cached_and_sign_out_clears_it(client, upstream):
    assert client.get("/me", headers={"cookie": SESSION}).json()["id"] == "u1"
    assert client.get("/me", headers={"cookie": SESSION}).json()["id"] == "u1"
    assert len(upstream.calls) == 1  # second call served from the 60 s cache
    client.post("/api/auth/sign-out", json={}, headers={"cookie": SESSION})
    client.get("/me", headers={"cookie": SESSION})
    assert len(upstream.calls) == 3  # sign-out forwarded, then a fresh lookup
