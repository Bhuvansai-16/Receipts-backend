# Receipts Web UI (Design)

Date: 2026-09-28. Product context: `PRODUCT.md`. Decisions made with the owner in brainstorming; the
owner asked to build directly after the layout/palette choices (no further mockups or review gates).

## Decisions

- **Stack:** FastAPI JSON API + SSE (`receipts/server.py`), Vite + React + TypeScript SPA (`web/`).
  FastAPI serves the built SPA (`web/dist`) so a demo is one command: `python -m receipts serve`.
- **Form input:** SWE-bench Verified instance (pytest repos) + PR choice: real fix / do-nothing PR / pasted diff.
- **Live view:** stage timeline + per-run tiles, rendered as the receipt "printing" line by line.
- **Evidence layout:** A, "The Receipt": one centered receipt card, verdict as the total; details
  (blind test, each run's output, existing tests, second opinion, writer log) in disclosures below.
- **Form layout:** B, form + recent receipts side by side (stacks on narrow screens).
- **Visual direction:** clay.com-like. Pure white `#FFFFFF`, near-black ink, no blue anywhere. Figtree
  (UI) + Geist Mono (receipt lines, code), self-hosted. Color only for meaning: verdict tints
  (PROVEN green, REFUTED red, REGRESSION rust, UNPROVEN grey) plus "honey moments"
  (`oklch(0.774 0.174 65.1)`) on the brand dot, the live printing cursor and (darker honey) focus rings.
- **Accessibility:** WCAG 2.2 AA; verdicts always glyph + word; aria-live for printing lines;
  printing animation off under prefers-reduced-motion.

## API

| Endpoint | Behavior |
|---|---|
| `GET /api/instances` | pytest-repo SWE-bench Verified list: `id, repo, difficulty, title` |
| `GET /api/instances/{id}` | `{id, repo, problem_statement}`; 404 unknown |
| `POST /api/runs` `{instance_id, pr: "gold"\|"none"\|"diff", diff?}` | 202 `{run_id}`; 404 unknown instance, 422 bad input, 413 diff > 200 KB |
| `GET /api/runs` | newest first: `run_id, instance_id, pr, status, verdict, started_at` (disk + in-memory) |
| `GET /api/runs/{id}` | `{status, evidence}`; evidence partial while running; 404 unknown |
| `GET /api/runs/{id}/events` | SSE: replays events so far, then live; ends after `done` |

Run ids equal the evidence filename stem (`<instance>-<pr>-<YYYYmmdd-HHMMSS>`). At most 2 checks
run at once; others wait with status `queued`.

## Events (engine `emit(type, data)`)

`claim {kind, claim}` · `env_ready {}` · `writer_submit {attempt, accepted, reason}` ·
`test_accepted {attempts}` · `fork {side: base|pr, n, passed, message}` ·
`suite {base_passed, base_total, pr_failed}` · `second_opinion {faithful, reason}` ·
`verdict {verdict, reason, seconds, tokens}` · `done {}`. Events are also stored in
`evidence["events"]`. `emit` defaults to a no-op; the CLI is unchanged.

## Testing

Engine: events fire in order (fake sandbox). Server: TestClient with a fake `check` (start run,
replay SSE, fetch evidence, validation errors). Front end: `npm run build` passes; verified in
the browser pane against real runs, including one live check.
