"""Pytest plugin, uploaded into the sandbox and loaded with `-p pytest_probe`.

Records every test's outcome and exact exception type to $RECEIPTS_OUT as JSON.
Runs inside old SWE-bench envs: keep it Python 3.6 / old-pytest compatible.
"""
import json
import os

import pytest

_results = {}


def _record(nodeid, outcome, exc, msg):
    prev = _results.get(nodeid)
    if prev is None or prev["outcome"] == "passed":  # first non-pass wins (setup/call/teardown)
        _results[nodeid] = {"outcome": outcome, "exc": exc, "msg": msg[:500]}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    rep = (yield).get_result()
    exc = call.excinfo.typename if call.excinfo else None
    msg = str(call.excinfo.value) if call.excinfo else ""
    if rep.skipped:
        _record(rep.nodeid, "skipped", exc, msg)
    elif rep.failed:
        _record(rep.nodeid, "failed" if rep.when == "call" else "error", exc, msg)
    elif rep.when == "call":
        _record(rep.nodeid, "passed", None, "")


def pytest_collectreport(report):
    if report.failed:
        text = str(report.longrepr)
        # head says where it started ("in <module>"), tail says what was raised
        msg = text if len(text) <= 500 else text[:200] + "\n...\n" + text[-295:]
        _record(report.nodeid or "<collection>", "error", "CollectionError", msg)


def pytest_sessionfinish(session):
    with open(os.environ.get("RECEIPTS_OUT", "/tmp/receipts_out.json"), "w") as f:
        json.dump(_results, f)


if __name__ == "__main__":  # sandbox entrypoint: targets come from a JSON file to dodge shell quoting
    import sys

    targets = json.load(open(os.environ.get("RECEIPTS_ARGS", "/tmp/receipts_args.json")))
    sys.exit(pytest.main(targets + ["-p", "pytest_probe", "-p", "no:cacheprovider", "-q",
                                    "--continue-on-collection-errors"]))
