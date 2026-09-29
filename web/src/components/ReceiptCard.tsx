import { Check, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { Evidence, Verdict } from "../api";
import {
  activeStep,
  formatDuration,
  formatTokens,
  issueNumber,
  PR_LABEL,
  repoOf,
  type Receipt,
  type Step,
  type Tile,
} from "../receipt";
import { VerdictChip, verdictLabel, verdictTone } from "./VerdictChip";

const CLAIM_KIND: Record<string, string> = {
  fix: "bug fix",
  dependency: "dependency bump",
  none: "no checkable claim",
};

const STEP_TEXT: Record<Step, string> = {
  claim: "Reading the claim",
  env: "Preparing the sandbox",
  writer: "Writing the blind test",
  runs: "Running the test on base and with the PR",
  suite: "Checking the existing tests",
  verdict: "Deciding the verdict",
  done: "",
};

const STRIP: Record<Verdict, string> = {
  PROVEN: "Fast lane: this PR does what it claims. Review it first.",
  REFUTED: "This PR doesn't fix the issue as described.",
  REGRESSION: "It fixes the issue but breaks existing tests. Look at those first.",
  UNPROVEN: "Not enough evidence either way. This says nothing against the PR.",
  NO_CHECKABLE_CLAIM: "This PR doesn't claim to fix a bug, so there is nothing to check.",
};

interface Props {
  receipt: Receipt;
  evidence: Evidence;
  live: boolean;
  queued: boolean;
  elapsed: number | null;
  onReveal: (sectionId: string) => void;
}

export function ReceiptCard({ receipt: r, evidence, live, queued, elapsed, onReveal }: Props) {
  const step: Step = live ? activeStep(r) : "done";
  const printing = (s: Step) => live && !queued && step === s;
  const [copied, setCopied] = useState(false);

  const repo = evidence.repo ?? repoOf(evidence.instance_id);
  const pr = evidence.pr ? (PR_LABEL[evidence.pr] ?? evidence.pr) : null;
  const wroteTest = r.testAttempts !== undefined;
  const announce = queued ? "Waiting for a free sandbox" : live ? STEP_TEXT[step] : r.verdict ? `Verdict: ${verdictLabel(r.verdict.verdict)}` : "";

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked: the URL bar still has it */
    }
  }

  return (
    <div className="receipt-wrap">
      <article className={`receipt ${live ? "receipt--live" : "receipt--static"}`} aria-labelledby="receipt-title" aria-busy={live}>
        <header className="receipt__head">
          <h1 id="receipt-title" className="receipt__title">
            Receipt
          </h1>
          <p className="receipt__meta">
            {repo} · issue #{issueNumber(evidence.instance_id)}
            {pr ? ` · ${pr}` : ""}
          </p>
        </header>
        <p className="visually-hidden" aria-live="polite">
          {announce}
        </p>

        <hr className="receipt__rule" />

        {queued && (
          <Line label="Queue" value={<>waiting for a sandbox <Cursor /></>} pending />
        )}

        {(r.claim || printing("claim")) && (
          <Line
            label="Claim"
            value={r.claim ? CLAIM_KIND[r.claim.kind] ?? r.claim.kind : <>reading <Cursor /></>}
            pending={!r.claim}
            note={r.claim?.claim}
            clamp
          />
        )}

        {(r.envReady || printing("env")) && (
          <Line label="Sandbox" value={r.envReady ? "ready" : <>preparing <Cursor /></>} pending={!r.envReady} />
        )}

        {(wroteTest || printing("writer") || (!live && r.envReady)) && (
          <Line
            label="Blind test"
            pending={!wroteTest && live}
            value={
              wroteTest ? (
                `${r.testAttempts} attempt${r.testAttempts === 1 ? "" : "s"}`
              ) : live ? (
                <>
                  {r.submissions.length ? `attempt ${r.submissions.length + 1}` : "writing"}
                  {r.writerCommands > 0 && ` · ${r.writerCommands} command${r.writerCommands === 1 ? "" : "s"}`} <Cursor />
                </>
              ) : (
                "no valid test"
              )
            }
            note={
              wroteTest
                ? "written from the issue alone"
                : live && r.submissions.length
                  ? `Rejected: ${r.submissions[r.submissions.length - 1].reason}`
                  : undefined
            }
          />
        )}

        {(r.base.length > 0 || printing("runs")) && (
          <Line
            label="On base, must fail"
            value={<Tiles tiles={r.base} mustPass={false} pending={live} />}
            note={r.base.find((t) => !t.passed)?.message || undefined}
            mono
            clamp
          />
        )}

        {(r.pr.length > 0 || printing("runs") || (!r.patchApplied && wroteTest)) && (
          <Line
            label="With the PR, must pass"
            value={r.patchApplied ? <Tiles tiles={r.pr} mustPass pending={live} /> : "patch did not apply"}
          />
        )}

        {(r.suite || printing("suite")) && (
          <Line
            label="Existing tests"
            pending={!r.suite}
            value={r.suite ? suiteText(r) : <>running <Cursor /></>}
          />
        )}

        {r.secondOpinion && (
          <Line
            label="Second opinion"
            value={r.secondOpinion.faithful ? "agrees" : "doubts the test"}
            note={r.secondOpinion.reason}
            clamp
          />
        )}

        {(r.verdict || printing("verdict")) && (
          <>
            <hr className="receipt__rule" />
            <div className="total">
              <span className="total__label">Verdict</span>
              {r.verdict ? <VerdictChip verdict={r.verdict.verdict} size="lg" /> : <Cursor />}
            </div>
            {r.verdict && <p className="total__reason">{sentence(r.verdict.reason)}</p>}
          </>
        )}

        {r.error && (
          <p className="run-error" role="alert">
            The check stopped: {r.error}
          </p>
        )}

        <div className="total__meta">
          {r.verdict && !live ? (
            <>
              <span>{formatDuration(r.verdict.seconds)}</span>
              <span>{formatTokens(r.verdict.tokens)}</span>
            </>
          ) : (
            <>
              <span>{elapsed !== null ? `${formatDuration(elapsed)} elapsed` : ""}</span>
              <span>{live && step === "runs" ? `${r.base.length + r.pr.length} of 6 runs` : ""}</span>
            </>
          )}
        </div>

        {r.verdict && !live && (
          <>
            <p className={`strip strip--${verdictTone(r.verdict.verdict)}`}>{STRIP[r.verdict.verdict]}</p>
            <div className="receipt__actions">
              {wroteTest && (
                <a href="#test" onClick={(e) => (e.preventDefault(), onReveal("test"))}>
                  See the test
                </a>
              )}
              {r.base.length > 0 && (
                <a href="#runs" onClick={(e) => (e.preventDefault(), onReveal("runs"))}>
                  Run output
                </a>
              )}
              <button type="button" className="link-btn" onClick={copyLink}>
                {copied ? "Link copied" : "Copy link"}
              </button>
            </div>
          </>
        )}
      </article>
    </div>
  );
}

function suiteText(r: Receipt): string {
  const s = r.suite!;
  if (s.broken > 0) return `${s.broken} broken`;
  if (r.verdict?.verdict === "PROVEN") return `all ${s.basePassed} still pass`;
  return `${s.basePassed} of ${s.baseTotal} pass on base`;
}

function sentence(text: string): string {
  const t = text.trim();
  return t ? t[0].toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".") : t;
}

function Line(props: { label: string; value: ReactNode; note?: ReactNode; pending?: boolean; mono?: boolean; clamp?: boolean }) {
  return (
    <div className={`rline${props.pending ? " rline--pending" : ""}`}>
      <div className="rline__row">
        <span className="rline__label">{props.label}</span>
        <span className="rline__value">{props.value}</span>
      </div>
      {props.note && (
        <p className={`rline__note${props.mono ? " rline__note--mono" : ""}${props.clamp ? " rline__note--clamp" : ""}`}>{props.note}</p>
      )}
    </div>
  );
}

function Tiles({ tiles, mustPass, pending }: { tiles: Tile[]; mustPass: boolean; pending: boolean }) {
  const slots = pending ? Math.max(3, tiles.length) : tiles.length;
  return (
    <span className="tiles">
      {Array.from({ length: slots }, (_, i) => {
        const t = tiles[i];
        if (!t) return <span key={i} className="tile tile--pending" role="img" aria-label={`Run ${i + 1}: pending`} />;
        const asRequired = t.passed === mustPass;
        const Icon = t.passed ? Check : X;
        return (
          <span
            key={i}
            className={`tile ${asRequired ? "tile--expected" : "tile--unexpected"}`}
            role="img"
            aria-label={`Run ${i + 1}: ${t.passed ? "passed" : "failed"}, ${asRequired ? "as required" : "not as required"}`}
          >
            <Icon size={15} strokeWidth={2.75} aria-hidden="true" />
          </span>
        );
      })}
    </span>
  );
}

function Cursor() {
  return <span className="cursor" aria-hidden="true" />;
}
