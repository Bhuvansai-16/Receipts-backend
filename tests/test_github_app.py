import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from receipts import auth, config, github_app


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return SimpleNamespace(private_pem=private, public_pem=public)


@pytest.fixture
def fake_github(monkeypatch, rsa_key):
    fake = SimpleNamespace(calls=[], checks=lambda r: httpx.Response(201, json={"id": 555}))

    def handler(request: httpx.Request) -> httpx.Response:
        fake.calls.append(request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": f"ghs_{len(fake.calls)}", "expires_at": "2099-01-01T00:00:00Z"})
        return fake.checks(request)

    monkeypatch.setattr(config, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(config, "github_private_key", lambda: rsa_key.private_pem)
    monkeypatch.setattr(auth, "_client", auth._new_client(httpx.MockTransport(handler)))
    github_app._tokens.clear()
    return fake


def run(coro):
    return asyncio.run(coro)


def test_verify_signature():
    body = b'{"zen":"hi"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert github_app.verify_signature("s3cret", body, good)
    assert not github_app.verify_signature("s3cret", body, "sha256=" + "0" * 64)
    assert not github_app.verify_signature("s3cret", body + b" ", good)
    assert not github_app.verify_signature("s3cret", body, None)
    assert not github_app.verify_signature("", body, good)


def test_app_jwt_claims(monkeypatch, rsa_key):
    monkeypatch.setattr(config, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(config, "github_private_key", lambda: rsa_key.private_pem)
    claims = jwt.decode(github_app.app_jwt(now=1_000_000), rsa_key.public_pem, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}


def test_installation_token_is_scoped_and_cached(fake_github):
    t1 = run(github_app.installation_token(7, repo_id=42, permissions={"contents": "read"}))
    t2 = run(github_app.installation_token(7, repo_id=42, permissions={"contents": "read"}))
    t3 = run(github_app.installation_token(7, repo_id=42, permissions={"checks": "write"}))
    token_calls = [c for c in fake_github.calls if c.url.path == "/app/installations/7/access_tokens"]
    assert t1 == t2 != t3 and len(token_calls) == 2
    assert json.loads(token_calls[0].content) == {"repository_ids": [42], "permissions": {"contents": "read"}}
    assert token_calls[0].headers["authorization"].startswith("Bearer ey")


def test_check_run_conclusions():
    assert github_app.CONCLUSION == {"PROVEN": "success", "REFUTED": "failure", "REGRESSION": "failure",
                                     "UNPROVEN": "neutral", "NO_CHECKABLE_CLAIM": "neutral"}


def test_check_runs_are_created_and_completed(fake_github):
    check_id = run(github_app.start_check(7, 42, "octo/hello", "abc", "http://localhost:5173/runs/r1"))
    run(github_app.finish_check(7, 42, "octo/hello", check_id, "REFUTED", "still fails", "http://localhost:5173/runs/r1"))
    create, complete = [c for c in fake_github.calls if "/check-runs" in c.url.path]
    assert check_id == 555 and create.method == "POST" and json.loads(create.content)["head_sha"] == "abc"
    body = json.loads(complete.content)
    assert complete.method == "PATCH" and complete.url.path.endswith("/check-runs/555")
    assert body["conclusion"] == "failure" and body["status"] == "completed"


def test_check_run_errors_never_raise(fake_github):
    fake_github.checks = lambda r: httpx.Response(403, json={"message": "Resource not accessible"})
    assert run(github_app.start_check(7, 42, "octo/hello", "abc", "http://x/runs/r1")) is None
    run(github_app.finish_check(7, 42, "octo/hello", 555, "PROVEN", "ok", "http://x/runs/r1"))  # no exception
