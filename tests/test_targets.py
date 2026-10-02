import asyncio
import io
import tarfile
from types import SimpleNamespace

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
    target = targets.RepoTarget("octo/hello#1", "octo/hello", "Crash on empty list", "sha", [], None)

    async def broken():
        raise targets.EnvironmentSetupError("setting up octo/hello failed: pip install failed")

    async def fix_claim(issue, patch):
        return engine.Claim(kind="fix", claim="empty lists no longer crash")

    monkeypatch.setattr(target, "base_image", broken)
    monkeypatch.setattr(engine, "classify", fix_claim)
    ev = asyncio.run(engine.check(target, "diff --git a/x b/x\n"))
    assert ev["verdict"] == "UNPROVEN" and "pip install failed" in ev["reason"]
    assert ev["instance_id"] == "octo/hello#1" and ev["repo"] == "octo/hello"


def test_base_image_downloads_the_repo_once_and_picks_the_suite(monkeypatch):
    """The download happens inside base_image, which runs alongside classification; a cached env skips it."""
    downloads = []
    tarball = make_tarball({"octo-hello-abc/pkg/calc.py": b"", "octo-hello-abc/tests/test_calc.py": b""})

    async def fetch():
        downloads.append(1)
        return tarball

    class Image:
        exit_code = 0

        async def run(self, **kw):
            assert kw["files"]["/tmp/src.tar.gz"] == tarball
            return self

        async def tag_as(self, tag):
            return self

    class Images:
        async def oci(self, ref):
            return Image()

        async def use(self, ref, strict=False):
            raise LookupError("no such tag")

    monkeypatch.setattr(targets.config, "contree", lambda: SimpleNamespace(images=Images()))
    monkeypatch.setattr(targets, "_envs", {})
    first = targets.RepoTarget("octo/hello#1", "octo/hello", "claim", "sha", ["pkg/calc.py"], fetch)
    again = targets.RepoTarget("octo/hello#2", "octo/hello", "claim", "sha", ["pkg/calc.py"], fetch)
    asyncio.run(first.base_image())
    asyncio.run(again.base_image())
    assert downloads == [1] and first.suite == again.suite == ["tests/test_calc.py"]


def _contree(images):
    return lambda: SimpleNamespace(images=images)


def test_a_kept_environment_skips_the_download_after_a_restart(monkeypatch):
    class Kept:
        exit_code, stdout = 0, b"tests/test_calc.py\npkg/test_util.py\n"

        async def run(self, **kw):
            return self

    class Images:
        async def use(self, ref, strict=False):
            assert ref == targets.env_tag("Octo/Hello", "SHA") and strict
            return Kept()

    async def fetch():
        raise AssertionError("a kept environment needs no download")

    monkeypatch.setattr(targets.config, "contree", _contree(Images()))
    monkeypatch.setattr(targets, "_envs", {})
    t = targets.RepoTarget("Octo/Hello#1", "Octo/Hello", "claim", "SHA", ["pkg/calc.py"], fetch)
    asyncio.run(t.base_image())
    assert t.suite == ["tests/test_calc.py"]


def test_a_new_environment_is_built_and_kept(monkeypatch):
    tags = []

    class Built:
        exit_code = 0

        async def run(self, **kw):
            return self

        async def tag_as(self, tag):
            tags.append(tag)
            return self

    class Images:
        async def use(self, ref, strict=False):
            raise LookupError("no such tag")

        async def oci(self, ref):
            return Built()

    async def fetch():
        return make_tarball({"octo-hello-abc/pkg/calc.py": b"", "octo-hello-abc/tests/test_calc.py": b""})

    monkeypatch.setattr(targets.config, "contree", _contree(Images()))
    monkeypatch.setattr(targets, "_envs", {})
    t = targets.RepoTarget("octo/hello#1", "octo/hello", "claim", "sha", ["pkg/calc.py"], fetch)
    asyncio.run(t.base_image())
    assert tags == [targets.env_tag("octo/hello", "sha")] and t.suite == ["tests/test_calc.py"]


def test_env_tags_are_lowercase_and_safe():
    assert targets.env_tag("Octo/Hello.World", "ABC123") == "receipts-env/octo--hello.world:abc123"
