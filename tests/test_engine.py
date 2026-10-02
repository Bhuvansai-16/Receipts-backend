import asyncio
import json
from types import SimpleNamespace

import pytest

from receipts import db, engine, research, swebench, verdict
from receipts.sandbox import MARKER, TEST_ARGS, TEST_PATH
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

    async def write_test(issue, img, emit=None, **kw):
        return SimpleNamespace(test_code="def test_bug(): assert 1 == 2", attempts=1, reason="ok", log=[],
                               submissions=[])

    async def no_research(repo, issue, search=None):
        return research.Brief()

    async def judge(*a):
        return engine.Judgement(faithful=True, reason="matches issue")

    async def judge_mixed(*a):
        raise AssertionError("only a mixed result asks for this opinion")

    for name, fn in [("classify", classify), ("write_test", write_test), ("judge", judge),
                     ("judge_mixed", judge_mixed)]:
        monkeypatch.setattr(engine, name, fn)
    async def no_cases(issue):
        return []

    async def own_copy(img):
        return img

    monkeypatch.setattr(swebench, "base_image", base_image)  # Instance.base_image() goes through it
    monkeypatch.setattr(engine.research, "research", no_research)
    monkeypatch.setattr(engine.config, "contree", lambda: None)
    monkeypatch.setattr(engine, "stated_cases", no_cases, raising=False)
    monkeypatch.setattr(engine, "blind_workspace", own_copy, raising=False)


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


def test_the_test_stands_only_if_every_second_opinion_trusts_it():
    yes, no = engine.Judgement(faithful=True, reason="ok"), engine.Judgement(faithful=False, reason="env error")
    assert engine.unanimous([yes, yes, yes]) is yes
    assert engine.unanimous([yes, no, yes]) is no  # a split is doubt: UNPROVEN, never REFUTED
    assert engine.unanimous([yes, None, yes]).faithful is False  # no structured answer


def test_claim_prompt_counts_reported_wrong_behaviour_as_fix():
    assert "even if the issue also suggests an option" in engine.claim_prompt("issue", "")


def test_events_fire_in_order_for_a_proven_run():
    seen = []
    ev = asyncio.run(engine.check(INST, PATCH, emit=lambda t, d: seen.append(t)))
    assert ev["verdict"] == "PROVEN"
    assert seen[:4] == ["claim", "env_ready", "research", "test_accepted"]
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
    run_id = engine.new_run_id("x__y-1", "gold")
    path = engine.save_evidence({"instance_id": "x__y-1", "verdict": "PROVEN"}, run_id)
    assert run_id.startswith("x__y-1-gold-") and path == tmp_path / f"{run_id}.json"
    assert '"PROVEN"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["../x", "a/b", "..\\x", "", "x y", ".hidden"])
def test_save_evidence_refuses_unsafe_run_ids(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(engine.config, "RUNS_DIR", tmp_path)
    with pytest.raises(ValueError):
        engine.save_evidence({}, bad)
    assert list(tmp_path.iterdir()) == []


def test_new_run_id_is_safe_and_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.config, "RUNS_DIR", tmp_path)
    first = engine.new_run_id("x__y-1", "../my fix!")
    assert engine.safe_run_id(first) and "/" not in first and " " not in first
    assert engine.new_run_id("x__y-1", "../my fix!", taken={first}) != first  # same second, no clash


def test_environment_is_built_while_the_claim_is_classified(monkeypatch):
    started = asyncio.Event()

    async def base_image(iid):
        started.set()
        return Img("base")

    async def classify(issue, patch):
        await asyncio.wait_for(started.wait(), 1)  # a build that waits for the claim would time out here
        return engine.Claim(kind="fix", claim="c")

    monkeypatch.setattr(swebench, "base_image", base_image)
    monkeypatch.setattr(engine, "classify", classify)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "PROVEN", ev["reason"]
    assert [e["type"] for e in ev["events"]][:2] == ["claim", "env_ready"]


def test_no_checkable_claim_stops_the_environment_build(monkeypatch):
    cancelled = []

    async def base_image(iid):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def classify(issue, patch):
        await asyncio.sleep(0.01)
        return engine.Claim(kind="none", claim="docs")

    monkeypatch.setattr(swebench, "base_image", base_image)
    monkeypatch.setattr(engine, "classify", classify)
    ev = asyncio.run(asyncio.wait_for(engine.check(INST, PATCH), 5))
    assert ev["verdict"] == "NO_CHECKABLE_CLAIM" and cancelled == [True]


def test_research_brief_reaches_the_writer(monkeypatch):
    seen = {}

    async def brief(repo, issue, search=None):
        return research.Brief(queries=["q", "q2"], sources=[{"title": "t", "url": "u"}], notes="- t: docs",
                              errors=["q2: ToolException: no results"])

    async def write_test(issue, img, emit=None, **kw):
        seen.update(kw)
        return SimpleNamespace(test_code="def test_bug(): assert 1 == 2", attempts=1, reason="ok", log=[],
                               submissions=[])

    monkeypatch.setattr(engine.research, "research", brief)
    monkeypatch.setattr(engine, "write_test", write_test)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert "docs" in seen["brief"] and ev["research"]["sources"] == [{"title": "t", "url": "u"}]
    # the evidence keeps what the writer read and which searches failed
    assert ev["research"]["notes"] == "- t: docs" and ev["research"]["errors"] == ["q2: ToolException: no results"]
    assert "research" in [e["type"] for e in ev["events"]]


def _writer_sequence(monkeypatch, results):
    calls = []

    async def write_test(issue, img, emit=None, **kw):
        calls.append(kw)
        code = results[len(calls) - 1]
        return SimpleNamespace(test_code=code, attempts=1, reason="writer gave up" if code is None else "ok",
                               log=[], submissions=[{"attempt": 1, "accepted": False, "reason": "r", "code": "x = 1"}])

    monkeypatch.setattr(engine, "write_test", write_test)
    return calls


def test_writer_failure_gets_one_retry_on_the_stronger_model(monkeypatch):
    calls = _writer_sequence(monkeypatch, [None, "def test_bug(): assert 1 == 2"])
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "PROVEN" and len(calls) == 2
    assert calls[1]["role"] == "writer_strong" and "writer gave up" in calls[1]["history"]
    assert ev["writer_first"]["reason"] == "writer gave up"
    assert [e["type"] for e in ev["events"]].count("writer_retry") == 1


def test_no_retry_when_the_first_test_is_accepted(monkeypatch):
    calls = _writer_sequence(monkeypatch, ["def test_bug(): assert 1 == 2"])
    ev = asyncio.run(engine.check(INST, PATCH))
    assert len(calls) == 1 and "writer_first" not in ev


def test_only_one_retry(monkeypatch):
    calls = _writer_sequence(monkeypatch, [None, None])
    ev = asyncio.run(engine.check(INST, PATCH))
    assert len(calls) == 2 and ev["verdict"] == "UNPROVEN" and "no valid reproducing test" in ev["reason"]


class MixedImg(Img):
    """Base fails test_a and test_b; with the PR test_a passes and test_b fails with `left` (a mixed result).

    By default test_b fails exactly as on base, a partial fix in the run data (sympy #16 live)."""

    left = "b"

    async def run(self, shell=None, files=None, **kw):
        if "/tmp/pr.diff" in (files or {}):
            return type(self)("pr")
        args = json.loads(files["/tmp/receipts_args.json"])
        if args != TEST_ARGS:
            return await super().run(shell, files, **kw)
        fail = lambda msg: {"outcome": "failed", "exc": "AssertionError", "msg": msg}  # noqa: E731
        res = {"receipts_test.py::test_a": PASSED if self.name == "pr" else fail("a is wrong"),
               "receipts_test.py::test_b": fail(self.left if self.name == "pr" else "b")}
        return SimpleNamespace(stdout=MARKER + json.dumps(res), stderr="", exit_code=0)


class NewFailureImg(MixedImg):
    left = "expr1=-(x + 2), expected=-x - 2"  # test_b fails differently with the PR


def test_a_mixed_result_without_a_partial_fix_gets_no_opinion(monkeypatch):
    # sympy #15 live: the PR changed how the test failed and the opinion blamed the PR, but the test could never
    # pass (srepr never prints evaluate=False). Only a partial fix in the run data backs an opinion.
    asked = []

    async def base_image(iid):
        return NewFailureImg("base")

    async def judge_mixed(*a):
        asked.append(a)
        return engine.Judgement(faithful=True, reason="the PR misses part of the issue")

    monkeypatch.setattr(swebench, "base_image", base_image)
    monkeypatch.setattr(engine, "judge_mixed", judge_mixed)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "UNPROVEN" and ev["reason"] == verdict.MIXED
    assert not asked and "second_opinion" not in ev


def test_a_partial_fix_gets_a_second_opinion_that_never_changes_the_verdict(monkeypatch):
    # The opinion only explains: here it doubts the failing assertion, and the verdict stays Unproven.
    asked = []

    async def base_image(iid):
        return MixedImg("base")

    async def judge_mixed(issue, test_code, pr_output):
        asked.append(pr_output)
        return engine.Judgement(faithful=False, reason="expects an evaluated expression")

    monkeypatch.setattr(swebench, "base_image", base_image)
    monkeypatch.setattr(engine, "judge_mixed", judge_mixed)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "UNPROVEN" and ev["reason"] == verdict.MIXED
    assert ev["second_opinion"] == {"faithful": False, "reason": "expects an evaluated expression", "about": "mixed"}
    assert "second_opinion" in [e["type"] for e in ev["events"]] and len(asked) == 1


def test_a_failed_opinion_never_breaks_a_mixed_result(monkeypatch):
    async def base_image(iid):
        return MixedImg("base")

    async def judge_mixed(*a):
        raise TimeoutError("model unavailable")

    monkeypatch.setattr(swebench, "base_image", base_image)
    monkeypatch.setattr(engine, "judge_mixed", judge_mixed)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "UNPROVEN" and ev["reason"] == verdict.MIXED and "second_opinion" not in ev


def test_the_first_writers_evidence_survives_a_failed_retry(monkeypatch):
    # Stored before the retry runs, commands included: a retry that errors must not erase what the first did.
    calls = []

    async def write_test(issue, img, emit=None, **kw):
        calls.append(kw)
        if len(calls) == 2:
            raise TimeoutError("model unavailable")
        return SimpleNamespace(test_code=None, attempts=2, reason="writer gave up", log=["cat a.py"], submissions=[])

    monkeypatch.setattr(engine, "write_test", write_test)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "UNPROVEN" and "TimeoutError" in ev["reason"]
    assert ev["writer_first"] == {"attempts": 2, "reason": "writer gave up", "submissions": [], "tool_log": ["cat a.py"]}


def test_a_provider_outage_gets_no_retry_and_says_so(monkeypatch):
    # The model client already retried 3 times: a second writer on the same provider only burns tokens, and
    # "no valid reproducing test" would blame the writer for an outage.
    calls = []

    async def write_test(issue, img, emit=None, **kw):
        calls.append(kw)
        return SimpleNamespace(test_code=None, attempts=0, reason="agent stopped: APIConnectionError: down", log=[],
                               submissions=[], provider_error="APIConnectionError: down")

    monkeypatch.setattr(engine, "write_test", write_test)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert len(calls) == 1 and "writer_first" not in ev
    assert ev["verdict"] == "UNPROVEN" and ev["reason"] == "the test writer's model was unavailable: APIConnectionError: down"


def test_a_model_outage_anywhere_in_the_pipeline_says_so(monkeypatch):
    # The classifier is the first model call, so a Token Factory outage usually surfaces there.
    import httpx
    import openai

    async def classify(issue, patch):
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://tokenfactory.example/v1"))

    monkeypatch.setattr(engine, "classify", classify)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "UNPROVEN" and ev["reason"].startswith("a model was unavailable: APIConnectionError")


def test_checks_of_the_same_issue_and_base_share_a_key():
    same = Instance("x__y-1", "psf/requests", "issue", "another patch", [])
    assert engine.blind_test_key(INST) == engine.blind_test_key(same)
    assert engine.blind_test_key(INST) != engine.blind_test_key(Instance("x__y-2", "psf/requests", "issue", PATCH, []))
    assert engine.blind_test_key(INST) != engine.blind_test_key(Instance("x__y-1", "psf/requests", "other", PATCH, []))


def test_a_pull_request_check_keys_on_its_base_commit():
    from receipts.targets import RepoTarget
    a, b = (RepoTarget(f"o/r#{n}", "o/r", "issue", "sha1", [], None) for n in (1, 2))
    assert engine.blind_test_key(a) == engine.blind_test_key(b)
    assert engine.blind_test_key(a) != engine.blind_test_key(RepoTarget("o/r#3", "o/r", "issue", "sha2", [], None))


def _store_with(code="def test_bug(): assert 1 == 2", run_id="r1"):
    store = db.MemoryRuns()
    asyncio.run(store.save_blind_test(engine.blind_test_key(INST), INST.repo, run_id, code))
    return store


def test_a_stored_test_is_reused_without_research_or_writer(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("a reused test needs no research and no writer")

    monkeypatch.setattr(engine, "write_test", never)
    monkeypatch.setattr(engine.research, "research", never)
    seen = []
    ev = asyncio.run(engine.check(INST, PATCH, emit=lambda t, d: seen.append(t), tests=_store_with(), run_id="r2"))
    assert ev["verdict"] == "PROVEN", ev["reason"]
    assert ev["writer"]["reused_from"] == "r1" and ev["writer"]["attempts"] == 0
    assert seen.index("test_reused") < seen.index("test_accepted") and "research" not in seen


class StaleImg(Img):
    """The stored test passes on this base: it no longer reproduces the bug here."""

    async def run(self, shell=None, files=None, **kw):
        if b"stale" in (files or {}).get(TEST_PATH, b""):
            res = {"receipts_test.py::test_bug": PASSED}
            return SimpleNamespace(stdout=MARKER + json.dumps(res), stderr="", exit_code=0)
        return await super().run(shell, files, **kw)


def test_a_stale_stored_test_is_replaced_by_a_new_one(monkeypatch):
    async def base_image(iid):
        return StaleImg("base")

    monkeypatch.setattr(swebench, "base_image", base_image)
    store = _store_with(code="def test_bug(): assert 'stale'")
    ev = asyncio.run(engine.check(INST, PATCH, tests=store, run_id="r2"))
    assert ev["verdict"] == "PROVEN" and "reused_from" not in ev["writer"]
    assert store.blind_tests[engine.blind_test_key(INST)]["run_id"] == "r2"


def test_an_accepted_test_is_kept_for_the_next_check():
    store = db.MemoryRuns()
    asyncio.run(engine.check(INST, PATCH, tests=store, run_id="r1"))
    kept = store.blind_tests[engine.blind_test_key(INST)]
    assert kept["run_id"] == "r1" and kept["test_code"] == "def test_bug(): assert 1 == 2"


def _doubting(monkeypatch):
    async def judge(*a):
        return engine.Judgement(faithful=False, reason="asks more than the issue")

    monkeypatch.setattr(engine, "judge", judge)


def test_a_doubted_test_is_never_kept(monkeypatch):
    _doubting(monkeypatch)
    store = db.MemoryRuns()
    ev = asyncio.run(engine.check(INST, None, tests=store, run_id="r1"))
    assert ev["verdict"] == "UNPROVEN" and store.blind_tests == {}


def test_a_reused_test_that_gets_doubted_is_forgotten(monkeypatch):
    _doubting(monkeypatch)
    store = _store_with()
    asyncio.run(engine.check(INST, None, tests=store, run_id="r2"))
    assert store.blind_tests == {}


def test_a_broken_store_never_fails_a_check():
    class Broken:
        async def get_blind_test(self, key):
            raise OSError("database down")

        async def save_blind_test(self, *a):
            raise OSError("database down")

        async def forget_blind_test(self, key):
            raise OSError("database down")

    ev = asyncio.run(engine.check(INST, PATCH, tests=Broken(), run_id="r1"))
    assert ev["verdict"] == "PROVEN", ev["reason"]


def test_the_writers_setup_starts_while_the_claim_is_classified(monkeypatch):
    started, all_started = set(), asyncio.Event()

    def mark(name):
        started.add(name)
        if len(started) == 3:
            all_started.set()

    async def brief(repo, issue, search=None):
        mark("research")
        return research.Brief()

    async def cases(issue):
        mark("cases")
        return ["f(2) == 4"]

    async def workspace(img):
        mark("workspace")
        return img

    async def classify(issue, patch):
        await asyncio.wait_for(all_started.wait(), 1)  # setup that waits for the claim would time out here
        return engine.Claim(kind="fix", claim="c")

    seen = {}

    async def write_test(issue, img, emit=None, **kw):
        seen.update(kw)
        return SimpleNamespace(test_code="def test_bug(): assert 1 == 2", attempts=1, reason="ok", log=[],
                               submissions=[])

    for name, fn in [("classify", classify), ("stated_cases", cases), ("blind_workspace", workspace),
                     ("write_test", write_test)]:
        monkeypatch.setattr(engine, name, fn, raising=False)
    monkeypatch.setattr(engine.research, "research", brief)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert ev["verdict"] == "PROVEN", ev["reason"]
    assert seen["cases"] == ["f(2) == 4"] and seen["workspace"] is not None


def test_a_claim_with_nothing_to_check_stops_the_writers_setup(monkeypatch):
    cancelled = []

    async def slow(*a, **k):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def classify(issue, patch):
        await asyncio.sleep(0.01)
        return engine.Claim(kind="none", claim="docs")

    monkeypatch.setattr(engine, "classify", classify)
    monkeypatch.setattr(engine, "stated_cases", slow, raising=False)
    monkeypatch.setattr(engine.research, "research", slow)
    ev = asyncio.run(asyncio.wait_for(engine.check(INST, PATCH), 5))
    assert ev["verdict"] == "NO_CHECKABLE_CLAIM" and cancelled == [True, True]


def test_the_retry_reuses_the_stated_cases(monkeypatch):
    calls = _writer_sequence(monkeypatch, [None, "def test_bug(): assert 1 == 2"])
    asyncio.run(engine.check(INST, PATCH))
    assert calls[1]["cases"] == calls[0]["cases"] == [] and calls[1].get("workspace") is None  # a fresh copy
