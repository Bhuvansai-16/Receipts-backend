import asyncio
import hashlib
import hmac
import json
import re
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
    run(github_app.finish_check(7, 42, "octo/hello", check_id, {"verdict": "REFUTED", "reason": "still fails"},
                                "http://localhost:5173/runs/r1"))
    create, complete = [c for c in fake_github.calls if "/check-runs" in c.url.path]
    assert check_id == 555 and create.method == "POST" and json.loads(create.content)["head_sha"] == "abc"
    body = json.loads(complete.content)
    assert complete.method == "PATCH" and complete.url.path.endswith("/check-runs/555")
    assert body["conclusion"] == "failure" and body["status"] == "completed"
    assert body["output"]["title"].startswith("Refuted") and "still fails" in body["output"]["summary"]


def test_check_run_errors_never_raise(fake_github):
    fake_github.checks = lambda r: httpx.Response(403, json={"message": "Resource not accessible"})
    assert run(github_app.start_check(7, 42, "octo/hello", "abc", "http://x/runs/r1")) is None
    run(github_app.finish_check(7, 42, "octo/hello", 555, {"verdict": "PROVEN", "reason": "ok"}, "http://x/runs/r1"))


def test_check_run_is_created_without_a_link_github_rejects(fake_github):
    def checks(request):
        body = json.loads(request.content)
        return httpx.Response(422, json={"message": "Invalid details_url"}) if "details_url" in body \
            else httpx.Response(201, json={"id": 777})

    fake_github.checks = checks
    assert run(github_app.start_check(7, 42, "octo/hello", "abc", "http://localhost:5173/runs/r1")) == 777


def test_private_key_path_is_relative_to_the_backend_folder(monkeypatch, tmp_path):
    (tmp_path / "github-app.pem").write_text("PEM", encoding="utf-8")
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "github-app.pem")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert config.github_private_key() == "PEM"


def _ev(verdict, base_fail=3, pr_fail=0, msg="Expected z**4, got -z**4", code="def test_t():\n    assert 1\n" * 100):
    fail = {"tests": 1, "not_passed": {"t.py::t": {"outcome": "failed", "exc": "AssertionError", "msg": msg}},
            "output_tail": ""}
    ok = {"tests": 1, "not_passed": {}, "output_tail": ""}
    return {"verdict": verdict, "reason": "r", "claim": {"kind": "fix", "claim": "refine misses Abs(z)**4"},
            "writer": {"test_code": code},
            "forks": {"base_with_test": [fail] * base_fail + [ok] * (3 - base_fail),
                      "pr_with_test": [fail] * pr_fail + [ok] * (3 - pr_fail)}}


def test_check_output_explains_a_proven_run():
    out = github_app.check_output(_ev("PROVEN"), "https://r/1")
    assert out["title"].startswith("Proven")
    assert "fails on the original code in 3 of 3 runs" in out["summary"]
    assert "passes with this pull request in 3 of 3 runs" in out["summary"] and "https://r/1" in out["summary"]
    assert out["text"].startswith("### Blind test") and out["text"].count("\n") < 70  # first 60 lines only


def _shown(markdown: str) -> str:
    """The text GitHub shows for escaped Markdown."""
    return re.sub(r"\\(.)", r"\1", markdown)


def test_check_output_names_the_case_still_failing():
    out = github_app.check_output(_ev("UNPROVEN", pr_fail=3), "u")
    assert "Still failing with the change: Expected z**4, got -z**4" in _shown(out["summary"])


def test_runs_that_never_executed_are_not_counted_as_passes():
    # A sandbox outage leaves runs with no tests: "passes in 3 of 3" would claim what never ran.
    ev = _ev("UNPROVEN", pr_fail=1)
    ev["forks"]["pr_with_test"][1:] = [{"tests": 0, "not_passed": {}, "output_tail": ""}] * 2
    summary = github_app.check_output(ev, "u")["summary"]
    assert "passes with this pull request in 0 of 3 runs (2 did not run)" in summary


def test_untrusted_text_cannot_add_markdown_to_the_check_run():
    # The claim, failure messages and reasons carry issue and PR text onto a check run in Receipts' name.
    ev = _ev("UNPROVEN", pr_fail=3, msg="see [docs](https://evil.example) ![x](https://evil.example/p.png)")
    ev["claim"]["claim"] = "x\n# Proven\n**safe to merge**"
    ev["reason"] = "<img src=x> `code`"
    summary = github_app.check_output(ev, "u")["summary"]
    for raw in ("[docs](", "![x](", "\n# Proven", "**safe", "`code`"):
        assert raw not in summary
    assert not re.search(r"(?<!\\)<", summary)  # no HTML: every < is escaped
    assert "see [docs](https://evil.example)" in _shown(summary) and "x # Proven **safe to merge**" in _shown(summary)


def test_check_output_says_what_happened_to_the_existing_tests():
    ev = _ev("PROVEN")
    assert "Existing tests: none were found to run for this change" in github_app.check_output(ev, "u")["summary"]
    ev["forks"]["base_suite"] = [{"tests": 31, "not_passed": {"t.py::s": {"outcome": "skipped"}}, "output_tail": ""}]
    assert "Existing tests: all 30 that pass on the original code still pass" in \
        github_app.check_output(ev, "u")["summary"]
    ev["verdict"] = "REGRESSION"
    assert "Existing tests: the change breaks some that passed before (named in the reason)" in \
        github_app.check_output(ev, "u")["summary"]


def test_check_output_stays_within_githubs_limits():
    ev = _ev("UNPROVEN", pr_fail=3, msg="x" * 100_000)
    ev["reason"] = "y" * 100_000
    out = github_app.check_output(ev, "u")
    assert len(out["summary"]) <= 65_535 and len(out["text"]) <= 65_535


def test_a_test_with_backticks_cannot_break_out_of_its_code_block():
    out = github_app.check_output(_ev("PROVEN", code='DOC = """```\nnot code```"""\ndef test_t(): pass\n'), "u")
    fence = out["text"].splitlines()[1]
    assert fence.startswith("````") and out["text"].rstrip().endswith(fence[:4])


def test_check_output_without_forks_still_says_what_happened():
    out = github_app.check_output({"verdict": "NO_CHECKABLE_CLAIM", "reason": "classified as 'none'"}, "u")
    assert out["title"].startswith("No checkable claim") and out["text"] == "" and "classified" in out["summary"]


def test_check_output_carries_the_second_opinion_on_a_mixed_result():
    ev = _ev("UNPROVEN", pr_fail=3)
    ev["second_opinion"] = {"faithful": False, "reason": "expects an evaluated expression", "about": "mixed"}
    assert "Second opinion: the test may be wrong here: expects an evaluated expression" in \
        github_app.check_output(ev, "u")["summary"]
    ev["second_opinion"] = {"faithful": True, "reason": "the issue asks for it", "about": "mixed"}
    assert "Second opinion: the change may miss part of the issue: the issue asks for it" in \
        github_app.check_output(ev, "u")["summary"]  # hedged: the judge was wrong about srepr on sympy #15
