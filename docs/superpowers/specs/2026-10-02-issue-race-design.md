# Issue race: one blind test, many patches, a ranked table: design

Date: 2 October 2026. Sub-project 3 of the Phase 2 plan (after judge-ready repositories and the evaluation;
before public pull requests by URL and the MCP server). Status: approved in conversation, then cut to browsing
only ("do this if it's really useful"): the live re-run is dropped, see Out of scope.

## Goal

For one issue, show which competing patches really fix it. The race is the 40 evaluation issues, each with its
real fix, an empty patch and three published coding agents' patches (Agentless, SWE-agent, OpenHands, Nebius's
own agent and others). Per patch: Receipts' verdict, what a model reading only the diff said, and SWE-bench's
hidden-test answer. It shows the evaluation's numbers as concrete cases, and that the five checks of an issue
share one blind test.

### Success criteria

1. Anyone, signed in or not, can browse the 40 races, with no spend and no backend change.
2. Each issue says how often Receipts and the reader agreed with SWE-bench, what the five checks cost and how many
   reused the blind test; every row links to its receipt.

### Out of scope

- The live re-run (a signed-in user racing an issue again). It needs a new endpoint, a sequential job, stop
  handling and spend, to show blind-test reuse that every receipt already shows ("reused" on the receipt).
- Races of real GitHub issues with competing pull requests: they come with sub-project 4 (public pull requests by
  URL), where the race is genuinely useful to maintainers.
- Changes to the engine, the verdict rules, the backend API or the database.

## What users see

**`/races`** (public; in the nav after "Results"; linked from the Results page): a short intro, then one section per
issue, sorted by repository then issue id, each with an anchor (`/races#pydata__xarray-4629`).

- Section heading: the issue id and title (its first line), the repository, and "Receipts agreed with SWE-bench on
  N of 5, the reader on M of 5".
- A table of its five patches, ranked: Proven first, then Unproven, then Refuted and Regression; ties keep the
  dataset order (real fix, empty patch, the two wrong agent patches, the right one).
  - Columns: patch (the real fix, an empty patch, or the agent's readable name), Receipts (verdict, linking to the
    receipt), SWE-bench (fixes it or not), the reader (fixed, not fixed or unsure), cost, "reused the blind test".
- Under the table: the five checks' total cost.
- "Agree" for Receipts: Proven on a patch SWE-bench says fixes the issue, or Refuted or Regression on one it says
  doesn't. Unproven counts as no answer, never as agreement or disagreement. For the reader: "fixed" on a real fix
  or "not fixed" on a wrong one; "unsure" is no answer.
- The one check that never finished (psf__requests-1724 with an agent patch) shows "not run".

## Data

`scripts/eval_report.py` also writes `eval/races.json` from the rows it already builds (dataset labels, baseline
receipts, the reader's answers): per issue `instance_id`, `repo`, `title`, and five patches with `run_id`, `kind`,
`agent`, `name` (readable agent name), `fixed`, `verdict`, `reader`, `cost_usd`, `reused`. No model or sandbox calls:
it reads the receipts on disk and the reader experiment from LangSmith. The frontend gets a copy as
`src/races.json`, like `eval-results.json`.

## Frontend

`src/races.json`, `src/site/RacesPage.tsx`, ranking and agreement helpers in `src/site/results.ts` with tests;
route `/races`; "Races" in the nav and footer; a link from the Results page. Same styles as the Results page
(`doc-page`, `sec-card`, `table-wrap`, `perm-table`), tables scrolling inside their wrappers at phone width.

## Testing

- Backend: `races()` groups rows by issue in dataset order and gives readable agent names; tested with fake rows.
- Frontend: ranking order, the Receipts and reader agreement counts, the total cost.

## Spend

None: no checks run.
