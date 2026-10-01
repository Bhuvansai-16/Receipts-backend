# Phase 1: ship it (Design)

Date: 2026-10-01. Builds on `2026-09-30-autonomous-checks-design.md`.

The roadmap comes from the 1 October strategy, summarised in `RECEIPTS-ARCHITECTURE.md` section 21. Phase 1
runs from 1 to 4 October.

## Goal

**A stranger can open a URL and watch a real check run without signing up.**

Done when, on the deployed URLs:

1. `/demo` starts a hand-picked check (or joins the one running) without an account, and the receipt prints
   live to a verdict.
2. The signed-in product still works end to end:
   - sign up or sign in;
   - connect GitHub;
   - check a pull request;
   - the pull request shows a `Receipts` check run that links to the receipt.
3. Both repositories are public under Apache-2.0. This is already done (see below).

## Decisions (made with the owner)

- **Repositories.** `Bhuvansai-16/Receipts-backend` and `Bhuvansai-16/Receipts-frontend` are public, on
  `main`, with full history and `LICENSE` (Apache-2.0). Pushed on 2026-10-01, after a scan of every commit
  found no secrets.
- **Hosting.**
  - The frontend runs on **Vercel**, deployed from its GitHub repository.
  - The backend runs on **Google Cloud Run**, region `us-east5`, next to Neon's `us-east-2`.
  - Both use free hostnames (`*.vercel.app`, `*.run.app`); there is no custom domain yet.
- **One origin for the browser.**
  - Vercel forwards `/api/*` to Cloud Run with a rewrite. The browser only talks to the Vercel address, so the
    API's `SameSite=Lax` session cookies stay first-party.
  - Giving the browser the Cloud Run URL directly would break sign-in: `vercel.app` and `run.app` are different
    sites, and Safari blocks third-party cookies.
- **Two direct paths to Cloud Run.** Vercel's proxy ends forwarded requests at 120 seconds, so:
  - the live event stream connects to Cloud Run directly (it is public and needs no cookie);
  - GitHub webhooks go to Cloud Run directly.
- **Demo.** Curated live checks: about 6 hand-picked cases, one demo check at a time, a hard daily cap, and a
  gallery of finished receipts.
- **Daytona leaves the code.** Nebius Sandboxes is the only sandbox provider.
- **The two bugs deferred from the final review are fixed.**

## Non-goals

- A custom domain. It can come later; the design already works without one.
- More than one backend instance. Live runs, streams and caches live in one process.
- Per-IP accounting for the demo. One demo check at a time plus a daily cap bounds the spend.
- Any change to the verdict rules.

## Components

### 1. No retry when the model provider fails

**Today:**

- When the writer agent's model call fails, `write_test` catches it as "agent stopped" and returns no test.
  This happens with connection errors, timeouts, rate limits, 5xx responses, and provider 4xx responses.
- The engine then retries with the same provider, which wastes tokens.
- The final reason, "no valid reproducing test ...", blames the writer.
- `ChatOpenAI` already retries each call 3 times, so a failure that reaches the writer means the provider is
  down.

**Change:**

- `WriterResult.provider_error` is set when the agent run raises `openai.APIError` (the base class for the
  provider's errors). Budget, recursion and other writer failures leave it unset.
- `engine._pipeline` retries only when there is no test **and** no provider error.
- With a provider error, the result is UNPROVEN: "the test writer's model was unavailable: <error>". The
  evidence keeps the writer's attempts as usual.
- Frontend: `explain.ts` maps that reason to "The model service didn't answer". The body says the result says
  nothing about the pull request, and suggests checking again later.

### 2. Eval output stays out of public examples

**Today:** `scripts/eval_prs.py` writes `runs/eval-*.json`. `import-runs`, and the in-memory store's seeding,
load every `runs/*.json` as public example receipts.

**Change:**

- The script writes `runs/eval/eval-<label>-pr<N>.json`, and existing eval files move there.
- `db.import_runs` reads `runs/*.json` without descending into folders, and also skips names that start with
  `eval-`.

### 3. Daytona removed

- **Delete:** `receipts/daytona_backend.py` and `tests/test_daytona.py`.
- **Remove the setting and its branches:**
  - `config.SANDBOX_PROVIDER`;
  - the Daytona branches in `swebench.base_image`, `server.lifespan` and the CLI (`smoke`, `_closing`);
  - the provider guard in `targets.RepoTarget.base_image`;
  - the Daytona case in `writer.agent_backend`.
- **Docs:** remove the README section and the `.env.example` lines.
- Git history keeps the stopgap for reference.

### 4. No-sign-in demo

**Cases.** `receipts/demo_cases.json` lists about 6 cases. Each has:

- `id`, `instance_id`, a title, and one sentence on what the issue is about;
- `patch`: `gold`, `none`, or a file in `receipts/demo_patches/` (a hand-written wrong fix);
- `kind`: real fix, empty patch, or wrong patch.

A wrong patch must apply cleanly and leave the bug in place. A case is listed only after one live run on
today's pipeline gives the expected verdict. xarray 4629 qualifies already: the real fix is Proven and the
empty patch Refuted. The expected verdict is used only to validate the case; it is never shown as a promise.

**Backend** (`receipts/demo.py`, a router mounted at `/api/demo`):

- `GET /api/demo` returns:
  - the cases (without patch contents);
  - `live`, the id of the demo check running now, or null;
  - `gallery`, the latest finished demo receipts;
  - `left_today`.
- `POST /api/demo/runs {"case": id}` needs no sign-in and returns `202 {run_id, joined}`:
  - **Unknown case:** 404.
  - **A demo check is already live:** its id comes back with `joined: true`. One demo check at a time, enforced
    by an `asyncio.Lock`; this is safe because the backend runs as a single instance.
  - **Over the daily cap** (`DEMO_RUNS_PER_DAY`, default 20) **or the global cap:** 429, "Today's demo checks
    are used up. The finished receipts below show real runs, or sign in to check your own pull requests."
  - **Otherwise:** the check is launched through `checks.launch` with `user_id = "demo"`. The id is reserved;
    Neon user ids are UUIDs. Demo runs count toward the global cap, and the existing store queries
    (`usage`, `list_for_user`) serve the cap and the gallery with no schema change.

**Frontend:**

- **A public `/demo` page:**
  - case cards, grouped by issue: "Real fix", "Empty patch", "Wrong patch";
  - "Run this check" calls `POST`, then opens `/runs/:id`, the public receipt page, which already prints live;
  - a banner, "A check is running now", that links to the live one;
  - the gallery below;
  - the 429 message when the caps are used up.
- **Landing page:** the primary call to action becomes "Watch a live check", linking to `/demo`. "Get
  started" stays for sign-up.

### 5. Split hosting

**Backend container:**

- **`Dockerfile`** (python:3.12-slim): `requirements.txt` with direct dependencies pinned to the versions
  tested locally, `receipts/` and `migrations/`.
  - The SWE-bench Verified cache is baked in at build time (`swebench._dataset()`), so a cold start skips the
    15 to 30 s Hugging Face load.
- **`.dockerignore`:** `.env`, `*.pem`, `runs/`, `.cache/`, `tests/`, `docs/`, `.superpowers/`.
- **`python -m receipts serve`** binds `0.0.0.0:$PORT` when `PORT` is set (Cloud Run), and
  `localhost:8000` otherwise.

**Cloud Run** (`receipts-api`, `us-east5`):

| Setting | Value | Why |
|---|---|---|
| Instances | min 1, max 1 | One process by design. |
| CPU | always allocated (`--no-cpu-throttling`) | Checks keep running after `POST` returns. |
| Request timeout | 3600 s | Live streams stay open. |
| Size | 2 vCPU, 2 GiB | |
| Concurrency | 250 | |

- **Secrets** come from Secret Manager:
  - `NEBIUS_API_KEY`, `CONTREE_PROJECT` (and `CONTREE_TOKEN` if used);
  - `TAVILY_API_KEY`, `LANGSMITH_API_KEY`;
  - `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `NEON_AUTH_URL`;
  - `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`.
- **Plain env:** `FRONTEND_URL=https://<project>.vercel.app`.
- **Deploying:**
  - The first deploy is `gcloud run deploy --source .`. Cloud Build builds the image, so no local Docker is
    needed.
  - Later deploys run from Cloud Run's "connect repository" (a Cloud Build trigger on pushes to `main`).

**Frontend on Vercel:**

- **The project:** imported from `Bhuvansai-16/Receipts-frontend`; build with `npm run build`, output `dist/`.
- **`vercel.json`:**
  - `/api/:path*` is rewritten to `https://<service>.run.app/api/:path*`, with
    `x-vercel-enable-rewrite-caching: 0`;
  - every other path falls back to `index.html`;
  - static files win over rewrites.
- **Code changes:**
  - `api.ts`:
    - Without `VITE_API_URL`, the API is the same origin in production builds and `http://localhost:8000` in
      development.
    - `EVENTS_URL` is `VITE_EVENTS_URL`, falling back to the API URL. `subscribe()` uses it.
  - `auth.ts` and `authFlow.socialCallbacks` build absolute URLs from `window.location.origin` when the API is
    same-origin, because the auth client and Neon's callback need absolute URLs.
- **Vercel env:** `VITE_EVENTS_URL=https://<service>.run.app`.

**Outside settings** (the owner sets these; the plan gives exact values):

| Where | Setting |
|---|---|
| Neon Auth | Add the Vercel URL to trusted domains |
| GitHub App | Setup URL `https://<project>.vercel.app/api/github/setup` (through Vercel, because it needs the session cookie) |
| GitHub App | Webhook URL `https://<service>.run.app/api/github/webhook` |
| GitHub App | Callback URL unchanged: `{NEON_AUTH_URL}/callback/github` |
| Local dev | smee is no longer needed in production; it stays for local development |

## Integrity and failure handling

- Demo checks run the same pipeline and the same verdict rules, and every receipt stays public evidence.
- **Cloud Run restarts.** A redeploy or maintenance restart ends any running check. On startup,
  `fail_unfinished` marks those checks as errors (already in place).
- **Dropped streams.** A dropped event stream reconnects, and the server replays the full backlog (already in
  place). This also covers proxies and mobile networks.
- **Spend is bounded:**
  - one demo check at a time;
  - `DEMO_RUNS_PER_DAY`;
  - the global daily cap;
  - per-user limits for signed-in checks;
  - one Cloud Run instance.

## Testing

**Backend, test first:**

- the provider error: no retry, the honest reason, and `provider_error` set only for `openai.APIError`;
- eval output skipped by `import_runs`;
- the suite stays green after the Daytona removal;
- demo endpoints:
  - no sign-in needed;
  - joining the live check;
  - 404 for an unknown case;
  - 429 at the daily cap and at the global cap;
  - the gallery shows finished receipts only;
- `serve` host and port from `PORT`.

**Frontend, test first:**

- API and events URL resolution;
- absolute auth callbacks;
- the provider-error explanation;
- the demo page's states (live banner, caps used up).

**Deployment checks:**

1. `/api/health` on the `run.app` URL, and through Vercel.
2. Sign-in on the Vercel URL.
3. A demo check streaming live from a phone and a laptop.
4. A redelivered GitHub webhook starting an Auto-check, and its check run linking to the Vercel receipt.

**Demo cases:** each one is validated by a live run before it is listed, and the results are recorded in this
spec.

## Build order

| Date | Work |
|---|---|
| 1 Oct | Components 1 to 3 (the bugs and Daytona) |
| 2 Oct | Component 4 (the demo): backend, frontend, case selection |
| 3 Oct | Component 5 (split hosting): code, container, first Cloud Run and Vercel deploys |
| 4 Oct | Demo case validation, outside settings, the end-to-end checks above, the Phase 1 check |

## Results (2026-10-01)

**Demo cases.** Each case ran once through the no-sign-in endpoint, on the local API with today's pipeline.
The results are stored in Neon as the first demo receipts, so the gallery starts with real runs. Both wrong
patches applied cleanly in a base sandbox before the runs.

| Case | Verdict | Time | Tokens |
|---|---|---|---|
| xarray 4629, real fix | PROVEN | 55 s | 52K |
| xarray 4629, empty patch | REFUTED | 50 s | 55K |
| xarray 4629, wrong patch (copies the attrs, returns the original) | REFUTED | 53 s | 56K |
| requests 1142, real fix | PROVEN | 42 s | 41K |
| requests 1142, empty patch | REFUTED | 97 s | 108K |
| requests 1142, wrong patch (skips Content-Length for HEAD only) | REFUTED | 49 s | 44K |

All six gave the expected verdict, so every case stays listed. Together they took about six minutes and 355K
tokens. A demo check costs well under a minute of a visitor's time and a few cents of model spend.

**Model outages.** A model outage anywhere in a check now reads as one. The writer's own outage gives "the
test writer's model was unavailable". Any other stage gives "a model was unavailable", for example the
classifier, which is the first model call. Both are UNPROVEN, with no retry and no blame.
