import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("cloudrun_secrets", Path(__file__).parent.parent / "scripts" / "cloudrun_secrets.py")
cloudrun_secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloudrun_secrets)


def test_secrets_come_from_env_lines_and_skip_comments_blanks_and_empty_values():
    text = '# comment\nNEBIUS_API_KEY=abc\nTAVILY_API_KEY="t v"\nEMPTY=\n  # MODEL_JUDGE=x\nDATABASE_URL=postgres://u:p@h/db?a=1\n'
    assert cloudrun_secrets.env_values(text) == {"NEBIUS_API_KEY": "abc", "TAVILY_API_KEY": "t v",
                                                 "DATABASE_URL": "postgres://u:p@h/db?a=1"}


def test_the_deploy_flag_names_secrets_only():
    assert cloudrun_secrets.secrets_flag(["A", "B"]) == "A=A:latest,B=B:latest"


def test_secret_values_reach_gcloud_as_bytes_with_their_newlines_intact(monkeypatch):
    # Text mode on Windows would turn the private key's \n into \r\n.
    seen = {}

    def run(args, **kw):
        seen.update(kw)
        return cloudrun_secrets.subprocess.CompletedProcess(args, 0, b"out\n", b"")

    monkeypatch.setattr(cloudrun_secrets.shutil, "which", lambda name: "gcloud")
    monkeypatch.setattr(cloudrun_secrets.subprocess, "run", run)
    r = cloudrun_secrets.gcloud("secrets", "create", "K", "--data-file=-", stdin="-----BEGIN\nabc\n-----END\n")
    assert seen["input"] == b"-----BEGIN\nabc\n-----END\n" and not seen.get("text")
    assert r.stdout == "out\n"
