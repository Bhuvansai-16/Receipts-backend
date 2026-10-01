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
