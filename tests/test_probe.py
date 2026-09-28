import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from receipts.sandbox import MARKER, parse_probe_output, run_pytest

PROBE_DIR = Path(__file__).resolve().parent.parent / "receipts"


def test_probe_records_exact_exception_types(tmp_path):
    (tmp_path / "test_x.py").write_text(
        "import pytest\n"
        "def test_assert(): assert 1 == 2\n"
        "def test_value(): raise ValueError('boom')\n"
        "def test_ok(): pass\n"
        "@pytest.mark.skip\n"
        "def test_skip(): pass\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('fixture')\n"
        "def test_setup_err(broken): pass\n"
        "@pytest.mark.parametrize('v', ['a b'])\n"
        "def test_param(v): assert v == 'x'\n"
    )
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONPATH": str(PROBE_DIR), "RECEIPTS_OUT": str(out)}
    subprocess.run([sys.executable, "-m", "pytest", "test_x.py", "-p", "pytest_probe", "-p", "no:cacheprovider", "-q"],
                   cwd=tmp_path, env=env, capture_output=True)
    r = json.loads(out.read_text())
    assert r["test_x.py::test_assert"] == {"outcome": "failed", "exc": "AssertionError", "msg": "assert 1 == 2"}
    assert r["test_x.py::test_value"]["exc"] == "ValueError"
    assert r["test_x.py::test_ok"]["outcome"] == "passed"
    assert r["test_x.py::test_skip"]["outcome"] == "skipped"
    assert r["test_x.py::test_setup_err"] == {"outcome": "error", "exc": "RuntimeError", "msg": "fixture"}
    assert r["test_x.py::test_param[a b]"]["exc"] == "AssertionError"


def test_probe_records_collection_errors(tmp_path):
    (tmp_path / "test_bad.py").write_text("import nonexistent_module_xyz\n")
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONPATH": str(PROBE_DIR), "RECEIPTS_OUT": str(out)}
    subprocess.run([sys.executable, "-m", "pytest", "test_bad.py", "-p", "pytest_probe", "-p", "no:cacheprovider", "-q"],
                   cwd=tmp_path, env=env, capture_output=True)
    r = json.loads(out.read_text())
    assert r["test_bad.py"]["outcome"] == "error"


def test_parse_probe_output():
    stdout = "1 failed\n" + MARKER + '\n{"t.py::a": {"outcome": "failed", "exc": "AssertionError", "msg": "m"}}\n'
    run = parse_probe_output(stdout, "warn")
    assert run.results["t.py::a"].exc == "AssertionError"
    assert "1 failed" in run.output and "warn" in run.output


def test_parse_probe_output_without_marker_is_empty():
    assert parse_probe_output("Killed").results == {}


def test_run_pytest_refuses_empty_targets():
    with pytest.raises(ValueError):
        asyncio.run(run_pytest(None, []))
