# Evaluation against SWE-bench's answer key, run in LangSmith: design

Date: 2 October 2026. Sub-project 2 of the Phase 2 plan (after judge-ready repositories; before the issue race,
public pull requests by URL and the MCP server). Status: approved in conversation, waiting for review of this
document.

## Goal

Real numbers for how often Receipts is right, measured against an answer key Receipts never sees, next to an AI
reviewer that only reads the diff, run as LangSmith experiments so every row has its trace; then use those
experiments to improve the agent, and publish everything: a public LangSmith dataset link, `eval/RESULTS.md`, a
`/results` page and a public receipt behind every row.

### Why this answer key

SWE-bench Verified ships, for every issue, hidden tests (FAIL_TO_PASS and PASS_TO_PASS) that decide whether a
patch resolves it. Leaderboard submissions publish each agent's patch per issue and which issues it resolved. A
patch the hidden tests reject is a wrong patch; many pass the repository's existing tests, which is the "green CI
that isn't a fix" case Receipts exists for. Receipts never sees the hidden tests; they are only the labels.

### Why LangSmith

Receipts already traces every check to LangSmith (project `receipts`) and has no datasets or experiments yet. A
LangSmith dataset plus `Client.aevaluate` runs the real pipeline over every row, scores each row with our own
evaluators, keeps each check's full trace (every model call and agent step) attached to its row, compares
experiments row by row, and can be shared publicly (examples, experiments, runs and feedback, no account
needed). Cost: LangSmith bills traces, about 200 per experiment, well within the $100 credit; models stay on
Token Factory.

### Success criteria

1. A dataset of about 40 issues and about 200 labelled patches, reproducible from `eval/dataset.json` and
   uploaded to LangSmith as `receipts-swebench-verified`.
2. A baseline experiment of the real pipeline over every row, with test reuse inside each issue, for about $2 to
   3 of Token Factory credit; a reader experiment on the same rows.
3. Scores per row and per experiment: catch rate, false Proven, false Refuted, Unproven, cost and time; the
   reader's accept and reject rates; every miss reachable with its trace.
4. Up to two improvement experiments chosen from the baseline's misses; a change is kept only if it improves the
   targeted metric and false Refuted does not rise.
5. Published: the dataset shared publicly (after checking traces carry no secrets), `eval/RESULTS.md`,
   `/results` on the site, each eval receipt opening on the site, README links.

### Not in this sub-project

The issue race (sub-project 3, which will reuse this dataset), non-pytest repositories (django, sympy), and
online evaluation of live checks.

## 1. The dataset (`scripts/eval_dataset.py` -> `eval/dataset.json` -> LangSmith)

**Issues.** SWE-bench Verified rows in `swebench.PYTEST_REPOS`.

**Agent patches.** About 8 submissions under `evaluation/verified/` in github.com/SWE-bench/experiments whose
`metadata.yaml` has a `logs` asset, chosen for a spread of strength (resolve rates from about 30 % to about
60 %). Resolved ids come from `results/results.json` (`resolved`) or `per_instance_details.json`
(`resolved: true`); a submission's patch for an issue is
`https://swe-bench-submissions.s3.amazonaws.com/verified/<submission>/logs/<instance>/patch.diff`. A patch counts
only if it downloads, is not empty, and is at most 200 KB (the app's own limit).

**Selection.** Issues where at least one sampled agent resolved it and at least two didn't, at most 8 per
repository, in a fixed order (sorted by instance id) until there are 40.

**Cases per issue (5):** the real fix (`gold`, fixed), an empty patch (`none`, not fixed), two wrong agent
patches from different agents (not fixed), one right agent patch (fixed).

**Committed:** `eval/dataset.json` with, per case, instance id, kind, agent, label, patch URL and sha256.
Patches are cached in `.cache/eval-patches/` (git-ignored) and checked against the sha256.

**LangSmith:** one example per case. Inputs: `instance_id`, `kind`, `agent`, `patch_url`, `patch_sha256`.
Reference outputs: `fixed` (bool). Metadata: `repo`. Re-uploading replaces the dataset's examples.

## 2. The baseline experiment (`scripts/eval_run.py`)

`await Client().aevaluate(target, data="receipts-swebench-verified", evaluators=[...],
summary_evaluators=[...], max_concurrency=4, experiment_prefix="receipts", metadata={models, git commit})`.

- **Target.** Loads the instance and the cached patch, then `engine.check(inst, patch, tests=store,
  run_id=...)` with one in-memory store for the whole experiment. A lock per instance id makes the first case of
  each issue (whatever its kind) write the blind test while the issue's other cases wait, then reuse it: the
  test never depends on the patch. Returns the verdict, reason, seconds, tokens, model cost at list prices,
  whether the test was reused, and the run id. The receipt is also saved to `runs/eval/<experiment>/<run id>.json`.
- **Row evaluators** (feedback keys): `caught` (not fixed and REFUTED or REGRESSION), `false_proven` (not fixed
  and PROVEN), `proven_fix` (fixed and PROVEN), `false_refuted` (fixed and REFUTED), `unproven` (UNPROVEN or
  NO_CHECKABLE_CLAIM), `cost_usd`, `seconds`, `reused`. Each row gets only the keys that apply to its truth
  class, so LangSmith's averages are the rates.
- **Summary evaluators:** `catch_rate`, `false_proven_rate`, `false_refuted_rate`, `proven_rate_on_fixes`,
  `unproven_rate`, `median_cost_usd`, `median_seconds`.
- The check's existing `receipts_check` trace is the row's run, so each row opens its full trace.

## 3. The reader experiment

Same dataset, `experiment_prefix="reader-ultra"`: the target gives Nemotron Ultra (the second-opinion model)
the issue and the diff (truncated to 30,000 characters) and asks for `fixed`, `not_fixed` or `unsure` with a
reason, as structured output. Evaluators: `accepted_fix` (fixed and `fixed`), `false_accept` (not fixed and
`fixed`), `rejected_wrong` (not fixed and `not_fixed`), `false_reject` (fixed and `not_fixed`), `unsure`, plus the
same summary rates. LangSmith's comparison view shows both experiments row by row.

## 4. Improvement loop

1. In LangSmith, filter the baseline's rows by `false_proven`, `false_refuted` and `unproven`, read their traces,
   and group the misses by cause (for example: no valid test written, a test the gates rejected wrongly, an
   environment that didn't build, a library pitfall a skill could cover).
2. Pick at most two changes, each aimed at the largest group and small enough to review (a skill, a prompt line,
   a gate's wording, a budget value). Each is written up in `eval/RESULTS.md` with its reason.
3. Run each as a new experiment (`experiment_prefix="receipts-<change>"`) on the same dataset.
4. Keep a change only if the targeted rate improves and `false_refuted_rate` doesn't rise; otherwise revert it
   and record the result. The verdict rules themselves are never changed to make numbers better.

## 5. Report and publishing

- `scripts/eval_report.py` reads the experiments from LangSmith (`list_runs` with feedback) or the saved receipts
  and writes `eval/RESULTS.md` and `eval/results.json`: headline rates for Receipts and the reader, the
  per-repository table, every false Proven, false Refuted and reader false accept with links (receipt and
  LangSmith trace), cost and time, and the improvement experiments with before and after.
- `python -m receipts import-eval runs/eval/<experiment>` loads the kept experiment's receipts into Neon under
  owner `eval`, which no gallery lists; each opens at `/runs/<id>` like any receipt.
- Before sharing, a script scans a sample of the experiment's traces for anything secret (API keys, tokens,
  `.env` values, private repository names); then `Client.share_dataset` makes the dataset and its experiments
  public, and the link goes into the README and `/results`.
- `/results` in the frontend renders `results.json` (copied into the frontend): headline numbers, Receipts next
  to the reader, the per-repository table, the misses with links, and the public LangSmith link.

## Error handling

A patch download that fails or doesn't match its sha256 is left out of selection (the dataset records what was
used). A pipeline error is a receipt like in production (Unproven, never Refuted). `aevaluate` runs with
`error_handling="log"`. To resume a stopped experiment, the harness passes its name to `experiment=` and
only the examples that have no run in it yet as `data=`, so finished rows are not paid for twice. The reader's failures count as `unsure`.

## Testing

- Unit tests: dataset selection from fixture submissions (per-issue rules, per-repository cap, size limit,
  labels); every row and summary evaluator on fixture outputs for both truth classes; the per-instance lock
  (the second case of an issue waits and reuses); the reader's output parsing; `import-eval` stores rows with
  owner `eval` and no gallery lists them; the secret scan flags a planted key; the results page renders from a
  fixture.
- Live: one issue end to end (five cases) as a small experiment before the full run; then the baseline, the
  reader, the improvements, the report, the import, the share, and the page on the live site.
