# Receipts: separate frontend, Neon login and database (Design)

Date: 2026-09-29. Builds on the MVP engine and web UI (`2026-09-28-*` specs). Product context: `PRODUCT.md`.

## Goal

Make Receipts a product people sign in to: a React frontend that deploys on its own, a FastAPI backend
that owns login (via Neon's managed auth) and stores runs in Neon Postgres, a minimal landing page,
sign-up/sign-in pages, and faster, cheaper API calls. GitHub repository access (GitHub App) is the next
step and out of scope here, apart from "Continue with GitHub" sign-in.

Done when: on localhost, a new user signs up (email/password or GitHub), starts a check from `/app`, the
receipt prints live, the run appears in "your receipts" from Neon, and anyone with the receipt link can
open it without signing in.

## Decisions (owner-approved)

| Topic | Decision |
|---|---|
| Layout | Two git repos: `receipts-backend/` (today's `receipts/`, renamed) and `receipts-frontend/` (made from `web/` with its history kept via `git subtree split`) |
| Auth | **Approach B:** FastAPI proxies all auth traffic to Neon Managed Better Auth, mirroring Neon's own server SDK proxy |
| GitHub | Now: "Continue with GitHub" sign-in through Neon Auth. Next step: GitHub App for repository access, webhooks and check runs |
| Visibility | Starting a check needs login; each user lists only their runs; any receipt URL is public (share by link) |
| Database | Neon Postgres via psycopg 3 async pool; plain SQL with versioned migration files |
| Cost guard | Per user: at most 2 active runs, 20 runs per day |

## Research basis (official documentation)

- Neon Managed Better Auth stores users, sessions and OAuth tokens in the `neon_auth` schema of our
  database; Better Auth 1.4.18 underneath. https://neon.com/docs/auth/overview
- Auth API lives under the Auth base URL (`NEON_AUTH_URL`); endpoints include `sign-up/email`,
  `sign-in/email`, `sign-in/social`, `sign-out`, `get-session`, `token`.
  https://neon.com/docs/auth/authentication-flow
- GitHub sign-in needs our own GitHub OAuth App; its callback must be `{NEON_AUTH_URL}/callback/github`;
  the app's `callbackURL` origin must be a trusted domain (localhost is pre-approved).
  https://neon.com/docs/auth/guides/setup-oauth , https://neon.com/docs/auth/guides/configure-domains
- Neon's server SDK proxies auth through the app and caches sessions; cookie domain and SameSite are
  configurable. https://neon.com/docs/auth/reference/nextjs-server . Protocol taken from the SDK source
  (`neondatabase/neon-js`, `packages/auth/src/server/{proxy,middleware,utils}`), see "Auth proxy".
- Connection pooling: use the `-pooler` connection string for app traffic (PgBouncer, transaction mode;
  protocol-level prepared statements supported), a direct connection for migrations.
  https://neon.com/docs/connect/connection-pooling
- Python: `DATABASE_URL=...?sslmode=require&channel_binding=require`. https://neon.com/docs/guides/python
- Production auth checklist: trusted domains, custom SMTP, app name, own OAuth credentials, email
  verification, disable "Allow Localhost". https://neon.com/docs/auth/production-checklist
- GitHub: only GitHub Apps can create check runs; GitHub recommends Apps over OAuth apps (fine-grained
  permissions, per-repo installs, 1-hour tokens); never trust `installation_id` from the setup URL, verify
  with a user access token. https://docs.github.com/en/rest/checks/runs ,
  https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps ,
  https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url

## Repositories

```
Nebiusxnemo/
  receipts-backend/            engine + FastAPI (git history kept; folder renamed from receipts/)
    receipts/auth.py             Neon Auth proxy, OAuth completion, current_user dependency
    receipts/db.py               psycopg AsyncConnectionPool + runs repository
    receipts/server.py           API (no SPA serving any more)
    migrations/001_init.sql
  receipts-frontend/           React + Vite + TypeScript (history of web/ kept)
```

The backend stops serving `web/dist`; `web/` is removed from the backend repo after the split.

## Auth proxy (mirrors Neon's SDK)

`GET|POST /api/auth/{path}` → `{NEON_AUTH_URL}/{path}?{same query}`:

- Request headers forwarded: `user-agent`, `authorization`, `referer`, `content-type`; `Origin` from the
  request's `origin` (else `referer` origin, else `FRONTEND_URL`); `Cookie` reduced to cookies whose name
  starts with `__Secure-neon-auth`; `x-neon-auth-middleware: true`. Body passed through.
- Response headers passed through: `content-type`, `content-encoding`, `date`, `set-cookie`,
  `set-auth-jwt`, `set-auth-token`, `x-neon-ret-request-id`. Each `Set-Cookie` is rewritten: drop
  `Partitioned`, force `Secure`, `SameSite=Lax`, `Domain=COOKIE_DOMAIN` when set.
- Upstream unreachable → `502 {"error", "code"}`.

OAuth completion: the frontend calls `signIn.social({provider: "github", callbackURL:
"{API_URL}/api/auth/complete?next={FRONTEND_URL}/app"})`. Neon's callback redirects there with
`?neon_auth_session_verifier=...` while the browser holds the `__Secure-neon-auth.session_challenge`
cookie (set on our API domain through the proxy). `GET /api/auth/complete` calls upstream `get-session`
with the verifier query and the challenge cookie, forwards the resulting `Set-Cookie`s (rewritten as
above) and redirects (302) to `next` with the verifier removed. `next` must start with `FRONTEND_URL`,
otherwise `FRONTEND_URL/app` (no open redirect).

`current_user` dependency: extract Neon Auth cookies; none → 401. Otherwise upstream `get-session`
(cached 60 s in memory keyed by SHA-256 of the cookie string); no user in the response → 401.
`POST /api/auth/sign-out` through the proxy also drops the cache entry.

CSRF: cookies are HttpOnly + Secure + SameSite=Lax; CORS allows only `FRONTEND_URL` with credentials;
state-changing endpoints accept JSON only. Deployment rule: frontend and backend under one parent
domain (e.g. `app.example.com`, `api.example.com`) so auth cookies stay first-party (Safari ITP).
Localhost ports count as the same site.

Frontend auth client: `createAuthClient("{VITE_API_URL}/api/auth", { fetchOptions: { credentials:
"include" } })` from `@neondatabase/neon-js/auth`.

## Data model

```sql
CREATE TABLE runs (
  id          text PRIMARY KEY,
  user_id     text,                -- neon_auth.user.id; NULL for imported example runs
  instance_id text NOT NULL,
  pr          text NOT NULL CHECK (pr IN ('gold', 'none', 'diff')),
  status      text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'error')),
  verdict     text,
  reason      text,
  seconds     real,
  tokens      integer,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  evidence    jsonb
);
CREATE INDEX runs_user_started ON runs (user_id, started_at DESC, id);
CREATE TABLE schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
```

No foreign key into `neon_auth` (Neon manages that schema). Writes per run: insert (queued), update
(running), update (done/error with verdict, reason, seconds, tokens, evidence). Live events stay in
memory during a run and are stored inside `evidence.events` at the end. On startup, runs left `queued`
or `running` become `error` ("backend restarted").

Commands: `python -m receipts migrate` (applies `migrations/*.sql` not yet in `schema_migrations`, over
`DATABASE_URL_UNPOOLED`), `python -m receipts import-runs` (loads `runs/*.json` as `user_id NULL`).

## API

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /api/health` | public | `{"ok": true}` |
| `GET /api/instances` | public | `Cache-Control: public, max-age=3600` + `ETag`, `304` on match |
| `GET /api/instances/{id}` | public | same caching; 404 unknown |
| `GET /api/runs/{id}` | public | `{status, evidence}`; finished runs `Cache-Control: public, max-age=31536000, immutable` |
| `GET /api/runs/{id}/events` | public | SSE replay + live (unchanged) |
| `GET /api/me` | cookie | `{id, email, name, image}` |
| `GET /api/runs?cursor=&limit=` | cookie | own runs, newest first, summary columns only, keyset cursor on `(started_at, id)`, `limit` ≤ 50 (default 20), `ETag` + `304` |
| `POST /api/runs` | cookie | validation as today; `429` when the user has 2 active runs or 20 runs in the last 24 h |
| `/api/auth/*`, `GET /api/auth/complete` | public | proxy and OAuth completion above |

Middleware: CORS (`FRONTEND_URL`, credentials), GZip for responses ≥ 1 KB except `text/event-stream`.
The DB pool opens at startup (min 1, max 10) on `DATABASE_URL`.

## Frontend (`receipts-frontend`)

| Route | Access | Content |
|---|---|---|
| `/` | public | Landing: headline, one sentence, example receipt as hero, three-step "How it works", CTAs "Get started" (`/signup`) and "See an example receipt" |
| `/signup`, `/signin` | public | Email + password form, "Continue with GitHub", switch link, inline errors, then `next` or `/app` |
| `/app` | signed in | Check form + "Your receipts" (own runs; polls only while one is active) |
| `/runs/:id` | public | Receipt (unchanged behavior) |
| `*` | public | Not found |

The landing hero renders a real receipt bundled as static JSON (`src/example-receipt.json`, taken from
the PROVEN run `pydata__xarray-4629-gold-20260928-201414`), so the landing page needs no API call; "See
an example receipt" links to `/runs/pydata__xarray-4629-gold-20260928-201414`, which `import-runs` puts
in the database.

Signed-out visitors to `/app` go to `/signin?next=/app`. Nav: signed out "Sign in" + "Get started";
signed in "New check" + account menu ("Sign out"). Design system unchanged (`PRODUCT.md`). Env:
`VITE_API_URL`. Static hosting needs an SPA fallback (all routes to `index.html`).

## Environment

Backend `.env` (new keys): `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `NEON_AUTH_URL`, `FRONTEND_URL`
(default `http://localhost:5173`), `API_URL` (default `http://localhost:8000`), `COOKIE_DOMAIN` (empty on
localhost), `MAX_ACTIVE_RUNS` (2), `RUNS_PER_DAY` (20). Frontend `.env`: `VITE_API_URL`.

Neon console: project in an AWS region; Auth enabled; Auth base URL and pooled/unpooled connection
strings copied. GitHub OAuth App (callback `{NEON_AUTH_URL}/callback/github`) added under Auth → OAuth
providers.

## Testing

- Backend unit tests with `httpx.MockTransport` as fake Neon Auth: forwarded headers/cookies,
  `Set-Cookie` rewriting, verifier exchange, open-redirect guard, 60 s session cache, 401s.
- API tests with an in-memory runs repository: ownership, public receipts, limits (`429`), pagination,
  `ETag`/`304`, caching headers.
- SQL tests against a Neon branch when `TEST_DATABASE_URL` is set (skipped otherwise).
- Frontend: Vitest for route guard and receipt model; `npm run build` passes.
- End to end in the browser once the owner's Neon keys are in: sign up, run a check, receipt prints,
  run listed, sign out, public link opens signed out.

## Out of scope

GitHub App (repository connect, webhooks, check runs), deployment and hosting, custom SMTP and email
verification (production checklist items), organizations/teams.
