import { useState } from "react";
import type { Evidence, RunSummaryRaw } from "../api";

/** Everything behind the verdict, one disclosure per question a reviewer would ask. */
export function EvidenceDetails({ evidence: ev, runId }: { evidence: Evidence; runId: string }) {
  const w = ev.writer;
  const f = ev.forks;
  const baseSuite = Array.isArray(f?.base_suite) ? f?.base_suite[0] : f?.base_suite;
  const log = w?.tool_log ?? [];
  const queries = w?.docs_queries ?? [];

  return (
    <section className="evidence" aria-labelledby="evidence-title">
      <h2 id="evidence-title" className="evidence__title">
        Evidence
      </h2>
      <p className="evidence__intro">Everything the verdict rests on, so you can check it yourself.</p>

      <div style={{ marginTop: 20 }}>
        {w?.test_code && (
          <details id="test" className="disclosure" open>
            <summary>
              Blind test
              <span className="disclosure__meta">
                written from the issue alone, {w.attempts} attempt{w.attempts === 1 ? "" : "s"}
              </span>
            </summary>
            <div className="disclosure__body">
              <CodeBlock filename="receipts_test.py" code={w.test_code.trim()} />
              {w.scope_check && <p>Scope check: {w.scope_check}</p>}
            </div>
          </details>
        )}

        {f && (
          <details id="runs" className="disclosure">
            <summary>
              Runs
              <span className="disclosure__meta">the test must fail on base and pass with the PR, 3 times each</span>
            </summary>
            <div className="disclosure__body">
              <RunGroup title="On base" runs={f.base_with_test} mustPass={false} />
              {Array.isArray(f.pr_with_test) ? (
                <RunGroup title="With the PR" runs={f.pr_with_test} mustPass />
              ) : (
                <p>The PR's patch did not apply at the issue's base commit, so it was never run.</p>
              )}
            </div>
          </details>
        )}

        {baseSuite && (
          <details className="disclosure">
            <summary>
              Existing tests
              <span className="disclosure__meta">the repo's own tests that must keep passing</span>
            </summary>
            <div className="disclosure__body">
              <dl className="kv">
                <dt>Pass on base</dt>
                <dd>
                  {baseSuite.tests - Object.keys(baseSuite.not_passed).length} of {baseSuite.tests}
                </dd>
                <dt>Result</dt>
                <dd>{ev.verdict === "REGRESSION" ? ev.reason : ev.verdict === "PROVEN" ? "All still pass with the PR." : "Not decisive for this verdict."}</dd>
              </dl>
              <p>Only tests that pass on base count, and a failure has to repeat on every rerun before it counts as broken.</p>
            </div>
          </details>
        )}

        {ev.second_opinion && (
          <details className="disclosure" open={!ev.second_opinion.faithful}>
            <summary>
              Second opinion
              <span className="disclosure__meta">checked before any negative verdict</span>
            </summary>
            <div className="disclosure__body">
              <p>
                <strong style={{ color: "var(--ink)" }}>{ev.second_opinion.faithful ? "Agrees" : "Doubts the test"}.</strong>{" "}
                {ev.second_opinion.reason}
              </p>
            </div>
          </details>
        )}

        {w && (
          <details className="disclosure">
            <summary>
              How the test was written
              <span className="disclosure__meta">
                {log.length} shell command{log.length === 1 ? "" : "s"}, {queries.length} documentation search
                {queries.length === 1 ? "" : "es"}
              </span>
            </summary>
            <div className="disclosure__body">
              <p>The writer never sees the pull request. Every command it ran is listed here so nothing is hidden.</p>
              {queries.length > 0 && (
                <dl className="kv">
                  <dt>Searches</dt>
                  <dd>{queries.join(" · ")}</dd>
                </dl>
              )}
              {log.length > 0 && (
                <ol className="log">
                  {log.map((entry, i) => (
                    <li key={i}>
                      {entry.cmd.length > 600 ? `${entry.cmd.slice(0, 600)}…` : entry.cmd}
                      <span className="log__exit"> · exit {entry.exit}</span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </details>
        )}
      </div>

      <p className="hint" style={{ marginTop: 18 }}>
        <a href={`/api/runs/${encodeURIComponent(runId)}`}>Raw evidence (JSON)</a>
      </p>
    </section>
  );
}

function RunGroup({ title, runs, mustPass }: { title: string; runs: RunSummaryRaw[]; mustPass: boolean }) {
  return (
    <div>
      <h3 className="run-list__head">
        {title}, {mustPass ? "must pass" : "must fail"}
      </h3>
      <ol className="run-list">
        {runs.map((run, i) => {
          const failures = Object.entries(run.not_passed);
          const passed = run.tests > 0 && failures.length === 0;
          return (
            <li key={i}>
              <p style={{ color: "var(--ink)" }}>
                Run {i + 1}: {passed ? "passed" : "failed"}
                {passed === mustPass ? ", as required" : ", not as required"}
              </p>
              {failures.map(([nodeid, f]) => (
                <p key={nodeid} className="rline__note rline__note--mono">
                  {nodeid}: {f.exc ?? f.outcome}
                  {f.msg ? `: ${f.msg.split("\n")[0]}` : ""}
                </p>
              ))}
              {run.output_tail.trim() && (
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: "pointer", fontSize: 14 }}>pytest output</summary>
                  <pre className="code code--wrap" style={{ marginTop: 8 }}>
                    {run.output_tail.trim()}
                  </pre>
                </details>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function CodeBlock({ filename, code }: { filename: string; code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code-block">
      <div className="code-block__bar">
        <span>{filename}</span>
        <button
          type="button"
          className="link-btn"
          onClick={() =>
            navigator.clipboard?.writeText(code).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            })
          }
        >
          {copied ? "Copied" : "Copy test"}
        </button>
      </div>
      <pre className="code">{code}</pre>
    </div>
  );
}
