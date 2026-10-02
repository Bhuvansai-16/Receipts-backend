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
