# Receipts MVP — Fix Engine on SWE-bench (Design)

Date: 2026-09-28. Source brief: Receipts Build Brief (claude.ai artifact).

## Goal

Given a SWE-bench Verified instance and a candidate patch ("the PR"), decide whether the patch
does what the issue claims, and emit evidence. MVP = fix engine, CLI only.

Done when: for one instance, the gold patch yields PROVEN and a no-op / known-wrong patch yields
REFUTED or UNPROVEN (never PROVEN).

## Stack

- Agent framework: Deepagents 0.7.x (`create_deep_agent`), test-writer only.
- Inference: Nebius Token Factory, OpenAI-compatible (`langchain_openai.ChatOpenAI`).
  - Classifier: `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`
  - Test writer: `nvidia/Nemotron-3_5-Lightning`
  - Judge (second opinion before Refuted): `nvidia/Nemotron-3-Ultra-550b-a55b`
- Execution: Token Factory Sandboxes via `contree-sdk` + `contree-client[httpx]`.
- Tracing: LangSmith via env vars + `@traceable` on pipeline steps.
- Web search: Tavily (`langchain_tavily.TavilySearch`), docs-only, code hosts excluded.
- Runtime: global Python 3.12, no venv. All config from `.env`.

## Architecture

Deterministic Python orchestrator. The only agentic component is the blind test writer.
Verdict rules are pure code; no model decides a verdict except the judge's veto (which can only
downgrade Refuted → Unproven).

```
receipts/receipts/
  config.py    settings from .env; model factory
  sandbox.py   Contree client; ContreeBackend(BaseSandbox); run_pytest() + JUnit parse
  swebench.py  load instance (HF datasets); resolve preloaded image; patch application
  writer.py    Deepagents blind test writer + tools (sandbox, docs_search, submit_test)
  verdict.py   pure verdict rules
  engine.py    orchestrator
  __main__.py  CLI: `smoke`, `run <instance_id> --patch gold|<file.diff>`
tests/test_verdict.py
runs/<instance>-<ts>.json   evidence
```

### ContreeBackend

Contree commands are stateless: each `run(shell=..., disposable=False)` returns a new snapshot.
The backend holds `current_image` and advances it after every `execute`, giving the agent a
stateful sandbox. `BaseSandbox` derives file tools from `execute`; we implement `execute`, `id`,
`upload_files`, `download_files`.

## Data flow

1. Load instance: `problem_statement`, `repo`, `base_commit`, `PASS_TO_PASS`. Never read
   `test_patch` or `FAIL_TO_PASS` (hidden ground truth). PR patch = gold `patch` or a file.
2. Classify (Nano, structured output): `fix | dependency | none`. Not `fix` → NO_CHECKABLE_CLAIM.
3. Write test (Lightning agent) against the clean base image. The agent never sees the patch.
   `submit_test()` runs pytest in a fresh fork from base with `--junitxml`; accepted only if the
   test case has a `<failure>` whose type/message is `AssertionError`. Errors, collection
   failures, skips, passes are rejected with the reason fed back. Cap: `MAX_TEST_ATTEMPTS`.
   No valid test → UNPROVEN.
4. Forks (asyncio, semaphore `SANDBOX_MAX_CONCURRENCY`), all from the clean base image:
   - base + test, `VERDICT_RUNS` times
   - base + patch + test, `VERDICT_RUNS` times
   - base + patch + `PASS_TO_PASS` suite once; failing tests rerun 2 more times, a failure counts
     only if it fails every time.
5. Verdict (pure):
   - base runs not all AssertionError failures → UNPROVEN
   - patch fails to apply → UNPROVEN
   - PR runs all pass, suite clean → PROVEN
   - PR runs all pass, consistent suite failures → REGRESSION
   - PR runs all fail with the same assertion signature as base → candidate REFUTED →
     judge (issue + test + base output, no patch): agrees → REFUTED, else UNPROVEN
   - anything else → UNPROVEN
6. Evidence JSON: verdict, reasons, test source, per-fork outcomes + truncated output, token
   usage per model, timings, Tavily queries.

## Error handling

Any exception or sandbox timeout → UNPROVEN with the error recorded. The pipeline never
escalates uncertainty to REFUTED (asymmetry rule).

## Testing

- `tests/test_verdict.py`: verdict rule table and JUnit parsing (pure, no network), test-first.
- `python -m receipts smoke`: sandbox echo, SWE image lookup for an instance, ping all 3 models.
- E2E: gold patch → PROVEN; no-op patch → not PROVEN.

## Out of scope (MVP)

Dollar cost (tokens recorded only), GitHub App, web UI, dependency engine, eval harness,
LICENSE file.

## Open items resolved at smoke test

- Exact `CONTREE_BASE_URL` (`/sandboxes` vs `/sandboxes/v1`).
- Preloaded SWE-bench image tag scheme, repo path (expected `/testbed`), and python/conda
  activation inside the image.
