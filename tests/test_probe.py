import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fakes import FakeImage
from receipts.sandbox import MARKER, apply_patch, parse_probe_output, run_pytest, suite_files

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


def test_probe_keeps_where_an_import_time_error_started(tmp_path):
    # Seen in sympy #14: code at module level raised deep inside the library; the tail alone hid "in <module>".
    (tmp_path / "test_mod.py").write_text(
        "def dig(n):\n"
        "    if n == 0:\n"
        "        raise TypeError('Invalid NaN comparison')\n"
        "    return dig(n - 1)\n"
        "dig(40)\n")
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONPATH": str(PROBE_DIR), "RECEIPTS_OUT": str(out)}
    subprocess.run([sys.executable, "-m", "pytest", "test_mod.py", "-p", "pytest_probe", "-p", "no:cacheprovider", "-q"],
                   cwd=tmp_path, env=env, capture_output=True)
    msg = json.loads(out.read_text())["test_mod.py"]["msg"]
    assert "in <module>" in msg and "TypeError" in msg and len(msg) <= 500


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


def test_probe_main_continues_past_collection_errors(tmp_path):
    (tmp_path / "test_good.py").write_text("def test_a(): assert True\n")
    (tmp_path / "test_bad.py").write_text("import nonexistent_module_xyz\n")
    (tmp_path / "args.json").write_text('["test_good.py", "test_bad.py"]')
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONPATH": str(PROBE_DIR), "RECEIPTS_OUT": str(out),
           "RECEIPTS_ARGS": str(tmp_path / "args.json")}
    subprocess.run([sys.executable, str(PROBE_DIR / "pytest_probe.py")], cwd=tmp_path, env=env, capture_output=True)
    r = json.loads(out.read_text())
    assert r["test_good.py::test_a"]["outcome"] == "passed"
    assert r["test_bad.py"]["outcome"] == "error"


def test_run_pytest_activates_testbed_env_portably():
    # `. bin/activate testbed` drops the argument under dash (/bin/sh on Ubuntu) and activates base.
    img = FakeImage(stdout=MARKER + "{}")
    asyncio.run(run_pytest(img, ["t.py"]))
    assert ". /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" in img.calls[0]["shell"]


def test_apply_patch_never_fuzzes():
    img = FakeImage(exit_code=1)
    assert asyncio.run(apply_patch(img, "diff")) is None
    assert "fuzz" not in img.calls[0]["shell"] and img.calls[0]["disposable"] is False


def test_suite_files():
    assert suite_files(["a.py::T::x", "a.py::y", "b.py::z w", "c.py"]) == ["a.py", "b.py", "c.py"]
