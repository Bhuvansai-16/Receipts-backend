import { describe, expect, it } from "vitest";
import type { Evidence, ReceiptEvent } from "./api";
import { activeStep, formatDuration, formatTokens, fromEvents, fromEvidence, issueNumber, repoOf, timeAgo } from "./receipt";

const e = (type: string, data: Record<string, unknown> = {}): ReceiptEvent => ({ type, data });
const fail = (msg: string) => ({ tests: 1, not_passed: { "t.py::t": { outcome: "failed", exc: "AssertionError", msg } }, output_tail: "out" });
const pass = { tests: 1, not_passed: {}, output_tail: "ok" };

describe("fromEvents", () => {
  it("prints lines in the order the check produces them", () => {
    const r = fromEvents([
      e("status", { status: "running" }),
      e("claim", { kind: "fix", claim: "GET must not send Content-Length" }),
      e("env_ready"),
      e("writer_submit", { attempt: 1, accepted: false, reason: "every test passed" }),
      e("writer_submit", { attempt: 2, accepted: true, reason: "fails with AssertionError" }),
      e("test_accepted", { attempts: 2 }),
      e("fork", { side: "pr", n: 2, passed: true, message: "" }),
      e("fork", { side: "base", n: 1, passed: false, message: "assert 1 == 2" }),
      e("fork", { side: "pr", n: 1, passed: true, message: "" }),
    ]);
    expect(r.claim?.kind).toBe("fix");
    expect(r.envReady).toBe(true);
    expect(r.submissions.map((s) => s.accepted)).toEqual([false, true]);
    expect(r.testAttempts).toBe(2);
    expect(r.base).toEqual([{ passed: false, message: "assert 1 == 2" }]);
    expect(r.pr).toHaveLength(2);
    expect(r.done).toBe(false);
    expect(activeStep(r)).toBe("runs");
  });

  it("counts the writer's shell commands while it works", () => {
    const r = fromEvents([e("claim", { kind: "fix", claim: "c" }), e("env_ready"), e("writer_progress", { commands: 1 }), e("writer_progress", { commands: 7 })]);
    expect(r.writerCommands).toBe(7);
    expect(activeStep(r)).toBe("writer");
  });

  it("finishes with a verdict and done", () => {
    const r = fromEvents([
      e("claim", { kind: "fix", claim: "c" }),
      e("verdict", { verdict: "UNPROVEN", reason: "no valid test", seconds: 12.5, tokens: 1200 }),
      e("done"),
    ]);
    expect(r.verdict).toEqual({ verdict: "UNPROVEN", reason: "no valid test", seconds: 12.5, tokens: 1200 });
    expect(r.done).toBe(true);
    expect(activeStep(r)).toBe("done");
  });
});

describe("fromEvidence (runs saved before events existed)", () => {
  const ev: Evidence = {
    instance_id: "psf__requests-1142",
    repo: "psf/requests",
    pr: "gold",
    claim: { kind: "fix", claim: "GET must not send Content-Length" },
    writer: { attempts: 1, reason: "ok", test_code: "def test_x(): ...", tool_log: [] },
    forks: {
      base_with_test: [fail("assert 'Content-Length' not in {...}"), fail("x"), fail("x")],
      pr_with_test: [pass, pass, pass],
      base_suite: [{ tests: 5, not_passed: {}, output_tail: "" }],
      pr_suite: [{ tests: 26, not_passed: { "a::b": { outcome: "failed", exc: "E", msg: "" } }, output_tail: "" }],
    },
    verdict: "PROVEN",
    reason: "test fails on base and passes on the PR in 3/3 runs",
    seconds: 90.8,
    tokens: { nano: { total_tokens: 600 }, lightning: { total_tokens: 179000 } },
  };

  it("derives every receipt line", () => {
    const r = fromEvidence(ev);
    expect(r.envReady).toBe(true);
    expect(r.testAttempts).toBe(1);
    expect(r.base.map((t) => t.passed)).toEqual([false, false, false]);
    expect(r.base[0].message).toBe("assert 'Content-Length' not in {...}");
    expect(r.pr.map((t) => t.passed)).toEqual([true, true, true]);
    expect(r.suite).toEqual({ basePassed: 5, baseTotal: 5, broken: 0 });
    expect(r.verdict).toEqual({ verdict: "PROVEN", reason: ev.reason, seconds: 90.8, tokens: 179600 });
    expect(r.done).toBe(true);
  });

  it("reads broken existing tests from a REGRESSION reason", () => {
    const r = fromEvidence({ ...ev, verdict: "REGRESSION", reason: "fixes the claim but breaks 2 existing test(s): a, b" });
    expect(r.suite?.broken).toBe(2);
  });

  it("handles a patch that did not apply and the old single-summary suite shape", () => {
    const r = fromEvidence({
      ...ev,
      forks: { ...ev.forks!, pr_with_test: "patch did not apply", base_suite: null, pr_suite: null },
      verdict: "UNPROVEN",
    });
    expect(r.patchApplied).toBe(false);
    expect(r.pr).toEqual([]);
    expect(r.suite).toBeUndefined();
    const old = fromEvidence({ ...ev, forks: { ...ev.forks!, base_suite: { tests: 3, not_passed: {}, output_tail: "" } } });
    expect(old.suite?.baseTotal).toBe(3);
  });

  it("handles a run that stopped before the writer (no forks, no claim)", () => {
    const r = fromEvidence({ instance_id: "x__y-1", verdict: "UNPROVEN", reason: "pipeline error", seconds: 7, tokens: null });
    expect(r.envReady).toBe(false);
    expect(r.base).toEqual([]);
    expect(r.verdict?.tokens).toBe(0);
  });

  it("prefers stored events and still takes the verdict from the evidence", () => {
    const r = fromEvidence({
      ...ev,
      events: [e("claim", { kind: "fix", claim: "from events" }), e("writer_submit", { attempt: 1, accepted: true, reason: "" })],
    });
    expect(r.claim?.claim).toBe("from events");
    expect(r.submissions).toHaveLength(1);
    expect(r.verdict?.verdict).toBe("PROVEN");
    expect(r.done).toBe(true);
  });
});

describe("formatting", () => {
  it("formats duration, tokens and issue numbers", () => {
    expect(formatDuration(42.4)).toBe("42 s");
    expect(formatDuration(90.8)).toBe("1 min 31 s");
    expect(formatTokens(179600)).toBe("180K tokens");
    expect(formatTokens(950)).toBe("950 tokens");
    expect(formatTokens(1_380_000)).toBe("1.4M tokens");
    expect(issueNumber("psf__requests-1142")).toBe("1142");
    expect(issueNumber("scikit-learn__scikit-learn-13328")).toBe("13328");
    expect(repoOf("scikit-learn__scikit-learn-13328")).toBe("scikit-learn/scikit-learn");
    expect(repoOf("psf__requests-1142")).toBe("psf/requests");
  });
});

describe("timeAgo", () => {
  const now = Date.parse("2026-09-29T12:00:00Z");
  it("uses words people read at a glance", () => {
    expect(timeAgo("2026-09-29T11:59:40Z", now)).toBe("just now");
    expect(timeAgo("2026-09-29T11:55:00Z", now)).toBe("5 minutes ago");
    expect(timeAgo("2026-09-29T09:00:00Z", now)).toBe("3 hours ago");
    expect(timeAgo("2026-09-28T11:00:00Z", now)).toBe("yesterday");
    expect(timeAgo("not a date", now)).toBe("");
  });
});
