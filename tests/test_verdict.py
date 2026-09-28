from receipts.verdict import PytestRun, TestResult, Verdict, fix_verdict, repro_check, restrict, suite_candidates

T = "receipts_test.py::test_bug"


def R(d):
    """R({nodeid: (outcome, exc, msg)}) -> PytestRun"""
    return PytestRun({k: TestResult(*v) for k, v in d.items()}, "out")


FAIL = {T: ("failed", "AssertionError", "assert 1 == 2")}
PASS = {T: ("passed",)}


def test_proven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.PROVEN


def test_refuted_same_assertion():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(FAIL)] * 3, None, None)
    assert v is Verdict.REFUTED


def test_regression_consistent_suite_failure():
    base_suite = R({"t.py::a": ("passed",), "t.py::b": ("passed",)})
    pr_full = R({"t.py::a": ("passed",), "t.py::b": ("failed", "AssertionError", "x")})
    rerun = R({"t.py::b": ("failed", "AssertionError", "x")})
    base_rerun = R({"t.py::b": ("passed",)})
    v, why = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, [base_suite, base_rerun, base_rerun], [pr_full, rerun, rerun])
    assert v is Verdict.REGRESSION and "t.py::b" in why


def test_flaky_suite_failure_is_not_regression():
    base_suite = R({"t.py::b": ("passed",)})
    pr_full = R({"t.py::b": ("failed", "AssertionError", "x")})
    rerun_ok = R({"t.py::b": ("passed",)})
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, [base_suite] * 3, [pr_full, rerun_ok, rerun_ok])
    assert v is Verdict.PROVEN


def test_suite_failing_on_base_is_not_regression():
    base_suite = R({"t.py::net": ("failed", "ConnectionError", "no network")})
    pr_full = R({"t.py::net": ("failed", "ConnectionError", "no network")})
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, [base_suite], [pr_full])
    assert v is Verdict.PROVEN


def test_suite_could_not_run_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, [R({"t.py::a": ("passed",)})], [PytestRun()])
    assert v is Verdict.UNPROVEN


def test_base_import_error_is_unproven():
    v, why = fix_verdict([R({T: ("error", "ImportError", "no module")})] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN and "AssertionError" in why


def test_base_other_exception_is_unproven():
    v, _ = fix_verdict([R({T: ("failed", "ValueError", "boom")})] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_base_all_pass_is_unproven():
    v, _ = fix_verdict([R(PASS)] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_base_empty_run_is_unproven():
    v, _ = fix_verdict([PytestRun()] * 3, [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_base_flaky_is_unproven():
    other = R({T: ("failed", "AssertionError", "a"), "receipts_test.py::t2": ("failed", "AssertionError", "b")})
    v, _ = fix_verdict([R(FAIL), R(FAIL), other], [R(PASS)] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_patch_not_applied_is_unproven():
    v, why = fix_verdict([R(FAIL)] * 3, None, None, None)
    assert v is Verdict.UNPROVEN and "apply" in why


def test_mixed_pr_runs_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS), R(FAIL), R(FAIL)], None, None)
    assert v is Verdict.UNPROVEN


def test_pr_fails_differently_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [R({T: ("failed", "AssertionError", "assert 5 == 2")})] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_empty_pr_run_is_unproven():
    v, _ = fix_verdict([R(FAIL)] * 3, [PytestRun()] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_memory_addresses_normalised():
    b = R({T: ("failed", "AssertionError", "assert <Obj at 0x7f00aa> == 1")})
    p = R({T: ("failed", "AssertionError", "assert <Obj at 0x7f99bb> == 1")})
    v, _ = fix_verdict([b] * 3, [p] * 3, None, None)
    assert v is Verdict.REFUTED


def test_repro_allows_extra_passing_tests():
    ok, _ = repro_check(R({**FAIL, "receipts_test.py::sanity": ("passed",)}))
    assert ok


def test_network_flap_after_base_is_not_regression():
    ok = R({"t.py::net": ("passed",)})
    down = R({"t.py::net": ("failed", "ConnectionError", "503")})
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, [ok, down, down], [down, down, down])
    assert v is Verdict.PROVEN


def test_empty_base_rerun_is_unproven():
    ok = R({"t.py::b": ("passed",)})
    bad = R({"t.py::b": ("failed", "AssertionError", "x")})
    v, _ = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, [ok, PytestRun(), ok], [bad, bad, bad])
    assert v is Verdict.UNPROVEN


def test_real_hex_values_are_not_normalised():
    b = R({T: ("failed", "AssertionError", "assert '0x1f' == '0x20'")})
    p = R({T: ("failed", "AssertionError", "assert '0x1e' == '0x20'")})
    v, _ = fix_verdict([b] * 3, [p] * 3, None, None)
    assert v is Verdict.UNPROVEN


def test_blind_test_missing_on_pr_is_unproven():
    base = R({**FAIL, "receipts_test.py::sanity": ("passed",)})
    pr = R({"receipts_test.py::sanity": ("passed",)})
    v, why = fix_verdict([base] * 3, [pr] * 3, None, None)
    assert v is Verdict.UNPROVEN and "did not run" in why


def test_restrict_keeps_only_listed_ids():
    run = R({"a": ("passed",), "b": ("failed", "E", "")})
    r = restrict(run, ["a", "zzz"])
    assert list(r.results) == ["a"] and r.output == run.output


def test_suite_candidates():
    base = R({"a": ("passed",), "b": ("passed",), "c": ("failed", "E", "")})
    pr = R({"a": ("passed",), "b": ("failed", "E", ""), "c": ("failed", "E", "")})
    assert suite_candidates(base, pr) == ["b"]
