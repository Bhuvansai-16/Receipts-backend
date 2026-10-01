"""Settings from .env and client factories. Runs on global Python; no venv."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# LangSmith tracing is env-driven; turn it on whenever a key is present.
os.environ.setdefault("LANGSMITH_PROJECT", "receipts")
if os.environ.get("LANGSMITH_API_KEY"):
    os.environ.setdefault("LANGSMITH_TRACING", "true")

NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
MODELS = {
    "classifier": os.environ.get("MODEL_CLASSIFIER", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
    # A/B on five sympy PRs (spec, 2026-09-30): Super wrote a valid test for 4 of 5 with half the tokens;
    # Lightning for 0 of 5. MODEL_TEST_WRITER=nvidia/Nemotron-3_5-Lightning is the cheaper per-token option.
    "writer": os.environ.get("MODEL_TEST_WRITER", "nvidia/nemotron-3-super-120b-a12b"),
    # the one automatic retry when no test was accepted; Ultra, probed on #16, wrote no valid test either
    "writer_strong": os.environ.get("MODEL_TEST_WRITER_STRONG", "nvidia/nemotron-3-super-120b-a12b"),
    "scope": os.environ.get("MODEL_SCOPE", "nvidia/nemotron-3-super-120b-a12b"),
    "judge": os.environ.get("MODEL_JUDGE", "nvidia/Nemotron-3-Ultra-550b-a55b"),
}
SANDBOX_TIMEOUT_S = int(os.environ.get("SANDBOX_TIMEOUT_S", "900"))
SANDBOX_MAX_CONCURRENCY = int(os.environ.get("SANDBOX_MAX_CONCURRENCY", "20"))
MAX_TEST_ATTEMPTS = int(os.environ.get("MAX_TEST_ATTEMPTS", "5"))
VERDICT_RUNS = int(os.environ.get("VERDICT_RUNS", "3"))
RUNS_DIR = ROOT / "runs"

# Neon: pooled URL for the app, direct (unpooled) URL for migrations (neon.com/docs/connect/connection-pooling)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATABASE_URL_UNPOOLED = os.environ.get("DATABASE_URL_UNPOOLED", "") or DATABASE_URL
NEON_AUTH_URL = os.environ.get("NEON_AUTH_URL", "").rstrip("/")  # Neon Console > Auth > Configuration
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173").rstrip("/")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "") or None  # e.g. ".example.com" for app. + api. subdomains
MAX_ACTIVE_RUNS = int(os.environ.get("MAX_ACTIVE_RUNS", "2"))
RUNS_PER_DAY = int(os.environ.get("RUNS_PER_DAY", "20"))
GLOBAL_RUNS_PER_DAY = int(os.environ.get("GLOBAL_RUNS_PER_DAY", "60"))  # every user together: caps spend
DEMO_RUNS_PER_DAY = int(os.environ.get("DEMO_RUNS_PER_DAY", "20"))  # no-sign-in demo checks, one at a time
# When set, webhook auto-checks run only on these GitHub accounts (the app is public; manual checks still work)
ALLOWED_GITHUB_ACCOUNTS = {a.strip().lower() for a in os.environ.get("ALLOWED_GITHUB_ACCOUNTS", "").split(",")
                           if a.strip()}

# GitHub App (github.com > Settings > Developer settings > GitHub Apps); see README "GitHub App"
API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")  # setup redirect + check links
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "")  # github.com/apps/<slug>
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def github_private_key() -> str:
    """PEM from GITHUB_APP_PRIVATE_KEY (newlines escaped as \\n) or the file at GITHUB_APP_PRIVATE_KEY_PATH."""
    if key := os.environ.get("GITHUB_APP_PRIVATE_KEY"):
        return key.replace("\\n", "\n")
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if not path:
        return ""
    # relative to the backend folder, not to wherever the server was started from
    return (Path(path) if Path(path).is_absolute() else ROOT / path).read_text(encoding="utf-8")


def llm(role: str):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=MODELS[role],
        api_key=os.environ["NEBIUS_API_KEY"],
        base_url=NEBIUS_BASE_URL,
        temperature=0,
        max_retries=3,
        timeout=300,
    )


def contree():
    from contree_sdk import Contree
    from contree_sdk.auth import IAMAuth
    from contree_sdk.config import ContreeConfig

    # IAMAuth takes env var *names* and resolves them itself.
    kw = {"token": "CONTREE_TOKEN" if os.environ.get("CONTREE_TOKEN") else "NEBIUS_API_KEY", "project_id": "CONTREE_PROJECT"}
    if os.environ.get("CONTREE_BASE_URL"):
        kw["base_url"] = "CONTREE_BASE_URL"
    return Contree(ContreeConfig(auth=IAMAuth(**kw), operation_run_timeout=SANDBOX_TIMEOUT_S))
