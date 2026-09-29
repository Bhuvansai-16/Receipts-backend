# Receipts: GitHub App, real PR checks, Google sign-in and the new UI (Design)

Date: 2026-09-29. Builds on `2026-09-29-accounts-and-split-design.md` (Neon login, runs in Neon, split repos).

## Goal

Make Receipts usable on real GitHub pull requests, end to end: sign in (email, Google or GitHub), connect
GitHub, pick repositories, check a PR (button or automatically), see the receipt live, and find the result on
the PR as a GitHub check. Rebuild the public site and the app so a first-time visitor understands the product
and a signed-in user knows what to do next.

Done when, on localhost with the owner's GitHub App: an email user connects GitHub, installs the app on a
Python repo, clicks "Check this PR" on an open PR, the receipt prints live, and the PR shows a "Receipts"
check linking to it; with Auto-check on, pushing to the PR starts a new check by itself.

## Decisions (owner-approved)

| Topic | Decision |
|---|---|
| Google | "Continue with Google" through Neon Auth (Neon's shared Google credentials in development) |
| GitHub for email users | "Connect GitHub" links a GitHub account to the signed-in user (Better Auth `link-social`) |
| GitHub integration | One GitHub App: it is Neon's GitHub sign-in provider, is installed on chosen repos, and posts check runs |
| Triggers | Both: "Check this PR" button, and automatic checks on PR opened/updated behind a per-repo Auto-check switch (off by default) |
| Scope of real repos | Python repositories tested with pytest; anything that can't be set up is UNPROVEN with the reason |
| UI | Public site (Home, How it works, Security, Docs) with navbar and footer; app shell (Overview, Repositories, Pull requests, Receipts, Try a demo, Account) |

## Research basis (official documentation)

- GitHub Apps are GitHub's recommended integration: fine-grained permissions, per-repo installs, installation
  tokens that expire after 1 hour, user tokens after 8 hours (refresh tokens 6 months; expiry can be turned
  off). Only GitHub Apps can create check runs. A GitHub App works as an OAuth provider through the same
  `/login/oauth/authorize` web flow. https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app
- Best practices: minimum permissions, webhook secret with signature verification, keep the private key
  secret, cache and scope installation tokens, identify users by immutable ids, prefer webhooks to polling.
  https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
- Webhook deliveries are signed: `X-Hub-Signature-256: sha256=<HMAC-SHA256(secret, raw body)>`, compared in
  constant time. https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
- Never trust `installation_id` from the setup redirect; confirm it with the user's token via
  `GET /user/installations`. https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url
- Neon Auth: Google and GitHub providers; this project has Google on shared credentials and GitHub on its own
  client. Better Auth endpoints present on this server: `sign-in/social`, `link-social`, `list-accounts`,
  `get-access-token`, `email-otp/*` (probed).
- Nebius sandboxes reach github.com, api.github.com and pypi.org (tested); `python:3.11-slim` has no git,
  the full `python:3.11` image does.

## GitHub App (the owner registers it)

- Permissions: Metadata read (required), Contents read, Pull requests read, Issues read, Checks read and write.
- Events: Pull request. (Installation events arrive without subscribing.)
- Callback URL: `{NEON_AUTH_URL}/callback/github` (the app is Neon's GitHub provider; its client ID and
  secret replace the current OAuth app's in Neon > Auth > OAuth providers). "Request user authorization
  during installation": off.
- Setup URL: `{API_URL}/api/github/setup`, "Redirect on update" on.
- Webhook URL: `{API_URL}/api/github/webhook` (locally a smee.io channel forwarded to it), secret set.
- Backend env: `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY` (PEM, `\n` escaped) or
  `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET`, `API_URL` (for the setup redirect and check links).

## Backend

New modules in `receipts/`:

- `github_app.py`: app JWT (RS256, 9 minutes), installation tokens (cached until 5 minutes before expiry,
  scoped to one repository and the permissions a call needs), a small GitHub REST client, webhook signature
  check, check-run create and update.
- `targets.py`: `RepoTarget`, the real-repository counterpart of `swebench.Instance`: claim text, suite files,
  and `base_image()` that builds the environment in a Nebius sandbox.
- `github.py` (grows): connection status, installation sync, repositories, pull requests, check start,
  webhook, Auto-check switch.

Engine change (small): `engine.check` takes any target with `instance_id`, `repo`, `problem_statement`,
`pass_to_pass` (nodeids to guard, may be empty), optional `suite` (test files) and `base_image()`.
`swebench.Instance` gets a `base_image()` method that calls today's function. `restrict` applies only when
`pass_to_pass` is non-empty.

Real PR check (`RepoTarget` from a PR):

1. Installation token scoped to the repo: PR (`title`, `body`, `head.sha`), merge base
   (`compare/{base}...{head}`), diff (`application/vnd.github.diff`), linked issue (first closing keyword
   `fixes|closes|resolves #N` in the PR body) and the tarball at the merge base (max 50 MB).
2. Claim text: linked issue title and body; otherwise PR title and body. The test writer never sees the diff.
3. Environment, built once per (repo, merge base) per process: `python:3.11` image, tarball extracted to
   `/testbed`, `pip install -e ".[test]"` (then `[tests]`, `[dev]`, plain `-e .`), `requirements*.txt`, pytest.
   A no-op `conda` shim at `/opt/miniconda3/etc/profile.d/conda.sh` lets the existing sandbox commands run
   unchanged. Tokens never enter the sandbox: the backend downloads the tarball and uploads it as a file.
4. Suite: test files at the merge base that the PR modifies, plus `test_<module>.py` for each changed module.
5. The existing pipeline runs (blind test, 3+3 forks, suite, verdict, second opinion).
6. Check run: created `in_progress` when the run starts (`details_url` = receipt page), completed with
   PROVEN=success, REFUTED/REGRESSION=failure, UNPROVEN/NO_CHECKABLE_CLAIM=neutral, summary = verdict and
   reason. Check-run failures are logged and never fail the run.

Data (`migrations/002_github.sql`):

```sql
CREATE TABLE github_installations (id bigint PRIMARY KEY, account_login text NOT NULL,
  account_type text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE github_installation_users (installation_id bigint REFERENCES github_installations(id)
  ON DELETE CASCADE, user_id text NOT NULL, PRIMARY KEY (installation_id, user_id));
CREATE TABLE github_repo_settings (repo_id bigint PRIMARY KEY, installation_id bigint NOT NULL
  REFERENCES github_installations(id) ON DELETE CASCADE, full_name text NOT NULL,
  auto_check boolean NOT NULL DEFAULT false);
ALTER TABLE runs DROP CONSTRAINT runs_pr_check, ADD CONSTRAINT runs_pr_check CHECK (pr IN ('gold','none','diff','github'));
ALTER TABLE runs ADD COLUMN repo text, ADD COLUMN pr_number integer, ADD COLUMN head_sha text,
  ADD COLUMN check_run_id bigint;
CREATE INDEX runs_repo_pr ON runs (repo, pr_number, started_at DESC);
```

API (all cookie-authenticated except the webhook; access to a repo requires one of the user's installations):

| Endpoint | Behavior |
|---|---|
| `GET /api/github/status` | `{app_configured, github_linked, install_url, installations}` |
| `GET /api/github/setup` | Setup URL target: sync installations with the user's token, redirect to `/app/repos` |
| `POST /api/github/installations/sync` | Re-read `GET /user/installations` for this user; store what GitHub confirms |
| `GET /api/github/repos` | Repositories in the user's installations, with `auto_check` |
| `PUT /api/github/repos/{repo_id}/auto-check` | `{enabled}` |
| `GET /api/github/repos/{owner}/{repo}/pulls` | Open PRs with linked issue and latest receipt |
| `POST /api/github/repos/{owner}/{repo}/pulls/{number}/check` | Start a check (same per-user limits), `{run_id}` |
| `POST /api/github/webhook` | Verify signature; `pull_request` opened/synchronize/reopened/ready_for_review on an Auto-check repo starts a check for the installation's first user (limits apply; one run per head SHA); `installation` deleted removes it; answers 202 at once |
| `GET /api/me` | adds `usage: {active, today, max_active, per_day}` |

Security: signature checked before parsing; unknown or disabled app answers 503; delivery IDs de-duplicated
in memory; installation tokens scoped per call; no token or secret reaches the browser or the sandbox; PR
code runs only inside Nebius sandboxes, which hold nothing secret.

## Frontend

Structure: `src/site/` (public layout, navbar, footer, pages, SVG illustrations), `src/app/` (app shell and
pages), shared `src/components/`.

Public site (white and cream fields, black type, honey accents, no blue; illustrations are inline SVG in a
soft, rounded, clay-like style):

- Navbar: logo, How it works, Security, Docs, then Sign in and Get started (or Open app); mobile menu.
- Home: hero (headline, lead, CTAs, receipt-printer illustration), "How it works" in 3 illustrated steps,
  the 5 verdicts, "Why you can trust it", live example receipt, closing CTA band.
- How it works, Security, Docs (getting started, connecting GitHub, verdicts, limits, FAQ).
- Footer: product, resources, source links, "Built with Nebius Token Factory, Neon, LangSmith, Tavily".
- Sign in / Sign up: Google, GitHub, email with code confirmation.

App shell (sidebar on desktop, tab bar on phones):

- Overview: checklist 1 Connect GitHub, 2 Add repositories, 3 Check your first PR (each with its button,
  ticked when done), recent receipts, checks left today.
- Repositories: installed repos, Auto-check switch, "Add repositories" (install link), "Refresh".
- Pull requests (`/app/repos/:owner/:repo`): open PRs, linked issue, latest receipt verdict, Check this PR.
- Receipts: history with paging.
- Try a demo: today's SWE-bench form.
- Account: signed-in identity, connected accounts (GitHub link button), sign out.

## Testing

- Backend: webhook signature (valid, missing, wrong), JWT claims, installation-token caching and scoping,
  installation sync trusts only GitHub's list, repo access control, PR target construction (linked issue,
  fallback claim, suite selection), check-run conclusion mapping, webhook-triggered run once per head SHA,
  migration 002 against Neon.
- Frontend: Vitest for checklist state, verdict-to-copy helpers; build passes; browser check of public pages
  (desktop and 375 px), sign-in pages and app pages.
- Live: one real PR check on a small public Python repo (credits: roughly 300K model tokens).

## Out of scope

Non-Python or non-pytest repos, private-key vault, multi-instance deployment, billing, organizations UI.
