import asyncio
import json
from types import SimpleNamespace

import pytest

from receipts import engine
from receipts.sandbox import MARKER, TEST_ARGS
from receipts.swebench import Instance

P2P = ["t.py::a", "t.py::b"]
PATCH = "diff --git a/m.py b/m.py\n"
INST = Instance("x__y-1", "psf/requests", "issue", PATCH, P2P)
PASSED = {"outcome": "passed", "exc": None, "msg": ""}


class Img:
    """Fake Contree image: base fails the blind test, PR passes it; every suite test passes."""

    def __init__(self, name):
        self.name, self.exit_code = name, 0

    async def run(self, shell=None, files=None, **kw):
        if "/tmp/pr.diff" in (files or {}):
            return Img("pr")
        args = json.loads(files["/tmp/receipts_args.json"])
        if args == TEST_ARGS:
            res = {"receipts_test.py::test_bug": PASSED if self.name == "pr"
                   else {"outcome": "failed", "exc": "AssertionError", "msg": "assert 1 == 2"}}
        else:  # file-level run: includes a test that is not in PASS_TO_PASS
            res = {i: PASSED for i in P2P + ["t.py::extra"] if i.split("::")[0] in args}
        return SimpleNamespace(stdout=MARKER + json.dumps(res), stderr="", exit_code=0)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    async def classify(issue, patch):
        return engine.Claim(kind="fix", claim="c")

    async def base_image(iid):
        return Img("base")

    async def write_test(issue, img, emit=None):
        return SimpleNamespace(test_code="def test_bug(): assert 1 == 2", attempts=1, reason="ok", queries=[], log=[])

    async def judge(*a):
        return engine.Judgement(faithful=True, reason="matches issue")

    for name, fn in [("classify", classify), ("base_image", base_image), ("write_test", write_test), ("judge", judge)]:
        monkeypatch.setattr(engine, name, fn)
    monkeypatch.setattr(engine.config, "contree", lambda: None)


def test_gold_patch_is_proven_and_suite_is_restricted_to_pass_to_pass():
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "PROVEN", ev["reason"]
    assert ev["forks"]["base_suite"][0]["tests"] == 2


def test_noop_patch_is_refuted_after_second_opinion():
    ev = asyncio.run(engine.check(INST, None))
    assert ev["verdict"] == "REFUTED", ev["reason"]
    assert ev["second_opinion"]["faithful"] is True


def test_claim_prompt_omits_empty_file_list():
    assert "Files changed" not in engine.claim_prompt("issue", "")
    assert "m.py" in engine.claim_prompt("issue", PATCH)


def test_majority_vote_picks_most_common_kind():
    fix, none = engine.Claim(kind="fix", claim="a"), engine.Claim(kind="none", claim="b")
    assert engine.majority([fix, none, fix]).kind == "fix"
    assert engine.majority([none, none, fix]).kind == "none"


def test_claim_prompt_counts_reported_wrong_behaviour_as_fix():
    assert "even if the issue also suggests an option" in engine.claim_prompt("issue", "")


def test_events_fire_in_order_for_a_proven_run():
    seen = []
    ev = asyncio.run(engine.check(INST, PATCH, emit=lambda t, d: seen.append(t)))
    assert ev["verdict"] == "PROVEN"
    assert seen[:3] == ["claim", "env_ready", "test_accepted"]
    assert seen.count("fork") == 6 and seen[-2:] == ["verdict", "done"]
    assert seen.index("suite") > max(i for i, t in enumerate(seen) if t == "fork")
    assert [e["type"] for e in ev["events"]] == seen  # stored for replay


def test_refuted_run_emits_second_opinion_before_verdict():
    seen = []
    asyncio.run(engine.check(INST, None, emit=lambda t, d: seen.append(t)))
    assert seen.index("second_opinion") < seen.index("verdict")


def test_fork_events_say_which_side_and_whether_it_passed():
    forks = []
    asyncio.run(engine.check(INST, PATCH, emit=lambda t, d: forks.append(d) if t == "fork" else None))
    assert {(f["side"], f["passed"]) for f in forks} == {("base", False), ("pr", True)}


def test_save_evidence_writes_run_file(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "RUNS_DIR", tmp_path)
    path = engine.save_evidence({"instance_id": "x__y-1", "verdict": "PROVEN"}, "gold")
    assert path.parent == tmp_path and path.name.startswith("x__y-1-gold-") and path.suffix == ".json"
    assert '"PROVEN"' in path.read_text(encoding="utf-8")
