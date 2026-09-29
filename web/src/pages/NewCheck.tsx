import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type InstanceDetail, type InstanceSummary, type PrKind } from "../api";
import { IssuePicker } from "../components/IssuePicker";
import { RecentRuns } from "../components/RecentRuns";
import { issueNumber } from "../receipt";

const MAX_DIFF_BYTES = 200_000;

// Instances with known-good end-to-end runs: a quick start for judges.
const QUICK_PICKS = ["psf__requests-1142", "pydata__xarray-4629", "scikit-learn__scikit-learn-13328"];

const PR_OPTIONS: { value: PrKind; label: string; hint: string }[] = [
  { value: "gold", label: "Real fix", hint: "The change that closed this issue upstream." },
  { value: "none", label: "Do-nothing PR", hint: "A pull request that changes nothing. It should not pass." },
  { value: "diff", label: "Paste a diff", hint: "A unified diff, applied with git apply at the issue's base commit." },
];

export function NewCheckPage() {
  const navigate = useNavigate();
  const [instances, setInstances] = useState<InstanceSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string>();
  const [instanceId, setInstanceId] = useState<string | null>(null);
  const [detail, setDetail] = useState<InstanceDetail | null>(null);
  const [fullIssue, setFullIssue] = useState(false);
  const [pr, setPr] = useState<PrKind>("gold");
  const [diff, setDiff] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    document.title = "Check a pull request · Receipts";
  }, []);

  const loadInstances = () => {
    setLoadError(undefined);
    api.instances().then(setInstances, (e: Error) => setLoadError(e.message));
  };
  useEffect(loadInstances, []);

  useEffect(() => {
    setDetail(null);
    setFullIssue(false);
    if (!instanceId) return;
    let alive = true;
    api.instance(instanceId).then((d) => alive && setDetail(d), () => undefined);
    return () => {
      alive = false;
    };
  }, [instanceId]);

  const diffBytes = new Blob([diff]).size;
  const hint = PR_OPTIONS.find((o) => o.value === pr)!.hint;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    if (!instanceId) return setError("Choose an issue to check.");
    if (pr === "diff" && !diff.trim()) return setError("Paste a unified diff, or choose the real fix or a do-nothing PR.");
    if (pr === "diff" && diffBytes > MAX_DIFF_BYTES) return setError("That diff is larger than 200 KB.");
    setBusy(true);
    try {
      const { run_id } = await api.start({ instance_id: instanceId, pr, ...(pr === "diff" ? { diff } : {}) });
      navigate(`/runs/${encodeURIComponent(run_id)}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="home">
      <section aria-labelledby="page-title">
        <h1 id="page-title" className="page-title">
          Check a pull request
        </h1>
        <p className="lead">
          Receipts writes the missing test from the issue alone, runs it on the code before and after the change, and
          hands you the evidence.
        </p>

        <form className="form" onSubmit={submit} noValidate>
          <div className="field">
            <label className="label" htmlFor="issue">
              Issue
            </label>
            <IssuePicker inputId="issue" hintId="issue-hint" instances={instances} value={instanceId} onChange={setInstanceId} />
            {loadError ? (
              <p className="error" role="alert">
                Couldn't load issues: {loadError}{" "}
                <button type="button" className="link-btn" onClick={loadInstances}>
                  Try again
                </button>
              </p>
            ) : (
              <p id="issue-hint" className="hint">
                {instances ? `${instances.length} real issues from SWE-bench Verified, Python repos tested with pytest.` : " "}
              </p>
            )}
            {!instanceId && instances && (
              <div className="quick-picks">
                <span>Try</span>
                {QUICK_PICKS.filter((id) => instances.some((i) => i.id === id)).map((id) => (
                  <button key={id} type="button" className="quick-pick" onClick={() => setInstanceId(id)}>
                    {id}
                  </button>
                ))}
              </div>
            )}
            {instanceId && (
              <div className="issue" aria-live="polite">
                <div className="issue__head">
                  <span>
                    Issue #{issueNumber(instanceId)}
                    {detail ? ` · ${detail.repo}` : ""}
                  </span>
                  {detail && detail.problem_statement.length > 480 && (
                    <button type="button" className="link-btn" onClick={() => setFullIssue((v) => !v)} aria-expanded={fullIssue}>
                      {fullIssue ? "Show less" : "Read the full issue"}
                    </button>
                  )}
                </div>
                {detail ? (
                  <p className={`issue__text${fullIssue ? " issue__text--full" : ""}`}>{detail.problem_statement.trim()}</p>
                ) : (
                  <div aria-hidden="true">
                    <span className="skeleton" style={{ width: "92%" }} />
                    <span className="skeleton" style={{ width: "70%" }} />
                  </div>
                )}
              </div>
            )}
          </div>

          <fieldset className="field">
            <legend className="label" style={{ marginBottom: 8 }}>
              Pull request
            </legend>
            <div className="segmented">
              {PR_OPTIONS.map((o) => (
                <label key={o.value}>
                  <input
                    type="radio"
                    name="pr"
                    value={o.value}
                    checked={pr === o.value}
                    onChange={() => setPr(o.value)}
                    aria-describedby="pr-hint"
                  />
                  {o.label}
                </label>
              ))}
            </div>
            <p id="pr-hint" className="hint">
              {hint}
            </p>
          </fieldset>

          {pr === "diff" && (
            <div className="field">
              <label className="label" htmlFor="diff">
                Diff
              </label>
              <textarea
                id="diff"
                className="textarea"
                value={diff}
                onChange={(e) => setDiff(e.target.value)}
                placeholder={"diff --git a/requests/models.py b/requests/models.py\n--- a/requests/models.py\n+++ b/requests/models.py\n@@ ..."}
                spellCheck={false}
                aria-describedby="diff-hint"
              />
              <p id="diff-hint" className="hint">
                {(diffBytes / 1000).toFixed(1)} KB of 200 KB
              </p>
            </div>
          )}

          <div className="submit-row">
            <button type="submit" className="btn btn--primary" disabled={busy || !instanceId}>
              {busy ? "Starting check…" : "Run check"}
            </button>
            <p className="hint">Takes about two minutes. The test is written without seeing the pull request.</p>
          </div>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </form>
      </section>

      <aside className="home__recent">
        <RecentRuns />
      </aside>
    </div>
  );
}
