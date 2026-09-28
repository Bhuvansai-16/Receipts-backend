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
    "writer": os.environ.get("MODEL_TEST_WRITER", "nvidia/Nemotron-3_5-Lightning"),
    "scope": os.environ.get("MODEL_SCOPE", "nvidia/nemotron-3-super-120b-a12b"),
    "judge": os.environ.get("MODEL_JUDGE", "nvidia/Nemotron-3-Ultra-550b-a55b"),
}
SANDBOX_PROVIDER = os.environ.get("SANDBOX_PROVIDER", "contree").lower()  # contree | daytona (stopgap)
SANDBOX_TIMEOUT_S = int(os.environ.get("SANDBOX_TIMEOUT_S", "900"))
SANDBOX_MAX_CONCURRENCY = int(os.environ.get("SANDBOX_MAX_CONCURRENCY", "20"))
MAX_TEST_ATTEMPTS = int(os.environ.get("MAX_TEST_ATTEMPTS", "5"))
VERDICT_RUNS = int(os.environ.get("VERDICT_RUNS", "3"))
RUNS_DIR = ROOT / "runs"


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
