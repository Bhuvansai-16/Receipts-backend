import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, subscribe, type ReceiptEvent, type RunResponse } from "../api";
import { EvidenceDetails } from "../components/EvidenceDetails";
import { ReceiptCard } from "../components/ReceiptCard";
import { verdictLabel } from "../components/VerdictChip";
import { fromEvents, fromEvidence } from "../receipt";

export function RunPage() {
  const { runId = "" } = useParams();
  const [res, setRes] = useState<RunResponse | null>(null);
  const [events, setEvents] = useState<ReceiptEvent[]>([]);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string>();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let alive = true;
    let stop = () => {};
    setRes(null);
    setEvents([]);
    setMissing(false);
    setError(undefined);
    api
      .run(runId)
      .then((first) => {
        if (!alive) return;
        setRes(first);
        if (first.status !== "queued" && first.status !== "running") return;
        stop = subscribe(runId, (evs) => {
          if (!alive) return;
          setEvents(evs);
          if (evs[evs.length - 1]?.type === "done")
            api.run(runId).then((final) => alive && setRes(final), () => undefined);
        });
      })
      .catch((e: Error) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) setMissing(true);
        else setError(e.message);
      });
    return () => {
      alive = false;
      stop();
    };
  }, [runId]);

  const live = res?.status === "queued" || res?.status === "running";
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);

  const receipt = res ? (live ? fromEvents(events) : fromEvidence(res.evidence)) : null;
  const instance = res?.evidence.instance_id ?? runId;

  useEffect(() => {
    document.title = receipt?.verdict
      ? `${verdictLabel(receipt.verdict.verdict)} · ${instance} · Receipts`
      : `Checking ${instance} · Receipts`;
  });

  function reveal(sectionId: string) {
    const el = document.getElementById(sectionId);
    if (!(el instanceof HTMLDetailsElement)) return;
    el.open = true;
    // Instant jump: smooth scrolling depends on animation frames, which background tabs throttle to zero.
    el.scrollIntoView({ block: "start" });
    el.querySelector("summary")?.focus({ preventScroll: true });
  }

  if (missing)
    return (
      <div className="not-found">
        <h1 className="page-title">No receipt with that id</h1>
        <p className="lead">It may have been removed, or the link is incomplete.</p>
        <Link to="/" className="btn btn--primary">
          Check a pull request
        </Link>
      </div>
    );

  if (error)
    return (
      <div className="not-found">
        <h1 className="page-title">Couldn't load this receipt</h1>
        <p className="lead">{error}</p>
        <button type="button" className="btn btn--quiet" onClick={() => window.location.reload()}>
          Try again
        </button>
      </div>
    );

  if (!res || !receipt)
    return (
      <div className="run" aria-busy="true">
        <div className="receipt-wrap">
          <div className="receipt" aria-hidden="true">
            {[40, 90, 70, 85, 60].map((w, i) => (
              <span key={i} className="skeleton" style={{ width: `${w}%`, margin: i ? undefined : "0 auto" }} />
            ))}
          </div>
        </div>
      </div>
    );

  const started = res.evidence.started_at ? Date.parse(res.evidence.started_at) : NaN;
  return (
    <div className="run">
      <ReceiptCard
        receipt={receipt}
        evidence={res.evidence}
        live={live}
        queued={live && !events.some((e) => e.type === "status")}
        elapsed={Number.isNaN(started) ? null : Math.max(0, (now - started) / 1000)}
        onReveal={reveal}
      />
      {!live && <EvidenceDetails evidence={res.evidence} runId={runId} />}
    </div>
  );
}
