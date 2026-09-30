import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from receipts import auth, checks, config, engine, github, github_app, server, swebench, targets

AUTH = "https://ep-test.neonauth.aws.neon.tech/neondb/auth"
USER = {"id": "u1", "email": "a@b.c"}
SECRET = "whsec_test"
REPO = {"id": 42, "full_name": "octo/hello", "private": False, "language": "Python", "description": "Hi",
        "pushed_at": "2026-09-28T10:00:00Z", "html_url": "https://github.com/octo/hello"}
PR = {"number": 12, "title": "Fix crash", "body": "Fixes #3", "user": {"login": "dev"}, "state": "open",
      "html_url": "https://github.com/octo/hello/pull/12", "draft": False, "updated_at": "2026-09-29T00:00:00Z",
      "head": {"sha": "head1"}, "base": {"sha": "base1"}}


@pytest.fixture
def gh(monkeypatch):
    """Neon Auth and GitHub behind one mock transport, plus a fake engine and PR target."""
    fake = SimpleNamespace(calls=[], github_linked=True,
                           installations=[{"id": 7, "app_id": 123, "account": {"login": "octo", "type": "User"}}])

    def handler(request: httpx.Request) -> httpx.Response:
        fake.calls.append(request)
        path = request.url.path
        if request.url.host != "api.github.com":  # Neon Auth
            if path.endswith("/get-access-token"):
                return (httpx.Response(200, json={"accessToken": "ghu_user"}) if fake.github_linked
                        else httpx.Response(400, json={"message": "Account not found"}))
            return httpx.Response(200, json={"user": USER})
        routes = {
            "/user/installations": (200, {"installations": fake.installations}),
            "/app/installations/7/access_tokens": (201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}),
            "/installation/repositories": (200, {"repositories": [REPO]}),
            "/repos/octo/hello/pulls": (200, [PR]),
            "/repos/octo/hello/pulls/12": (200, PR),
            "/repos/octo/hello/check-runs": (201, {"id": 555}),
            "/repos/octo/hello/check-runs/555": (200, {"id": 555}),
        }
        status, body = routes.get(path, (404, {"message": "Not Found"}))
        return httpx.Response(status, json=body)

    async def fake_pr_target(installation_id, repo_id, full_name, number):
        return SimpleNamespace(instance_id=f"{full_name}#{number}", repo=full_name), "diff --git a/x b/x\n", "head1"

    async def fake_check(target, patch, emit=None):
        emit("claim", {"kind": "fix", "claim": "c"})
        return {"instance_id": target.instance_id, "repo": target.repo, "verdict": "PROVEN", "reason": "r",
                "seconds": 1.0, "tokens": {}}

    monkeypatch.setattr(config, "NEON_AUTH_URL", AUTH)
    monkeypatch.setattr(config, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(config, "GITHUB_APP_SLUG", "receipts-dev")
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "github_private_key", lambda: "pem")
    monkeypatch.setattr(github_app, "app_jwt", lambda now=None: "app.jwt")
    monkeypatch.setattr(auth, "_client", auth._new_client(httpx.MockTransport(handler)))
    monkeypatch.setattr(targets, "pr_target", fake_pr_target)
    monkeypatch.setattr(engine, "check", fake_check)
    github_app._tokens.clear()
    github._deliveries.clear()
    github._repo_lists.clear()
    auth.sessions.items.clear()
    return fake


@pytest.fixture
def client(gh, monkeypatch, tmp_path):
    monkeypatch.setattr(swebench, "_dataset", lambda: {})
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    server._instances.cache_clear()
    checks.LIVE.clear()
    with TestClient(server.app, follow_redirects=False) as c:
        c.store = server.app.state.runs
        yield c
    server.app.dependency_overrides.clear()


def signed_in(client):
    server.app.dependency_overrides[auth.current_user] = lambda: USER
    return client


def wait_for(client, run_id):
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        for _ in r.iter_lines():
            pass
    return client.get(f"/api/runs/{run_id}").json()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def webhook(client, payload, event="pull_request", delivery="d1", signature=None):
    body = json.dumps(payload).encode()
    return client.post("/api/github/webhook", content=body, headers={
        "x-github-event": event, "x-github-delivery": delivery, "content-type": "application/json",
        "x-hub-signature-256": sign(body) if signature is None else signature})


PR_EVENT = {"action": "synchronize", "installation": {"id": 7}, "repository": {"id": 42, "full_name": "octo/hello"},
            "pull_request": PR}


def test_status_reports_link_and_install_url(client):
    body = signed_in(client).get("/api/github/status").json()
    assert body == {"app_configured": True, "github_linked": True, "installations": [],
                    "install_url": "https://github.com/apps/receipts-dev/installations/new"}


def test_sync_stores_only_what_github_confirms(client):
    # a real browser visit: the session cookie, not a test override (setup redirects signed-out visitors)
    r = client.get("/api/github/setup?installation_id=99&setup_action=install",
                   headers={"cookie": "__Secure-neon-auth.session_token=t"})
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/app/repos?installed=1"
    assert [i["id"] for i in signed_in(client).get("/api/github/status").json()["installations"]] == [7]


def test_sync_needs_a_linked_github_account(client, gh):
    gh.github_linked = False
    assert signed_in(client).post("/api/github/installations/sync").status_code == 409


def test_repos_and_pulls(client):
    signed_in(client).post("/api/github/installations/sync")
    [repo] = client.get("/api/github/repos").json()["repos"]
    assert repo == {"id": 42, "full_name": "octo/hello", "private": False, "language": "Python", "description": "Hi",
                    "pushed_at": "2026-09-28T10:00:00Z", "url": "https://github.com/octo/hello",
                    "installation_id": 7, "auto_check": False}
    [pr] = client.get("/api/github/repos/octo/hello/pulls").json()["pulls"]
    assert (pr["number"], pr["linked_issue"], pr["author"], pr["latest"]) == (12, 3, "dev", None)


def test_pr_endpoints_require_the_users_installation(client):
    signed_in(client)
    assert client.get("/api/github/repos/octo/hello/pulls").status_code == 404  # nothing installed yet
    client.post("/api/github/installations/sync")
    assert client.get("/api/github/repos/someone/else/pulls").status_code == 404
    assert client.post("/api/github/repos/someone/else/pulls/1/check").status_code == 404
    assert client.put("/api/github/repos/999/auto-check", json={"enabled": True}).status_code == 404


def test_check_button_starts_a_run_and_a_check_run(client, gh):
    signed_in(client).post("/api/github/installations/sync")
    run_id = client.post("/api/github/repos/octo/hello/pulls/12/check").json()["run_id"]
    got = wait_for(client, run_id)
    row = client.store.rows[run_id]
    assert got["status"] == "done" and got["evidence"]["verdict"] == "PROVEN"
    assert got["evidence"]["source"] == {"repo": "octo/hello", "pr_number": 12, "head_sha": "head1",
                                         "url": "https://github.com/octo/hello/pull/12", "title": "Fix crash",
                                         "linked_issue": 3}
    assert (row["repo"], row["pr_number"], row["head_sha"], row["check_run_id"]) == ("octo/hello", 12, "head1", 555)
    create, complete = [c for c in gh.calls if "/check-runs" in c.url.path]
    assert json.loads(create.content)["details_url"] == f"http://localhost:5173/runs/{run_id}"
    assert json.loads(complete.content)["conclusion"] == "success"
    assert client.get("/api/github/repos/octo/hello/pulls").json()["pulls"][0]["latest"]["id"] == run_id


def test_webhook_rejects_bad_signatures(client):
    assert webhook(client, PR_EVENT, signature="").status_code == 401
    assert webhook(client, PR_EVENT, signature="sha256=" + "0" * 64).status_code == 401
    assert client.store.rows == {}


def test_webhook_ignores_repos_without_auto_check(client):
    signed_in(client).post("/api/github/installations/sync")
    assert webhook(client, PR_EVENT).status_code == 202 and client.store.rows == {}


def test_webhook_runs_once_per_head_sha(client):
    signed_in(client).post("/api/github/installations/sync")
    assert client.put("/api/github/repos/42/auto-check", json={"enabled": True}).json() == {"auto_check": True}
    assert webhook(client, PR_EVENT, delivery="d1").status_code == 202
    [run_id] = client.store.rows
    wait_for(client, run_id)
    webhook(client, PR_EVENT, delivery="d1")  # redelivery
    webhook(client, PR_EVENT, delivery="d2")  # same head SHA again
    assert list(client.store.rows) == [run_id] and client.store.rows[run_id]["user_id"] == "u1"


def test_uninstall_removes_the_installation(client):
    signed_in(client).post("/api/github/installations/sync")
    webhook(client, {"action": "deleted", "installation": {"id": 7}}, event="installation")
    assert client.get("/api/github/status").json()["installations"] == []


def test_me_reports_usage(client):
    assert signed_in(client).get("/api/me").json()["usage"] == {"active": 0, "today": 0, "max_active": 2,
                                                                  "per_day": 20}


def test_a_running_pr_check_already_names_its_pull_request(client):
    import asyncio
    source = {"repo": "octo/hello", "pr_number": 12, "head_sha": "head1"}
    asyncio.run(client.store.create("r-live", "u1", "octo/hello#12", "github", source=source))
    ev = client.get("/api/runs/r-live").json()["evidence"]
    assert ev["repo"] == "octo/hello"
    assert ev["source"] == {**source, "url": "https://github.com/octo/hello/pull/12"}


def test_pr_receipts_carry_the_pr_title_and_linked_issue(client):
    signed_in(client).post("/api/github/installations/sync")
    run_id = client.post("/api/github/repos/octo/hello/pulls/12/check").json()["run_id"]
    source = wait_for(client, run_id)["evidence"]["source"]
    assert (source["title"], source["linked_issue"]) == ("Fix crash", 3)


def test_repo_lists_are_read_from_github_once_a_minute(client, gh):
    def lists():
        return len([c for c in gh.calls if c.url.path == "/installation/repositories"])

    signed_in(client).post("/api/github/installations/sync")
    client.get("/api/github/repos")
    client.get("/api/github/repos/octo/hello/pulls")
    client.put("/api/github/repos/42/auto-check", json={"enabled": True})
    assert lists() == 1
    client.post("/api/github/installations/sync")  # the Refresh button re-reads GitHub
    client.get("/api/github/repos")
    assert lists() == 2
    webhook(client, {"action": "added", "installation": {"id": 7}}, event="installation_repositories")
    client.get("/api/github/repos")
    assert lists() == 3
