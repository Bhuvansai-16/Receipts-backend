import pytest

from receipts import swebench

ROW = {"instance_id": "psf__requests-1", "repo": "psf/requests", "problem_statement": "bug", "patch": "diff",
       "PASS_TO_PASS": '["t.py::a"]', "test_patch": "HIDDEN", "FAIL_TO_PASS": '["HIDDEN"]', "hints_text": "HIDDEN"}


@pytest.fixture(autouse=True)
def fake_dataset(monkeypatch):
    rows = {"psf__requests-1": ROW, "django__django-1": {**ROW, "instance_id": "django__django-1", "repo": "django/django"}}
    monkeypatch.setattr(swebench, "_dataset", lambda: rows)


def test_load_instance_hides_ground_truth():
    inst = swebench.load_instance("psf__requests-1")
    assert inst.pass_to_pass == ["t.py::a"] and inst.gold_patch == "diff"
    assert "HIDDEN" not in repr(inst)


def test_load_instance_rejects_unknown_id():
    with pytest.raises(ValueError, match="not in SWE-bench Verified"):
        swebench.load_instance("nope__nope-1")


def test_load_instance_rejects_non_pytest_repo():
    with pytest.raises(ValueError, match="pytest"):
        swebench.load_instance("django__django-1")
