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

## Models

| Step | Model |
|---|---|
| Claim classification | Nemotron 3 Nano |
| Blind test writing (Deepagents agent in sandbox) | Nemotron 3.5 Lightning |
| Second opinion before REFUTED | Nemotron 3 Ultra |
