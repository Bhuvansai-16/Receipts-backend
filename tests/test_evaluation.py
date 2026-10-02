from receipts import evaluation as E
from receipts.engine import safe_run_id

ROWS = {f"o__r-{n}": {"instance_id": f"o__r-{n}", "repo": "o/r", "patch": "GOLD"} for n in range(1, 5)}
RESOLVED = {"a1": {"o__r-1", "o__r-2"}, "a2": set(), "a3": {"o__r-3"}}
GENERATED = {"a1": set(ROWS), "a2": set(ROWS), "a3": set(ROWS)}


def fetch_ok(agent, iid):
    return f"diff --git a/x b/x\n# {agent} {iid}\n"


def _by_issue(cases):
    out = {}
    for c in cases:
        out.setdefault(c.instance_id, []).append(c)
    return out


def test_an_issue_needs_one_right_and_two_wrong_agent_patches():
    by_issue = _by_issue(E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10))
    assert sorted(by_issue) == ["o__r-1", "o__r-2", "o__r-3"]  # nobody fixed o__r-4
    assert sorted((c.kind, c.agent, c.fixed) for c in by_issue["o__r-1"]) == [
        ("gold", "", True), ("none", "", False), ("right", "a1", True), ("wrong", "a2", False), ("wrong", "a3", False)]


def test_unusable_patches_are_skipped():
    def fetch(agent, iid):  # a2's patches are missing, a3's are over 200 KB
        return None if agent == "a2" else ("x" * 300_000 if agent == "a3" else fetch_ok(agent, iid))

    assert E.select_cases(ROWS, RESOLVED, GENERATED, fetch, issues=10) == []


def test_per_repo_cap_and_issue_count():
    assert len(_by_issue(E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10, per_repo=1))) == 1
    assert len(_by_issue(E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=2))) == 2


def test_interleave_spreads_issues():
    cases = E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10)
    order = [c.instance_id for c in E.interleave(cases)]
    assert order[:3] == ["o__r-1", "o__r-2", "o__r-3"] and sorted(order) == sorted(c.instance_id for c in cases)


def test_run_ids_are_safe_and_unique():
    ids = [c.run_id for c in E.select_cases(ROWS, RESOLVED, GENERATED, fetch_ok, issues=10)]
    assert len(set(ids)) == len(ids) and all(safe_run_id(i) for i in ids)


def test_row_scores_follow_the_truth_class():
    assert E.row_scores(False, "REFUTED") == {"caught": 1, "false_proven": 0, "unproven": 0}
    assert E.row_scores(False, "REGRESSION")["caught"] == 1
    assert E.row_scores(False, "PROVEN") == {"caught": 0, "false_proven": 1, "unproven": 0}
    assert E.row_scores(True, "PROVEN") == {"proven_fix": 1, "false_refuted": 0, "unproven": 0}
    assert E.row_scores(True, "REFUTED")["false_refuted"] == 1
    assert E.row_scores(False, None) == {"caught": 0, "false_proven": 0, "unproven": 1}  # the check errored


def test_reader_scores():
    assert E.reader_scores(True, "fixed") == {"accepted_fix": 1, "false_reject": 0, "unsure": 0}
    assert E.reader_scores(False, "fixed") == {"rejected_wrong": 0, "false_accept": 1, "unsure": 0}
    assert E.reader_scores(False, "unsure")["unsure"] == 1


def test_summary_rates_are_means_over_rows_that_have_the_key():
    rows = [E.row_scores(False, "REFUTED"), E.row_scores(False, "PROVEN"), E.row_scores(True, "PROVEN")]
    assert E.summary(rows, ["caught", "false_proven", "proven_fix", "false_refuted"]) == {
        "caught": 0.5, "false_proven": 0.5, "proven_fix": 1.0, "false_refuted": 0.0}


def test_reader_prompt_truncates_the_diff():
    p = E.reader_prompt("issue text", "x" * 50_000)
    assert "issue text" in p and len(p) < 32_000


def test_an_issues_cases_run_one_after_another_and_share_the_store(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    spans, stores = [], set()

    async def check(inst, patch, emit=None, *, tests=None, run_id=None):
        stores.add(id(tests))
        start = len(spans)
        spans.append(("start", run_id))
        await asyncio.sleep(0.05)
        spans.append(("end", run_id))
        return {"verdict": "PROVEN", "reason": "r", "seconds": 1.0,
                "tokens": {"m/nemotron-3-super-120b-a12b": {"input_tokens": 1000, "output_tokens": 100}},
                "writer": {"reused_from": "x"} if start else {}}

    monkeypatch.setattr(E.engine, "check", check)
    monkeypatch.setattr(E.swebench, "load_instance", lambda iid: SimpleNamespace(gold_patch="GOLD"))
    harness = E.Harness(load_patch=lambda inputs: "diff", out_dir=tmp_path)
    rows = [{"instance_id": "o__r-1", "kind": k, "agent": "", "patch_url": "", "patch_sha256": ""} for k in ("gold", "none")]

    async def both():
        return await asyncio.gather(*(harness(r) for r in rows))

    outs = asyncio.run(both())
    assert [s[0] for s in spans] == ["start", "end", "start", "end"] and len(stores) == 1
    assert outs[0]["verdict"] == "PROVEN" and outs[1]["reused"] is True and outs[0]["cost_usd"] > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["eval-o__r-1-gold.json", "eval-o__r-1-none.json"]


def _row(repo, fixed, verdict, answer=None, reason="r", kind="wrong"):
    return {"run_id": f"eval-{repo}-{kind}", "instance_id": repo + "-1", "repo": repo, "kind": kind, "agent": "a",
            "fixed": fixed, "verdict": verdict, "reason": reason, "cost_usd": 0.01, "seconds": 20.0,
            "reused": True, "reader_answer": answer, "reader_reason": "x"}


def test_report_scores_receipts_and_the_reader_and_lists_the_misses():
    rows = [_row("o/a", False, "REFUTED", "fixed"), _row("o/a", False, "PROVEN", "not_fixed"),
            _row("o/b", True, "PROVEN", "fixed"),
            _row("o/b", True, "UNPROVEN", "unsure", reason="no valid reproducing test after 5 attempt(s): x")]
    rep = E.report(rows)
    assert rep["receipts"]["caught"] == 0.5 and rep["receipts"]["false_proven"] == 0.5
    assert rep["receipts"]["proven_fix"] == 0.5 and rep["receipts"]["false_refuted"] == 0.0
    assert rep["reader"]["false_accept"] == 0.5 and rep["reader"]["accepted_fix"] == 0.5
    assert [m["verdict"] for m in rep["misses"]] == ["PROVEN"] and len(rep["reader_false_accepts"]) == 1
    assert rep["unproven_reasons"] == {"no valid test": 1} and set(rep["per_repo"]) == {"o/a", "o/b"}
    assert E.reason_group("pipeline error: ApiTimeoutError: x") == "pipeline error (sandbox or API)"
    assert "Catch rate" in E.markdown(rep, {})


def test_find_secrets_names_what_it_found_without_repeating_it():
    assert E.find_secrets("clean text with a sk- prefix but nothing more", ["supersecretvalue"]) == []
    found = E.find_secrets("token supersecretvalue and ghp_" + "a" * 36, ["supersecretvalue"])
    assert found == ["a configured secret value", "a GitHub token"] and "supersecret" not in " ".join(found)
