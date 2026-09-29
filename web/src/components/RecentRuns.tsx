import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type RunSummary } from "../api";
import { PR_LABEL, timeAgo } from "../receipt";
import { VerdictChip } from "./VerdictChip";

const SHOWN = 12;
const POLL_MS = 5000;

export function RecentRuns() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .runs()
        .then((r) => alive && (setRuns(r), setError(undefined)))
        .catch((e: Error) => alive && setError(e.message));
    load();
    const timer = setInterval(() => !document.hidden && load(), POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <section aria-labelledby="recent-title">
      <h2 id="recent-title" className="section-title">
        Recent receipts
      </h2>

      {error && runs === null && (
        <p className="empty" role="alert">
          Couldn't load receipts: {error}
        </p>
      )}

      {runs === null && !error && (
        <div aria-hidden="true" style={{ marginTop: 20 }}>
          {[80, 64, 72].map((w) => (
            <span key={w} className="skeleton" style={{ width: `${w}%` }} />
          ))}
        </div>
      )}

      {runs?.length === 0 && (
        <p className="empty">No receipts yet. Run a check and it will show up here, live.</p>
      )}

      {runs && runs.length > 0 && (
        <ol className="runs">
          {runs.slice(0, SHOWN).map((run) => (
            <li key={run.run_id}>
              <Link to={`/runs/${encodeURIComponent(run.run_id)}`} className="run-row">
                <span className="run-row__id">{run.instance_id}</span>
                <span className="run-row__meta">
                  {PR_LABEL[run.pr] ?? run.pr} · {timeAgo(run.started_at)}
                </span>
                <span className="run-row__status">
                  <RunStatus run={run} />
                </span>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function RunStatus({ run }: { run: RunSummary }) {
  if (run.verdict) return <VerdictChip verdict={run.verdict} />;
  if (run.status === "error") return <span className="status">Failed to run</span>;
  return (
    <span className="status">
      <span className="pulse" aria-hidden="true" />
      {run.status === "queued" ? "Queued" : "Running"}
    </span>
  );
}
