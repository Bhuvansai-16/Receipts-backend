# Judge-ready repositories: design

Date: 2 October 2026. Sub-project 1 of the Phase 2 plan (then: evaluation, issue race, public PRs by URL, MCP
server). Status: approved in conversation, waiting for review of this document.

## Goal

A judge who only reads the repository understands what Receipts does, why it matters, how it uses NVIDIA
Nemotron on Nebius Token Factory, and can try it in minutes; and the live demo is awake when they click it.

### What judges score

From the official rules (nebiusglobalaihackathon.devpost.com/rules): a pass/fail check that the project fits its
track ("agents that write, run, and test code in Token Factory" for Coding and Agentic Engineering), then four
equally weighted criteria: Technological Implementation, Design, Potential Impact, Quality of the Idea. Judges
may score from the text, images and video alone, without running anything. Submissions close 30 October 2026,
10:00 PT; judging runs 1 to 15 December 2026. A separate $3,000 prize goes to the best use of Tavily (a runtime
call to the Tavily API qualifies).

### What a judge sees today

The backend repository has no description, topics or homepage; its README opens with "MVP: fix engine on
SWE-bench Verified", names the Nemotron models near the end, and has no demo link, screenshot, diagram, numbers
or test badge. Running it locally asks for about eight keys, and nothing says which two are enough. The frontend
repository has a homepage link and developer notes.

### Success criteria

1. The backend README's first screen has the pitch, the live demo link, the test badge and a receipt screenshot.
2. The README names each Nemotron model with its role and why it was chosen, says where Token Factory and its
   Sandboxes helped with measured numbers, and shows the other services, Tavily included.
3. "Run it yourself" works with only `NEBIUS_API_KEY` and `CONTREE_PROJECT` set: verified by one real check.
4. Both repositories run their tests in GitHub Actions on every push, green, with a badge.
5. A receipt shows how many documentation pages the Tavily search brought in.
6. The user has the exact text for repository descriptions, topics and homepages, and steps for an UptimeRobot
   monitor on `/api/health`.

### Not in this sub-project

Evaluation numbers (sub-project 2 adds them to the README), the demo video, and the submission text.

## 1. Backend README

Sections, in order:

1. **Receipts**: one sentence ("Proof that a pull request does what it claims: Receipts writes the missing
   test from the issue alone, runs it before and after the change in Nebius Token Factory Sandboxes, and
   reports the evidence."), badges (Apache-2.0, tests, live demo), links (live demo with no sign-in, the app,
   the frontend repository, the video once it exists).
2. **Screenshot**: a finished receipt from the live site (`docs/images/receipt.png`), plus the live demo page.
3. **Why**: a green CI check says nothing old broke, not that the bug is gone; many pull requests are now
   written by AI agents; Receipts gives maintainers executed evidence in minutes.
4. **How a check works**: a Mermaid flowchart: claim (Nemotron Nano, three votes) and environment in parallel;
   research brief (Tavily); blind test writer (Nemotron Super, Deepagents middleware and skills, in a Token
   Factory Sandbox); gates on each submitted test (Super scope check, Ultra after-fix review); three runs on
   the base and three with the PR in forked sandboxes, plus the existing tests; deterministic verdict rules; a
   Nemotron Ultra second opinion before any Refuted; the receipt, and a check on the pull request.
5. **NVIDIA Nemotron on Nebius Token Factory**: a table of the three models (Nano, Super, Ultra) with role,
   why (the A/B on five real pull requests; Ultra only where a wrong call would accuse a contributor) and the
   tokens each uses in a typical check.
6. **Where Token Factory helped**: one API key for three Nemotron sizes and the Sandboxes; forked sandboxes
   run the six verdict runs at once (about 7 s); built environments are kept by tag across restarts; measured
   cost per check ($0.013 written, $0.004 reused, at list prices) from `scripts/usage_report.py`.
7. **Other services**: Token Factory Sandboxes, Tavily (library documentation for the APIs an issue names,
   fetched before the writer starts, with code hosts and source pages filtered out so the writer stays blind),
   Neon (Postgres and Auth), LangSmith (traces), Render and Vercel (hosting).
8. **Verdicts** and **What blind means** (kept from today's README, tightened).
9. **Results**: the measured numbers so far (the six demo cases before and after, reuse, kept environments);
   sub-project 2 adds the evaluation.
10. **Run it yourself**: the live demo first; then locally with Python 3.12: `pip install -r
    requirements.txt`, a `.env` with `NEBIUS_API_KEY` and `CONTREE_PROJECT` (optional `TAVILY_API_KEY`,
    `LANGSMITH_API_KEY`), and `python -m receipts run psf__requests-1142 --patch none` (expected: REFUTED in
    about a minute). Tests: `python -m pytest -q`.
11. **Repository map** and **Self-hosting** (one paragraph linking to `docs/SETUP.md`), **License**.

`docs/SETUP.md` takes today's detailed sections (API server, Neon sign-in and database, GitHub App, no-sign-in
demo, deploying on Render and Vercel, Cloud Run alternative, the full environment list), unchanged in substance.

## 2. Frontend README

The pitch and live links, two screenshots (landing and a receipt), "the main README is in receipts-backend",
then today's development notes.

## 3. Continuous integration

`.github/workflows/tests.yml` in each repository, on push and pull request:

- Backend: Python 3.12, `pip install -r requirements.txt`, `python -m pytest -q`. No secrets: the tests use
  fakes, and the Neon store tests run only when `TEST_DATABASE_URL` is set.
- Frontend: Node 22, `npm ci`, `npx vitest run`, `npm run build`.

Each README shows the workflow's badge.

## 4. Tavily on the receipt

The receipt gets a line after Sandbox: "Docs | N pages" with the note "library documentation found with Tavily
for the APIs the issue names", shown when the research brief found at least one source. Live, the count comes
from the `research` event (`{"sources": N}`); stored, from `evidence.research.sources`. The evidence section
already lists those sources. A reused test has no research, so no line.

## 5. Left to the user

- Repository settings: description, topics (`nvidia-nemotron`, `nebius`, `token-factory`, `ai-agents`,
  `code-review`, `testing`, `pull-requests`, `tavily`), homepage (`https://receipts-frontend-six.vercel.app`),
  for both repositories.
- An UptimeRobot (or cron-job.org) HTTP monitor on `https://receipts-backend-wnjy.onrender.com/api/health`
  every 5 minutes, with email alerts, kept until 15 December.
- The Token Factory credit balance, to be sure the demo can run through judging.

## Testing

- Frontend unit tests for the Docs line from events and from stored evidence, and its absence on a reused test.
- One real local run with only the two keys (criterion 3).
- The CI workflows pass on their first push.
- Screenshots taken from the live site with headless Edge.
