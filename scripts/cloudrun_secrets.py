"""Copy the API's secrets from .env (and the GitHub App key) into Google Secret Manager for Cloud Run.

    gcloud auth login && gcloud config set project <PROJECT_ID>
    python scripts/cloudrun_secrets.py

Creates each secret, or adds a new version when it exists; values go to gcloud on stdin and are never printed
or put on a command line. Lets Cloud Run's runtime service account read them, then prints the --set-secrets
value for `gcloud run deploy`.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ["NEBIUS_API_KEY", "CONTREE_PROJECT", "CONTREE_TOKEN", "TAVILY_API_KEY", "LANGSMITH_API_KEY",
           "DATABASE_URL", "DATABASE_URL_UNPOOLED", "NEON_AUTH_URL", "GITHUB_APP_ID", "GITHUB_APP_SLUG",
           "GITHUB_WEBHOOK_SECRET", "GITHUB_APP_PRIVATE_KEY"]


def env_values(text: str) -> dict[str, str]:
    """KEY=VALUE lines of a .env file: comments, blank lines and empty values skipped, quotes removed."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            values[key.strip()] = value
    return values


def secrets_flag(names: list[str]) -> str:
    return ",".join(f"{n}={n}:latest" for n in names)


def gcloud(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Bytes in, not text: text mode on Windows would turn the private key's \\n into \\r\\n."""
    exe = shutil.which("gcloud") or sys.exit("gcloud is not installed or not on PATH")
    r = subprocess.run([exe, *args], input=stdin.encode() if stdin is not None else None, capture_output=True)
    return subprocess.CompletedProcess(r.args, r.returncode, r.stdout.decode(errors="replace"),
                                       r.stderr.decode(errors="replace"))


def main() -> None:
    values = env_values((ROOT / ".env").read_text(encoding="utf-8"))
    key_path = values.pop("GITHUB_APP_PRIVATE_KEY_PATH", "")
    if key_path and "GITHUB_APP_PRIVATE_KEY" not in values:
        path = Path(key_path) if Path(key_path).is_absolute() else ROOT / key_path
        values["GITHUB_APP_PRIVATE_KEY"] = path.read_text(encoding="utf-8")
    project = gcloud("config", "get-value", "project").stdout.strip() or sys.exit("run: gcloud config set project ID")
    stored = []
    for name in SECRETS:
        if name not in values:
            continue
        exists = gcloud("secrets", "describe", name).returncode == 0
        r = (gcloud("secrets", "versions", "add", name, "--data-file=-", stdin=values[name]) if exists else
             gcloud("secrets", "create", name, "--replication-policy=automatic", "--data-file=-", stdin=values[name]))
        print(f"{name}: {'updated' if exists else 'created'}" if r.returncode == 0 else f"{name}: FAILED {r.stderr.strip()}")
        if r.returncode == 0:
            stored.append(name)
    number = gcloud("projects", "describe", project, "--format=value(projectNumber)").stdout.strip()
    account = f"{number}-compute@developer.gserviceaccount.com"  # Cloud Run's default runtime identity
    r = gcloud("projects", "add-iam-policy-binding", project, f"--member=serviceAccount:{account}",
               "--role=roles/secretmanager.secretAccessor", "--condition=None")
    print(f"secret access for {account}: {'ok' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}")
    print(f"\n--set-secrets \"{secrets_flag(stored)}\"")


if __name__ == "__main__":
    main()
