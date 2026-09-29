# Receipts

Checks whether a pull request does what it claims: writes the missing test blind (from the issue only),
runs it in forked Nebius Token Factory sandboxes, and reports the evidence.

MVP: fix engine on SWE-bench Verified, CLI only.

## Setup (global Python, no venv)

```bash
pip install contree-sdk "contree-client[httpx]" deepagents langchain-openai langchain-tavily langsmith datasets python-dotenv
cp .env.example .env   # fill in keys
python -m receipts smoke --instance psf__requests-1142
```

## Web UI

Pick an issue, choose the PR (the real fix, a do-nothing PR, or your own diff) and watch the receipt print
live; finished receipts show the blind test, every run's output and the second opinion. Needs Node 20+ once
to build the UI.

```bash
cd web && npm install && npm run build   # once
python -m receipts serve                  # http://127.0.0.1:8000
```

UI development: run `python -m receipts serve` and, in `web/`, `npm run dev` (Vite on :5173, proxying
`/api` to :8000). `npm test` runs the front-end tests.

## Sandbox provider

`SANDBOX_PROVIDER=contree` (default) runs on Nebius Token Factory Sandboxes, which the submission uses.
`SANDBOX_PROVIDER=daytona` is a stopgap while Sandboxes beta access is pending. It builds one Daytona snapshot
per SWE-bench instance on first use; each run starts a fresh sandbox from it and replays recorded steps
(patch apply, `.git` removal) in place of Contree's fork.

## Run

```bash
python -m receipts run psf__requests-1142 --patch gold   # real fix, expect PROVEN
python -m receipts run psf__requests-1142 --patch none   # PR that changes nothing, expect REFUTED/UNPROVEN
python -m receipts run psf__requests-1142 --patch my.diff
```

Evidence JSON lands in `runs/`. Traces in LangSmith project `receipts`.

## Verdicts

| Verdict | Meaning |
|---|---|
| PROVEN | Blind test fails on base with AssertionError (3/3), passes on PR (3/3), existing tests that passed on base still pass |
| REGRESSION | Test passes on PR, but existing tests that passed on base now fail consistently |
| REFUTED | Test fails on PR with the same assertion as base (3/3) and Nemotron Ultra confirms the test matches the issue |
| UNPROVEN | Anything else. Explicitly not evidence against the PR |
| NO_CHECKABLE_CLAIM | Not a bug-fix claim |

## Blindness: what is and isn't guaranteed

- The test writer never receives the patch, SWE-bench's hidden tests (`test_patch`, `FAIL_TO_PASS`) or hints.
- It works in its own sandbox copy with `.git` removed; every verdict run forks the untouched base image.
- Tavily is limited to documentation sites, with code hosts excluded.
- Residual risk: the sandbox has network access and docs sites can show newer source. Every shell command
  and search the agent ran is recorded in the evidence JSON (`writer.tool_log`, `writer.docs_queries`) so a
  leak can be audited.

## Models

| Step | Model |
|---|---|
| Claim classification | Nemotron 3 Nano |
| Blind test writing (Deepagents agent in sandbox) | Nemotron 3.5 Lightning |
| Scope check of each submitted test (only what the issue asks) | Nemotron 3 Super |
| Second opinion before REFUTED | Nemotron 3 Ultra |
