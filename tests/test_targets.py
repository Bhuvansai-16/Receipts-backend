import asyncio
import io
import tarfile

from receipts import engine, targets


def make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_linked_issue():
    assert targets.linked_issue("Fixes #12 and more") == 12
    assert targets.linked_issue("closes: #7") == 7
    assert targets.linked_issue("This resolves #3.") == 3
    assert targets.linked_issue("see #3") is None and targets.linked_issue(None) is None


def test_claim_prefers_the_issue():
    pr = {"title": "Fix crash", "body": "Fixes #3"}
    assert targets.claim_text(pr, {"title": "Crash on empty list", "body": "Steps..."}) == "Crash on empty list\n\nSteps..."
    assert targets.claim_text(pr, None) == "Fix crash\n\nFixes #3"
    assert targets.claim_text({"title": "Only a title", "body": None}, None) == "Only a title"


def test_suite_for_picks_related_existing_tests():
    base = {"pkg/core.py", "tests/test_core.py", "tests/test_other.py", "tests/test_new.py"}
    assert targets.suite_for(["pkg/core.py", "tests/test_other.py", "tests/test_added.py"], base) == \
        ["tests/test_core.py", "tests/test_other.py"]


def test_tar_files_strips_the_top_folder():
    tarball = make_tarball({"octo-hello-abc/pkg/a.py": b"", "octo-hello-abc/README.md": b""})
    assert targets.tar_files(tarball) == {"pkg/a.py", "README.md"}


def test_env_setup_failure_is_unproven(monkeypatch):
    target = targets.RepoTarget("octo/hello#1", "octo/hello", "Crash on empty list", b"", "sha", [])

    async def broken():
        raise targets.EnvironmentSetupError("setting up octo/hello failed: pip install failed")

    async def fix_claim(issue, patch):
        return engine.Claim(kind="fix", claim="empty lists no longer crash")

    monkeypatch.setattr(target, "base_image", broken)
    monkeypatch.setattr(engine, "classify", fix_claim)
    ev = asyncio.run(engine.check(target, "diff --git a/x b/x\n"))
    assert ev["verdict"] == "UNPROVEN" and "pip install failed" in ev["reason"]
    assert ev["instance_id"] == "octo/hello#1" and ev["repo"] == "octo/hello"
