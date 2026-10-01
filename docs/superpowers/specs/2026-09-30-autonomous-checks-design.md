# Autonomous checks: research brief, self-recovery, richer GitHub checks, cost guard

Date: 2026-09-30. Status: approved scope, awaiting spec review.

## Why

An audit of all 46 stored runs (and the five sympy PRs checked today) showed:

- Most Unproven verdicts come from the **test writer**, not from the pull request: it ran out of budget,
  looped on one command (15 identical calls in #15), put code at module level (#14, #15, #26, #30), wrote a
  test that fails on any code (#13), or drifted into fixing the library it was meant to test (#14).
- The grading harness itself is correct: a known-failing test on PR #15's base is recorded as a failed
  AssertionError.
- Tavily is wired in as a `docs_search` tool the writer may call, and was called **0 times in 40 runs**.
  The docs allowlist does not even include docs.sympy.org.

Seven targeted fixes already shipped (commits 27d8ace through the exception-hint fix): no attempt spent on a
missing file, repeated identical calls short-circuited, import-time errors explained, passing tests count as
coverage, tests that would fail after a correct fix rejected by the judge model, every submission kept in the
evidence, and a try/except hint when the test raises the issue's own exception. What remains is that the
small writer model (Nemotron Lightning) is the bottleneck, and nothing acts on its failure.

## Goals

1. Fewer Unproven verdicts caused by the writer, without weakening what any verdict means.
2. Tavily does work that shows up in the evidence, for a purpose the check needs.
3. A check started by GitHub finishes and explains itself on the pull request with nobody watching.
4. Spend stays bounded, now that the GitHub App is public.

## Non-goals (decided)

- **No PR comments.** They need write permission beyond "reads, never pushes" and repeat the check run.
- **Auto-check stays opt-in per repository.** On by default, strangers' PRs would spend the credits.
- **No dependency-claim checks for now.** Release notes are read, not run; they would dilute what PROVEN
  means. Revisit after submission.
- **No retries of fork-based verdicts.** Re-running forks until one looks better would be p-hacking.
- **No change to the verdict rules** in `verdict.py`.

## Integrity rules (unchanged, restated because every component touches them)

- **Blind:** the researcher and the writer see the issue and the unpatched repository only, never the
  patch. Research queries are built from the issue text; code hosts stay excluded.
- **Asymmetry:** anything uncertain is Unproven.
- **Recovery is not a second verdict:** it only happens when no test was accepted, so it runs before any
  pull request fork exists. At most one recovery per check.

## Components

### 1. Research brief (`receipts/research.py`, new)

A fixed first step, not an optional tool.

- **Input:** issue text, repository name. **Output:** `Brief(queries, sources[{title, url}], notes)`.
- **Queries (deterministic, no model):** the library name (from the repository, e.g. `sympy`) plus up to two
  names from the issue's code: identifiers that are called or passed (`parse_expr`, `refine`,
  `default_sort_key`), excluding Python builtins and names shorter than four characters, longest first.
- **Search:** Tavily, `include_domains` = the library's docs site (a small map: sympy to docs.sympy.org,
  plus the existing `DOC_DOMAINS`), `exclude_domains` = `CODE_HOSTS`, 3 results per query.
- **Notes:** the result snippets, trimmed to about 1,200 characters in total, with their URLs.
- **Use:** added to the writer's first message under "Docs for the APIs in the issue (usage only; the issue
  decides what is correct)". Stored as `evidence.research`.
- **Failure:** any Tavily error gives an empty brief. Research never blocks or fails a check.
- The optional `docs_search` tool is removed: the fixed step replaces it.

### 2. Self-recovery (`engine.py`, `writer.py`)

- **Trigger:** `write_test` returns no accepted test, and the cause is the writer (not an environment
  error, not a user Stop).
- **Action:** one more `write_test` in a fresh sandbox workspace, with:
  - the research brief,
  - the first attempt's rejections (reasons, and the last rejected file),
  - the **stronger writer model**, `MODEL_TEST_WRITER_STRONG` (default `nvidia/nemotron-3-super-120b-a12b`),
    **only if the A/B below shows it helps**; otherwise the same model.
- **Events:** `writer_retry {model, why}`. Evidence keeps the first attempt under `writer_first`.
- **A/B gate before enabling the stronger model:** run the five failing sympy PRs (#13, #14, #15, #16, #26)
  with each writer model once. Adopt the stronger model for recovery if it gets an accepted test in at least
  three of five and the token cost stays under 1M per check.

### 3. Richer GitHub check runs (`github_app.finish_check`)

- The check run's output gets a plain-language summary: the verdict and what it means, the claim, the runs
  on each side (fails 3 of 3 before, passes 3 of 3 after), the existing tests, and for a mixed result the
  case that still fails with its message.
- The output text carries the blind test (first 60 lines) in a code block and the link to the full receipt.
- Uses the existing `checks: write` permission only.

### 4. Cost guard (`checks.py`, `config.py`)

- `GLOBAL_RUNS_PER_DAY` (default 60): all checks by all users in the last 24 hours; over it, starts are
  refused with a clear message (429) and webhook auto-checks are skipped and logged.
- `ALLOWED_GITHUB_ACCOUNTS` (optional, comma separated): when set, webhook auto-checks run only for
  installations on those accounts. Manual checks are governed by the per-user limits as today.
- A recovery counts as part of its check, capped at one.

### 5. Frontend

- The "Write the blind test" step says "Retrying with a stronger model" after a `writer_retry` event.
- Evidence details gain "Docs consulted" (links) and "Attempts" (each submission's reason, with its file
  behind a disclosure).
- The explanation panel mentions a recovery when one happened.

## Testing

- Unit tests per component with fakes for Tavily, the models and the sandbox (the existing style).
- Live evaluation with the eval script: the five failing PRs plus two known-good ones (sympy #30, xarray
  4629) to catch regressions. Report Unproven count, accepted tests, tokens and time before and after.
  About 12 checks of credit in total, including the A/B.

## Build order

1. Cost guard (protects everything that follows).
2. Research brief.
3. A/B of the writer model, then self-recovery.
4. Check-run output.
5. Frontend.

## Results (2026-10-01)

Writer-model A/B on sympy #13, #14, #15, #16, #26 (`scripts/eval_prs.py`, pipeline with fixes 1-7 and the research
brief, before self-recovery). Prices from the Token Factory catalog (input / output per 1M tokens).

| Writer | Valid test | PROVEN | Tokens (5 checks) | Worst check | Price | Model spend |
|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | 0 of 5 | 0 | 1.73M | 971K | $0.06 / $0.24 | about $0.11 |
| Nemotron 3 Super | 4 of 5 | 3 (#13, #14, #26) | 0.78M | 549K | $0.30 / $0.90 | about $0.25 |

Decisions:

- Super passes the gate (at least 3 of 5, under 1M tokens) and is the retry model.
- **Ruling beyond the spec: Super is also the first writer.** Lightning wrote no valid test and spent more tokens
  failing than Super spent succeeding; about 6 cents per valid test with Super. `MODEL_TEST_WRITER` switches back.
- Ultra as writer, probed once on #16 (where Super failed): Ultra wrote no valid test either; the self-recovery
  retry on Super, given Ultra's failure, wrote one first time. The retry's value is the history and a fresh
  attempt, not a bigger model, so `MODEL_TEST_WRITER_STRONG` stays Super ($0.30 vs Ultra's $1.00 per 1M input).
