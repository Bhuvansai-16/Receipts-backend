from receipts.verdict import PytestRun, TestResult, Verdict, fix_verdict, partial_fix, repro_check, restrict, suite_candidates

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


def test_repro_explains_other_errors_raised_at_import():
    # Seen in sympy #14: the buggy call sat at module level, so pytest reported only a CollectionError.
    msg = ("receipts_test.py:9: in <module>\n    sorted([f(nan), f(1)], key=default_sort_key)\n ... \n"
           "E   TypeError: Invalid NaN comparison")
    ok, reason = repro_check(R({"receipts_test.py": ("error", "CollectionError", msg)}))
    assert not ok and "TypeError" in reason and "def test_" in reason and "assert False" in reason


def test_repro_explains_an_assert_at_module_level():
    # Seen twice: the writer's assert ran at import time, and "CollectionError" alone never got it fixed.
    msg = 'receipts_test.py:7: in <module>\n    assert result == -z**2\nE   AssertionError: Expected -z**2'
    ok, reason = repro_check(R({"receipts_test.py": ("error", "CollectionError", msg)}))
    assert not ok and "module level" in reason and "def test_" in reason


def test_partial_fix_needs_some_but_not_all_base_failures_unchanged_on_every_pr_run():
    # Only this pattern backs a "the change missed part of it" reading (review, Critical 1).
    a, b = "receipts_test.py::test_a", "receipts_test.py::test_b"
    base = R({a: ("failed", "AssertionError", "a"), b: ("failed", "AssertionError", "b")})
    part = R({a: ("passed",), b: ("failed", "AssertionError", "b")})  # #16: one case fixed, one exactly as before
    differently = R({a: ("passed",), b: ("failed", "AssertionError", "b, but new")})
    one_test = R({a: ("failed", "AssertionError", "a")})
    one_test_differently = R({a: ("failed", "AssertionError", "a, differently")})  # #15
    assert partial_fix([base] * 3, [part] * 3)
    assert not partial_fix([base] * 3, [differently] * 3)
    assert not partial_fix([one_test] * 3, [one_test_differently] * 3)
    assert not partial_fix([base] * 3, [part, PytestRun(), part])  # a run that never executed (sandbox timeout)
    assert not partial_fix([base] * 3, [R({a: ("passed",), b: ("passed",)})] * 3)
    assert not partial_fix([base] * 3, [part, R({a: ("failed", "AssertionError", "a"), b: ("passed",)}), part])  # runs disagree
    assert not partial_fix([base] * 3, [R({b: ("failed", "AssertionError", "b")})] * 3)  # test_a never ran: not fixed


def test_proven_without_existing_tests_does_not_say_they_hold():
    # No tests found for the changed files means no suite ran; "existing tests hold" would claim what never ran.
    _, why = fix_verdict([R(FAIL)] * 3, [R(PASS)] * 3, None, None)
    assert "existing tests hold" not in why and "no existing tests" in why


def test_a_pr_run_that_never_executed_is_not_called_mixed():
    # A sandbox outage is not "fails differently with the change": nothing ran.
    v, why = fix_verdict([R(FAIL)] * 3, [R(PASS), PytestRun(), R(PASS)], None, None)
    assert v is Verdict.UNPROVEN and "did not run on the PR in 1/3 runs" in why
