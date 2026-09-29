// Typed client for the Receipts API (receipts/server.py).

export type Verdict = "PROVEN" | "REFUTED" | "REGRESSION" | "UNPROVEN" | "NO_CHECKABLE_CLAIM";
export type PrKind = "gold" | "none" | "diff";
export type RunStatus = "queued" | "running" | "done" | "error";

export interface InstanceSummary {
  id: string;
  repo: string;
  difficulty: string;
  title: string;
}

export interface InstanceDetail {
  id: string;
  repo: string;
  problem_statement: string;
}

export interface RunSummary {
  run_id: string;
  instance_id: string;
  pr: string;
  status: RunStatus;
  verdict: Verdict | null;
  started_at: string;
}

export interface ReceiptEvent {
  type: string;
  data: Record<string, unknown>;
}

export interface TestOutcome {
  outcome: string;
  exc: string | null;
  msg: string;
}

export interface RunSummaryRaw {
  tests: number;
  not_passed: Record<string, TestOutcome>;
  output_tail: string;
}

export interface Evidence {
  run_id?: string;
  instance_id: string;
  repo?: string;
  pr?: string;
  started_at?: string;
  claim?: { kind: string; claim: string };
  writer?: {
    attempts: number;
    reason: string;
    docs_queries?: string[];
    test_code: string | null;
    scope_check?: string;
    tool_log?: { cmd: string; exit: number; output: string }[];
  };
  forks?: {
    base_with_test: RunSummaryRaw[];
    pr_with_test: RunSummaryRaw[] | string;
    // older runs stored a single summary instead of [full run, ...reruns]
    base_suite: RunSummaryRaw[] | RunSummaryRaw | null;
    pr_suite: RunSummaryRaw[] | RunSummaryRaw | null;
  };
  second_opinion?: { faithful: boolean; reason: string };
  verdict?: Verdict;
  reason?: string;
  seconds?: number;
  tokens?: Record<string, { total_tokens?: number }> | null;
  events?: ReceiptEvent[];
}

export interface RunResponse {
  status: RunStatus;
  evidence: Evidence;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  instances: () => fetch("/api/instances").then(json<InstanceSummary[]>),
  instance: (id: string) => fetch(`/api/instances/${enc(id)}`).then(json<InstanceDetail>),
  runs: () => fetch("/api/runs").then(json<RunSummary[]>),
  run: (id: string) => fetch(`/api/runs/${enc(id)}`).then(json<RunResponse>),
  start: (body: { instance_id: string; pr: PrKind; diff?: string }) =>
    fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<{ run_id: string }>),
};

const EVENT_TYPES = [
  "status",
  "claim",
  "env_ready",
  "writer_progress",
  "writer_submit",
  "test_accepted",
  "fork",
  "suite",
  "second_opinion",
  "verdict",
  "error",
  "done",
];

/**
 * Stream a run's events. The server replays the whole backlog on every (re)connect, so the list is
 * rebuilt from scratch on open: `onEvents` always receives the complete, de-duplicated sequence.
 */
export function subscribe(runId: string, onEvents: (events: ReceiptEvent[]) => void): () => void {
  const source = new EventSource(`/api/runs/${enc(runId)}/events`);
  let events: ReceiptEvent[] = [];
  source.onopen = () => {
    events = [];
  };
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (message) => {
      events = [...events, { type, data: JSON.parse((message as MessageEvent).data) }];
      onEvents(events);
      if (type === "done") source.close(); // otherwise EventSource reconnects forever
    });
  }
  return () => source.close();
}
