# Receipts

Checks whether a pull request does what it claims: writes the missing test blind (from the issue only),
runs it in forked Nebius Token Factory sandboxes, and reports the evidence.

MVP: fix engine on SWE-bench Verified, as a CLI and an API (UI in the `receipts-frontend` repo).

## Setup (global Python, no venv)

```bash
pip install contree-sdk "contree-client[httpx]" deepagents langchain-openai langchain-tavily langsmith datasets python-dotenv \
    fastapi uvicorn sse-starlette "psycopg[binary]" psycopg-pool httpx
cp .env.example .env   # fill in keys
python -m receipts smoke --instance psf__requests-1142
```

## API server

The React UI is the separate `receipts-frontend` repo; it talks only to this API. People sign up with email
and password or GitHub (Neon Auth), runs are stored in Neon Postgres, and anyone with a receipt link can open
it without signing in.

```bash
python -m receipts migrate       # once, and after new files in migrations/
python -m receipts import-runs   # optional: publish runs/*.json as example receipts
python -m receipts serve         # http://localhost:8000
```

Without `DATABASE_URL` the API still starts: it keeps runs in memory, starting from the saved ones in `runs/`.

## Sign-in and database (Neon)

1. Neon Console > your project (AWS region) > Connect: the pooled connection string (host contains
   `-pooler`) goes in `DATABASE_URL`, the direct one in `DATABASE_URL_UNPOOLED`. Keep
   `sslmode=require&channel_binding=require` on both.
2. Neon Console > Auth: enable it and copy the Auth URL into `NEON_AUTH_URL`.
3. GitHub sign-in: create a GitHub OAuth App (GitHub > Settings > Developer settings) with the callback URL
   `{NEON_AUTH_URL}/callback/github`, then add its client ID and secret under Neon Auth > OAuth providers.
4. `FRONTEND_URL` is where the UI runs (default `http://localhost:5173`). In production, serve UI and API from
   one parent domain (`app.example.com` + `api.example.com`) so auth cookies stay first-party, add both to Neon
   Auth's trusted domains, and work through Neon's production checklist (own SMTP, email verification,
   "Allow localhost" off).

The browser never talks to Neon directly: `/api/auth/*` proxies Neon Auth the way Neon's own server SDK does
and rewrites its cookies to `HttpOnly; Secure; SameSite=Lax`. Other endpoints: `/api/me`, `/api/runs` (your
runs, newest first, paged), `/api/runs/{id}` and `/api/runs/{id}/events` (public receipts), `/api/github/repos` (the
signed-in user's public GitHub repositories, read with their GitHub sign-in token on the server), `/api/health`.

## GitHub App (real pull requests)

One GitHub App does three jobs: it is Neon's "Sign in with GitHub" provider, users install it on the
repositories they want checked, and it posts each verdict as a `Receipts` check on the pull request.

1. GitHub > Settings > Developer settings > GitHub Apps > New GitHub App.
   - Callback URL: `{NEON_AUTH_URL}/callback/github`. Leave "Request user authorization during installation" off.
   - Setup URL: `{API_URL}/api/github/setup`, with "Redirect on update" on.
   - Webhook: active, URL `{API_URL}/api/github/webhook` (locally, a smee.io channel, see below), and a secret.
   - Repository permissions: Metadata read, Contents read, Pull requests read, Issues read, Checks read and write.
   - Subscribe to events: Pull request.
2. Generate a private key (a `.pem` file) and note the App ID and the app's URL name (`github.com/apps/<slug>`).
3. Neon Console > Auth > OAuth providers > GitHub: replace the client ID and secret with this app's.
   Existing GitHub users sign in once more afterwards.
4. Add to `.env`: `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY_PATH` (or the PEM itself in
   `GITHUB_APP_PRIVATE_KEY` with newlines written as `\n`), `GITHUB_WEBHOOK_SECRET`, and `API_URL`.
5. Run `python -m receipts migrate` for the GitHub tables.

Local webhooks: GitHub can't reach `localhost`, so forward a smee.io channel to the API:

```bash
npx smee-client --url https://smee.io/<your-channel> --target http://localhost:8000/api/github/webhook
```

Checks on real repositories need `SANDBOX_PROVIDER=contree` (Nebius sandboxes) and Python projects tested with
pytest. The API only trusts installations that GitHub lists for the signed-in user, verifies every webhook
signature, and gives each GitHub call a token limited to one repository; no token enters a sandbox.

Limits keep a public app affordable: each user gets `MAX_ACTIVE_RUNS` checks at a time and `RUNS_PER_DAY` in 24
hours, everyone together gets `GLOBAL_RUNS_PER_DAY`, and `ALLOWED_GITHUB_ACCOUNTS` (when set) limits webhook
auto-checks to those GitHub accounts.

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
- The research brief searches only the library's documentation site, from names in the issue text. Code hosts
  are excluded by Tavily and dropped again locally, as are Sphinx "view source" pages, which can show newer code.
- Residual risk: the sandbox has network access. Every shell command the agent ran and every source the brief
  used is recorded in the evidence JSON (`writer.tool_log`, `research.sources`) so a leak can be audited.

## Models

| Step | Model |
|---|---|
| Claim classification | Nemotron 3 Nano |
| Research brief before the writer starts (library docs for the APIs the issue names) | Tavily |
| Blind test writing (Deepagents agent in sandbox) | Nemotron 3 Super |
| One automatic retry when no test was accepted | `MODEL_TEST_WRITER_STRONG` (see below) |
| Scope check of each submitted test (only what the issue asks) | Nemotron 3 Super |
| Second opinion before REFUTED | Nemotron 3 Ultra |

The writer model was chosen by an A/B on five real pull requests (`scripts/eval_prs.py`): Nemotron 3 Super wrote a
valid blind test for 4 of 5 with half the tokens; Nemotron 3.5 Lightning for none. Set `MODEL_TEST_WRITER` (first
attempt) and `MODEL_TEST_WRITER_STRONG` (the one automatic retry) to change them.
