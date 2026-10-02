# Issue race (browse only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A public `/races` page: for each of the 40 evaluation issues, its five patches ranked, with Receipts'
verdict, the reader's answer and SWE-bench's label.

**Architecture:** `evaluation.races(rows, titles)` groups the report rows by issue; `scripts/eval_report.py` writes
`eval/races.json`; the frontend copies it to `src/races.json` and renders it statically.

**Tech Stack:** Python 3.12 + pytest; React + Vite + vitest.

**Spec:** `docs/superpowers/specs/2026-10-02-issue-race-design.md`

## Global Constraints

- No checks run: zero Token Factory spend. No backend API, engine or schema change.
- No em dashes in UI copy; verdicts always shown with `VerdictChip` (glyph and word).
- Re-running the report must keep the public dataset link already in `eval/results.json`.

## Review Focus

- The check that never finished (psf__requests-1724, one agent patch): shows "not run", counts as no answer.
- A reader answer missing for a row: no answer, not disagreement.
- Ties in ranking keep dataset order (real fix, empty, wrong, wrong, right).
- Phone width: tables scroll inside their wrappers; no page-level horizontal scroll.

---

### Task 1: `races()` and `eval/races.json`

**Files:** Modify `receipts/evaluation.py`, `scripts/eval_report.py`; Test `tests/test_evaluation.py`.

- [ ] Failing test: `E.races(rows, {"o__r-1": "Title"})` returns one issue per instance id in row order, five
  patches each with `run_id, kind, agent, name, fixed, verdict, reader, cost_usd, reused`; `name` is
  "the real fix" / "an empty patch" / the readable agent name (`AGENT_NAMES`, falling back to the raw name).
- [ ] Implement `AGENT_NAMES` and `races()`.
- [ ] `eval_report.py`: titles from `swebench.load_instance(iid).problem_statement` first line (120 chars);
  write `eval/races.json` from all 200 rows (the unfinished one has `verdict: null`); keep the old
  `links.dataset` when not sharing.
- [ ] Run the report (no `--share`), check `eval/races.json` has 40 issues x 5 patches, commit.

### Task 2: `/races` page

**Files:** Create `src/races.json` (copy), `src/site/RacesPage.tsx`; Modify `src/site/results.ts`,
`src/site/results.test.ts`, `src/App.tsx`, `src/site/SiteLayout.tsx`, `src/site/ResultsPage.tsx`.

- [ ] Failing tests: `rankPatches` (Proven, Unproven/null, Refuted/Regression; stable), `receiptsAgree`
  (Proven+fixed, Refuted/Regression+not fixed; Unproven and null are no answer), `readerAgrees`
  ("fixed"+fixed, "not_fixed"+not fixed), `totalCost`.
- [ ] Implement helpers; `RacesPage`: intro, one `sec-card` per issue with `id={instance_id}`, ranked table.
- [ ] Route `/races`, nav "Races" after "Results", footer link, Results page link.
- [ ] `tsc -b`, vitest, check in the preview at desktop and 375 px, commit.
- [ ] Merge both branches to main, push, check `/races` live.
