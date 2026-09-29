"""Real GitHub repositories as check targets (the counterpart of swebench.Instance)."""
import io
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import config, github_app
from .engine import changed_files
from .sandbox import text

CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.I)
MAX_TARBALL_BYTES = 50_000_000
READ = {"contents": "read", "pull_requests": "read", "issues": "read", "metadata": "read"}
PYTHON_IMAGE = "docker://docker.io/library/python:3.11"  # full image: has git for `git apply`
# The engine's sandbox commands activate SWE-bench's conda env; in this image python already *is* the env,
# so a no-op `conda` keeps those commands unchanged.
SETUP = """set -e
mkdir -p /testbed /opt/miniconda3/etc/profile.d
printf 'conda() { :; }\\n' > /opt/miniconda3/etc/profile.d/conda.sh
tar -xzf /tmp/src.tar.gz -C /testbed --strip-components=1
cd /testbed
pip install -q --disable-pip-version-check -e '.[test]' || pip install -q -e '.[tests]' \\
  || pip install -q -e '.[dev]' || pip install -q -e . || true
for f in requirements*.txt requirements/*.txt; do if [ -f "$f" ]; then pip install -q -r "$f" || true; fi; done
pip install -q pytest"""
_envs: dict[tuple[str, str], object] = {}


class EnvironmentSetupError(RuntimeError):
    pass


def linked_issue(body: str | None) -> int | None:
    m = CLOSING.search(body or "")
    return int(m.group(1)) if m else None


def claim_text(pr: dict, issue: dict | None) -> str:
    src = issue or pr
    return f"{src.get('title') or ''}\n\n{src.get('body') or ''}".strip()


def tar_files(tarball: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        return {m.name.split("/", 1)[1] for m in tf.getmembers() if m.isfile() and "/" in m.name}


def suite_for(changed: list[str], base_files: set[str]) -> list[str]:
    """Existing tests to guard: test files the PR touches, plus test_<module>.py for each changed module."""
    tests = {p for p in base_files if PurePosixPath(p).name.startswith("test_") and p.endswith(".py")}
    picked = {p for p in changed if p in tests}
    stems = {PurePosixPath(p).stem for p in changed if p.endswith(".py") and p not in tests}
    picked |= {p for p in tests if PurePosixPath(p).stem.removeprefix("test_") in stems}
    return sorted(picked)


@dataclass
class RepoTarget:
    instance_id: str  # owner/repo#number
    repo: str
    problem_statement: str
    tarball: bytes = field(repr=False)
    base_sha: str
    suite: list[str]
    pass_to_pass: list[str] = field(default_factory=list)

    async def base_image(self):
        if config.SANDBOX_PROVIDER != "contree":
            raise EnvironmentSetupError("real repositories need SANDBOX_PROVIDER=contree (Nebius sandboxes)")
        key = (self.repo, self.base_sha)
        if key not in _envs:  # ponytail: per-process cache; a restart rebuilds it
            image = await config.contree().images.oci(PYTHON_IMAGE)
            env = await image.run(shell=SETUP, files={"/tmp/src.tar.gz": self.tarball}, disposable=False,
                                  timeout=config.SANDBOX_TIMEOUT_S)
            if env.exit_code != 0:
                raise EnvironmentSetupError(f"setting up {self.repo} failed: {text(env.stderr)[-300:].strip()}")
            _envs[key] = env
        return _envs[key]


async def pr_target(installation_id: int, repo_id: int, full_name: str, number: int):
    """(target, diff, head_sha) for a PR. The token only reads this one repository and never enters a sandbox."""
    token = await github_app.installation_token(installation_id, repo_id, READ)
    pr = (await github_app.api(token, "GET", f"/repos/{full_name}/pulls/{number}")).json()
    head = pr["head"]["sha"]
    compare = (await github_app.api(token, "GET", f"/repos/{full_name}/compare/{pr['base']['sha']}...{head}")).json()
    base_sha = compare["merge_base_commit"]["sha"]  # the PR's diff applies here, even if the base branch moved on
    diff = (await github_app.api(token, "GET", f"/repos/{full_name}/pulls/{number}",
                                 accept="application/vnd.github.diff")).text
    issue = None
    if n := linked_issue(pr.get("body")):
        r = await github_app.api(token, "GET", f"/repos/{full_name}/issues/{n}", ok=(200, 404, 410))
        issue = r.json() if r.status_code == 200 and "pull_request" not in r.json() else None
    tar = await github_app.api(token, "GET", f"/repos/{full_name}/tarball/{base_sha}", follow_redirects=True)
    if len(tar.content) > MAX_TARBALL_BYTES:
        raise EnvironmentSetupError(f"{full_name} is larger than {MAX_TARBALL_BYTES // 1_000_000} MB")
    suite = suite_for(changed_files(diff), tar_files(tar.content))
    target = RepoTarget(f"{full_name}#{number}", full_name, claim_text(pr, issue), tar.content, base_sha, suite)
    return target, diff, head
