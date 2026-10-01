import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("usage_report", Path(__file__).parent.parent / "scripts" / "usage_report.py")
usage_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage_report)

EV = {"run_id": "r1", "verdict": "PROVEN", "seconds": 50.0,
      "tokens": {"nvidia/nemotron-3-super-120b-a12b": {"input_tokens": 40_000, "output_tokens": 1_000, "total_tokens": 41_000},
                 "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": {"input_tokens": 4_000, "output_tokens": 800, "total_tokens": 4_800}},
      "writer": {"attempts": 1, "tool_log": [{}, {}, {}]}}


def test_usage_sums_tokens_by_model_and_prices_them(monkeypatch):
    monkeypatch.setattr(usage_report, "PRICES", {"nemotron-3-super-120b-a12b": (1.0, 2.0),
                                                 "NVIDIA-Nemotron-3-Nano-30B-A3B": (0.5, 0.5)})
    row = usage_report.usage(EV)
    assert row["tokens"] == 45_800 and row["commands"] == 3 and row["reused"] is False
    assert row["models"]["nemotron-3-super-120b-a12b"] == (40_000, 1_000)
    assert row["usd"] == round((40_000 * 1.0 + 1_000 * 2.0 + 4_800 * 0.5) / 1e6, 4)


def test_an_unpriced_model_reports_tokens_only(monkeypatch):
    monkeypatch.setattr(usage_report, "PRICES", {})
    assert usage_report.usage(EV)["usd"] is None
