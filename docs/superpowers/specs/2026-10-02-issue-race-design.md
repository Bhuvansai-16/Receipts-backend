# Issue race: one blind test, many patches, a ranked table: design

Date: 2 October 2026. Sub-project 3 of the Phase 2 plan (after judge-ready repositories and the evaluation;
before public pull requests by URL and the MCP server). Status: approved in conversation, waiting for review of
this document.

## Goal

For one issue, show which competing patches really fix it: Receipts writes the blind test once, runs it against
every patch, and ranks them. The race is SWE-bench first: the 40 evaluation issues, each with its real fix, an
empty patch and three published coding agents' patches (Agentless, SWE-agent, OpenHands, Nebius's own agent and
others), with SWE-bench's hidden-test answer next to Receipts' verdict.

It shows the claim the per-check pages can't: one test, many verdicts, almost no extra cost, because every check
after the first reuses the blind test.

### Success criteria

1. Anyone, signed in or not, can browse the 40 finished races from the evaluation, with no spend.
2. A signed-in user can re-run a race live and watch the five checks finish one after another, the first writing
   the blind test and the other four reusing it.
3. Each race page says how often Receipts agreed with SWE-bench's hidden tests, what it cost and how long it took.
4. Verifying the feature on production spends about $0.05 of Token Factory credit: one live race, nothing else.

### Out of scope

Races of real GitHub issues (they come with sub-project 4, public pull requests by URL), issues outside the 40,
live races without signing in, changes to the engine, the verdict rules or the database schema.

## What users see

- **`/races`** (public; linked from the Results page and the site nav): the 40 issues. Each row: issue title,
  repository, and "Receipts agreed with SWE-bench on N of 5".
- **`/races/<instance_id>`** (public): the ranked table of the issue's five patches.
  - Columns: patch (the real fix, an empty patch, or the agent's name), Receipts' verdict, SWE-bench's answer
    (fixes it or not), whether they agree, cost, time, "reused the blind test", and a link to the receipt.
  - Order: Proven first, then Unproven, then Refuted and Regression; ties keep the dataset order.
  - Below the table: the total cost, and how many checks reused the blind test.
  - "Agree" means Proven on a patch SWE-bench says fixes the issue, or Refuted or Regression on one it says
    doesn't. Unproven is counted as no answer, never as agreement or disagreement.
  - A **Run this race live** button. Signed out, it reads "Sign in to run this race". Signed in, it starts the
    race (it needs no check of yours running and 5 of your daily checks left), then shows the live view.
- **Live view**: the same page with `?runs=<five run ids>`. The rows come from those runs, polled every 4 s until
  all five have finished; each row shows queued, running or its verdict. Receipts are public by link, so the
  live view is shareable like any receipt.
- A refused start (limits) shows the reason next to the button.

## Backend

### Race data: `receipts/races.json`

Committed, built once by `scripts/race_data.py` from `eval/dataset.json`, `eval/results.json` and the baseline
receipts in `runs/eval/receipts-b30c80b0/`. Per issue: `instance_id`, `repo`, `title` (the issue's first line),
and five patches, each with `kind` (gold, none, right, wrong), `agent` (full submission name), `label` (a short
agent name for run ids and the table, such as `agentless`, `sweagent`, `openhands`, `nebius`), `fixed` (SWE-bench's
answer), `patch_url`, `sha256`, and the baseline receipt's `run_id`, `verdict`, `cost_usd`, `seconds` and
`reused`. The frontend gets a copy as `src/races.json`, like `eval-results.json`.

The one baseline check that never finished (psf__requests-1724 with an agent patch) shows as "not run" in its race.

### `POST /api/races` `{instance_id}` (signed in)

1. 404 if the issue is not one of the 40.
2. 429 with the reason if the user has a check running, fewer than 5 of their daily checks left, or fewer than 5
   left in the global daily cap.
3. Creates five runs as queued (run ids from `engine.new_run_id(instance_id, label)`, so each names its patch),
   and returns `{"run_ids": [...]}` in `races.json` order (real fix, empty patch, the right agent patch, the two
   wrong ones).
4. One background task runs the five checks **in order** through the same `checks._execute` path as every check,
   sharing the server's two sandbox slots. `prepare()` for an agent patch downloads it from SWE-bench's public
   bucket and compares its sha256 with `races.json`; a download failure or a mismatch fails that check with the
   reason (Unproven), and the race goes on. The real fix and the empty patch come from SWE-bench, as in the demo.

### Reuse

The checks use the production blind-test store (`tests=runs`), keyed by repository, base and issue text. The first
check of a race writes the test; the next four reuse it, and so does every later race of the same issue, by anyone.
Each row's `reused` flag shows it.

### Stop

Each of the race's live runs shares the race task, so pressing Stop on any of them stops the race: the running
check ends as "Stopped before it finished." and every queued one is finished as an error, "Stopped before it
started.", so nothing stays queued. A server restart already marks queued and running runs as errors.

### Unchanged

The engine, the verdict rules, the database schema (no migration) and every existing endpoint.

## Frontend

- `src/races.json` (copied), `src/site/RacesPage.tsx` (the list), `src/site/RacePage.tsx` (the table, both data
  sources), `src/site/races.ts` (ranking, agreement, totals) with `races.test.ts`.
- Routes `/races` and `/races/:instanceId` in the public site; "Races" in the nav after "Results"; a link from the
  Results page.
- The live view polls `GET /api/runs/{id}` (public) for each run until its status is done or error.
- Same styles as the Results and Security pages (`doc-page`, `sec-card`, `table-wrap`, `perm-table`), tables
  scrolling inside their wrappers at phone width.

## Testing

No model or sandbox calls in tests.

- Backend, with a fake `engine.check` and the in-memory run store:
  - an unknown issue is 404; a running check or too few daily checks left is 429;
  - a race creates five queued runs and returns their ids in order;
  - the five checks run strictly one after another and share the store;
  - Stop during the second check leaves it "Stopped before it finished." and the three queued runs "Stopped before
    it started.";
  - an agent patch whose sha256 doesn't match fails that check with the reason and the race continues.
- `race_data.py`: the 40 issues, five patches each, labels unique within an issue.
- Frontend: ranking order (Proven, Unproven, Refuted or Regression, then dataset order), the agreement count, the
  totals.

## Spend

Browsing costs nothing. Tests use fakes. Verification on production is one live race of one issue: about $0.03
for the first check (writes the blind test) and about $0.005 for each of the other four. Nothing else is run.
