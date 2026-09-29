import sys
import types

from receipts import swebench

ROW = {"instance_id": "psf__requests-1", "repo": "psf/requests", "problem_statement": "bug", "patch": "d",
       "PASS_TO_PASS": "[]"}


def test_dataset_is_downloaded_once_then_read_from_disk(monkeypatch, tmp_path):
    calls = []
    fake = types.ModuleType("datasets")
    fake.load_dataset = lambda *a, **k: calls.append(a) or [ROW]
    monkeypatch.setitem(sys.modules, "datasets", fake)
    monkeypatch.setattr(swebench, "CACHE", tmp_path / "swebench_verified.json")
    swebench._dataset.cache_clear()
    try:
        assert swebench._dataset()["psf__requests-1"]["repo"] == "psf/requests"
        swebench._dataset.cache_clear()  # a fresh process
        assert swebench._dataset()["psf__requests-1"]["repo"] == "psf/requests"
        assert len(calls) == 1
        assert not list(tmp_path.glob("*.tmp"))  # written atomically
    finally:
        swebench._dataset.cache_clear()
