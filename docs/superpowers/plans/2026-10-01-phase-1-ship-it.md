# Phase 1: Ship It Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** A stranger opens a URL and watches a real check run without signing up. Receipts is deployed with the
frontend on Vercel and the backend on Cloud Run.

**Architecture:**

- **No-sign-in demo:** a new `receipts/demo.py` router reuses `checks.launch` and the run store. Demo runs
  belong to the reserved user id `"demo"`.
- **Split hosting:** Vercel serves the SPA and forwards `/api/*` to Cloud Run, so the browser has one origin.
  The live event stream and GitHub webhooks go to Cloud Run directly.
- **Bugs and cleanup:** the two deferred bugs and the Daytona removal are small, test-first backend changes.

**Tech Stack:**

- Backend: FastAPI, Python 3.12, Cloud Run, Cloud Build.
- Frontend: React 19, Vite 8, Vitest, Vercel.

**Spec:** `docs/superpowers/specs/2026-10-01-phase-1-ship-it-design.md`

## Global Constraints

- Verdict rules in `verdict.py` are unchanged; demo checks run the same pipeline.
- Asymmetry rule: anything uncertain is UNPROVEN, and every new reason is honest about its cause.
- No secrets in the repositories, in URLs or in the frontend bundle; Cloud Run reads them from Secret Manager.
- One Cloud Run instance (min 1, max 1), CPU always allocated, request timeout 3600 s, region `us-east5`.
- `DEMO_RUNS_PER_DAY` defaults to 20; one live demo check at a time.
- Local development stays on global Python with `.env` (no venv); the container is only for Cloud Run.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push to GitHub once, after every
  task is done and both suites pass.

## Review Focus

1. **Two visitors press Run at the same moment.** Exactly one demo check starts and the other joins it. Test:
   start, then start again while live, and expect `joined: true` with the same id.
2. **A demo check ends with an error, for example after a Cloud Run restart.** It never appears in the gallery,
   which shows finished `done` runs only. Test in Task 4.
3. **`VITE_EVENTS_URL` is missing on Vercel.** The stream falls back to the same-origin proxy, which reconnects
   every 120 s and replays the backlog: degraded, not broken. Test of the URL resolution in Task 6.
4. **Social sign-in with a same-origin API.** The callback URL must be absolute (built from
   `window.location.origin`), or Neon rejects it. Test in Task 6.
5. **The caps are used up.** The 429 detail is shown on `/demo` in plain words, and the gallery stays
   reachable. Tests in Tasks 4 and 5.

---

### Task 1: No retry when the model provider fails

**Files:**
- Modify: `receipts-backend/receipts/writer.py` (`WriterResult`, the `except` around `agent.ainvoke`)
- Modify: `receipts-backend/receipts/engine.py` (the retry block)
- Modify: `receipts-frontend/src/run/explain.ts` (UNPROVEN entry)
- Test: `receipts-backend/tests/test_writer.py`, `tests/test_engine.py`, `receipts-frontend/src/run/explain.test.ts`

**Interfaces:**
- Produces: `WriterResult.provider_error: str = ""` (empty when the writer itself gave up). Engine reason:
  `"the test writer's model was unavailable: <TypeName>: <message>"`.

- [ ] **Step 1: Write the failing engine test**

```python
def test_a_provider_outage_gets_no_retry_and_says_so(monkeypatch):
    calls = []

    async def write_test(issue, img, emit=None, **kw):
        calls.append(kw)
        return SimpleNamespace(test_code=None, attempts=0, reason="agent stopped: APIConnectionError: down", log=[],
                               submissions=[], provider_error="APIConnectionError: down")

    monkeypatch.setattr(engine, "write_test", write_test)
    ev = asyncio.run(engine.check(INST, PATCH))
    assert len(calls) == 1 and "writer_first" not in ev
    assert ev["verdict"] == "UNPROVEN" and ev["reason"] == "the test writer's model was unavailable: APIConnectionError: down"
```

- [ ] **Step 2: Write the failing writer test.** The agent raises `openai.APIConnectionError`, and
  `provider_error` is set. A budget error (`AgentBudgetExceeded`) leaves it empty.
- [ ] **Step 3: Run both and watch them fail.**
  `python -m pytest tests/test_engine.py tests/test_writer.py -q -k "provider"`
- [ ] **Step 4: Implement.**
  - Writer: `except openai.APIError as e: out.provider_error = f"{type(e).__name__}: {e}"[:300]` before the
    generic `except`.
  - Engine:
    ```python
    if w.test_code is None and getattr(w, "provider_error", ""):
        ev["writer"] = ...
        return UNPROVEN, ...
    ```
    Retry only when there is no `provider_error`.
- [ ] **Step 5: Frontend.**
  - Add an `explain.test.ts` case: the reason "the test writer's model was unavailable: X" gets the headline
    "The model service didn't answer".
  - Add the UNPROVEN entry: match `/model was unavailable/`; body "The test-writing model didn't respond, so
    nothing was checked. This says nothing about the pull request."; next "Check again in a few minutes."
- [ ] **Step 6: Run both suites, then commit in each repository.**

### Task 2: Eval output stays out of public examples

**Files:**
- Modify: `receipts-backend/scripts/eval_prs.py`, `receipts-backend/receipts/db.py` (`import_runs`)
- Test: `receipts-backend/tests/test_runs_store.py`

- [ ] **Step 1: Failing test.**

```python
def test_import_runs_skips_eval_output(tmp_path):
    ev = {"instance_id": "x", "verdict": "PROVEN", "reason": "r", "seconds": 1.0, "events": []}
    (tmp_path / "a-gold-20260101-000000.json").write_text(json.dumps(ev))
    (tmp_path / "eval-final-pr16.json").write_text(json.dumps(ev))
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "eval-fixed-pr15.json").write_text(json.dumps(ev))
    store = db.MemoryRuns()
    assert asyncio.run(db.import_runs(store, tmp_path)) == 1
```

- [ ] **Step 2: Implement.**
  - `import_runs` skips `path.name.startswith("eval-")`.
  - `eval_prs.py` writes `config.RUNS_DIR / "eval" / f"eval-{label}-pr{number}.json"` (with `mkdir(parents=True)`).
- [ ] **Step 3: Move existing local `runs/eval-*.json` into `runs/eval/`.** `runs/` is gitignored, so no commit
  is involved.
- [ ] **Step 4: Run the suite, then commit.**

### Task 3: Remove Daytona

**Files:**
- Delete: `receipts-backend/receipts/daytona_backend.py`, `tests/test_daytona.py`
- Modify:
  - `receipts/config.py`: drop `SANDBOX_PROVIDER`.
  - `receipts/swebench.py`: `base_image` always uses Contree.
  - `receipts/server.py`: drop the lifespan Daytona close.
  - `receipts/__main__.py`: `smoke` always tests Contree; `_closing` is removed and its call sites become plain
    `asyncio.run`.
  - `receipts/targets.py`: drop the provider guard.
  - `receipts/writer.py`: `agent_backend` drops the `hasattr(image, "agent_backend")` branch.
  - `tests/test_targets.py`: drop the `SANDBOX_PROVIDER` monkeypatch.
  - `README.md`: drop the "Sandbox provider" section.
  - `.env.example`: drop `SANDBOX_PROVIDER` and the `DAYTONA_*` lines.

- [ ] **Step 1: Make the changes.**
  `grep -rn "daytona\|SANDBOX_PROVIDER" receipts tests README.md .env.example` must print nothing (the specs and
  plans in `docs/` keep their history).
- [ ] **Step 2: Run the full suite (minus the deleted file) and expect it green.**
- [ ] **Step 3: Commit.**

### Task 4: Demo backend

**Files:**
- Create: `receipts-backend/receipts/demo.py`, `receipts/demo_cases.json`,
  `receipts/demo_patches/requests-1142-head-only.diff`, `receipts/demo_patches/xarray-4629-copy-unused.diff`
- Modify: `receipts/server.py` (`app.include_router(demo.router)`), `receipts/config.py` (`DEMO_RUNS_PER_DAY`)
- Test: `receipts-backend/tests/test_demo.py`

**Interfaces:**
- `GET /api/demo`: `{"cases": [{"id", "title", "repo", "instance_id", "kind", "summary"}], "live": str|null,
  "gallery": [RunSummary], "left_today": int}`.
- `POST /api/demo/runs {"case": str}`: `202 {"run_id": str, "joined": bool}`; 404 for an unknown case; 429 when
  a cap is used up.
- `DEMO_USER = "demo"`; `cases()` reads the JSON once (`lru_cache`).
- `prepare` returns `(swebench.load_instance(...), patch)`, where the patch is gold, `None`, or the file text.

- [ ] **Step 1: Failing tests in `tests/test_demo.py`.** Reuse `test_server.py`'s fake engine and fixture
  pattern.
  - `GET /api/demo` needs no sign-in, and lists the cases without patch text.
  - `POST` with no sign-in returns 202, and the run is stored with `user_id == "demo"`.
  - A second `POST` while one is live returns the same `run_id` with `joined: true`. (The fake engine is held
    open with an `asyncio.Event`.)
  - An unknown case returns 404.
  - With `DEMO_RUNS_PER_DAY = 1` and one demo run already today, `POST` returns 429 with the plain-words
    detail.
  - When the global cap is reached, `POST` returns 429.
  - The gallery shows only `done` demo runs (not errors, not running), newest first, at most 12.
- [ ] **Step 2: Watch them fail.**
- [ ] **Step 3: Implement `demo.py`.**
  - An `asyncio.Lock` around the "live? else caps? else launch" sequence.
  - Live means `checks.LIVE` holds the remembered `_live_id`.
  - Caps:
    - `runs.usage(DEMO_USER, since)[1] >= config.DEMO_RUNS_PER_DAY`;
    - `runs.global_recent(since) >= config.GLOBAL_RUNS_PER_DAY`.
  - Launch: `checks.launch(runs, DEMO_USER, run_id, inst.instance_id, pr_kind, prepare)`, with `pr_kind` from the
    case (`gold`, `none`, or `diff` for a patch file) and `run_id = engine.new_run_id(instance_id, pr_kind,
    taken=LIVE)`.
- [ ] **Step 4: Cases and patches.**
  - The six cases from the spec.
  - The wrong patches (hunks verified by `git apply --check` in Task 7):
    - `requests-1142-head-only.diff`: `elif self.method != 'HEAD':` in place of the real
      `not in ('GET', 'HEAD')`, so GET keeps `Content-Length: 0`.
    - `xarray-4629-copy-unused.diff`: `attrs = dict(variable_attrs[0])` is computed, but `variable_attrs[0]`
      is still returned.
- [ ] **Step 5: Run the suite, then commit.**

### Task 5: Demo frontend

**Files:**
- Create: `receipts-frontend/src/pages/DemoPage.tsx`, `src/demo.ts` (pure helpers), `src/demo.test.ts`
- Modify:
  - `src/api.ts`: `api.demo()`, `api.startDemo(caseId)`, and the `DemoInfo`/`DemoCase` types.
  - `src/App.tsx`: the `/demo` route inside `SiteLayout`.
  - `src/site/SiteLayout.tsx`: a "Live demo" nav link.
  - `src/site/HomePage.tsx`: the primary call to action becomes "Watch a live check" for signed-out visitors.
  - Styles in `src/site/site.css`.

- [ ] **Step 1: Failing tests for `demo.ts`.**
  - `groupCases(cases)` groups by issue (instance id) in file order, with kinds in the order real fix, empty
    patch, wrong patch.
  - `demoMessage(err)` turns a 429 `ApiError` into the server's detail, and anything else into "Couldn't start
    the check. Try again in a moment."
- [ ] **Step 2: Implement `demo.ts` and `DemoPage`.**
  - Fetch `api.demo()`.
  - Live banner: "A check is running now" with a link to `/runs/:live`.
  - One card per issue with three buttons. Each calls `startDemo` and then `navigate(`/runs/${run_id}`)`.
  - On error, an inline `role="alert"` message.
  - Gallery: a list of finished demo receipts with `VerdictChip` and `runLabel`.
  - "Sign in to check your own pull requests" link.
- [ ] **Step 3: Wire up the route, nav and CTA.**
  - Signed-out visitors see "Watch a live check" (to `/demo`) as primary and "Get started" as secondary.
  - Signed-in users keep "Open app".
- [ ] **Step 4: Run `npx vitest run` and `npm run build`, check `/demo` in the browser pane against the local
  API, then commit.**

### Task 6: Split-hosting code

**Files:**
- Backend:
  - Modify: `receipts/__main__.py` (`serve` host and port).
  - Create: `Dockerfile`, `.dockerignore`, `requirements.txt`.
  - Test: `tests/test_cli.py`.
- Frontend:
  - Create: `src/config.ts` (URL resolution, pure).
  - Modify: `src/api.ts` (`API_URL` and `EVENTS_URL` from `config.ts`), `src/auth.ts` (absolute base),
    `src/authFlow.ts` (absolute callback base), `vercel.json`.
  - Test: `src/config.test.ts`, `src/authFlow.test.ts`.

**Interfaces:**
- `resolveUrls(env: {VITE_API_URL?, VITE_EVENTS_URL?, DEV: boolean}) -> {api: string, events: string}`:
  - `api` is `VITE_API_URL`, else `"http://localhost:8000"` in development, else `""` (same origin).
  - `events` is `VITE_EVENTS_URL`, else `api`.
  - Trailing slashes are removed.
- `absoluteBase(apiUrl, origin) -> string`: `apiUrl || origin`.

- [ ] **Step 1: Failing backend test.** The `serve` command calls `uvicorn.run(..., host="0.0.0.0", port=8080)`
  when `PORT=8080`, and `host="localhost", port=8000` without it. `uvicorn.run` is monkeypatched.
- [ ] **Step 2: Implement `serve`.**
  `port = a.port or int(os.environ.get("PORT", 8000))`; `host = "0.0.0.0" if os.environ.get("PORT") else "localhost"`.
- [ ] **Step 3: Container files.**
  - `requirements.txt`: the direct dependencies, pinned to the local versions:
    ```
    contree-sdk==0.3.6
    contree-client[httpx]==0.4.0
    deepagents==0.7.15
    langchain-openai==1.4.1
    langchain-tavily==0.2.11
    langsmith==0.13.0
    datasets==3.6.0
    python-dotenv==1.2.3
    fastapi==0.141.1
    uvicorn==0.52.4
    sse-starlette==3.4.11
    psycopg[binary]==3.3.5
    psycopg-pool==3.3.1
    httpx==0.28.1
    PyJWT==2.14.0
    cryptography==50.0.1
    openai==2.54.0
    ```
  - `Dockerfile`: `python:3.12-slim`, `pip install --no-cache-dir -r requirements.txt`, copy `receipts/`,
    `migrations/` and `scripts/`, then
    `RUN python -c "from receipts import swebench; swebench._dataset()"` to bake the SWE-bench cache.
    `CMD ["python", "-m", "receipts", "serve"]`.
  - `.dockerignore`: `.env`, `*.pem`, `runs/`, `.cache/`, `tests/`, `docs/`, `.superpowers/`, `__pycache__/`,
    `.git/`.
- [ ] **Step 4: Failing frontend tests.**
  - `resolveUrls` covers development, production same-origin, explicit values and trailing slashes.
  - `socialCallbacks("/app", "https://r.vercel.app", "")` returns `https://r.vercel.app/api/auth/complete?...`.
- [ ] **Step 5: Implement `config.ts`, use it in `api.ts`, and make `auth.ts` and `socialCallbacks` absolute.**
- [ ] **Step 6: Write `vercel.json`.**

```json
{
  "rewrites": [
    { "source": "/api/:path*", "destination": "https://CLOUD_RUN_URL/api/:path*" },
    { "source": "/(.*)", "destination": "/index.html" }
  ],
  "headers": [{ "source": "/api/(.*)", "headers": [{ "key": "x-vercel-enable-rewrite-caching", "value": "0" }] }]
}
```

`CLOUD_RUN_URL` is replaced with the real host after the first Cloud Run deploy. This is the one value that
can't be known before it; the deploy guide makes it step 1 on Vercel.

- [ ] **Step 7: Run both suites and the build, then commit in each repository.**

### Task 7: Validate the demo cases live

- [ ] **Step 1: Check that the wrong patches apply.** In a base sandbox of each instance (a short script using
  `sandbox.apply_patch`), the patch must give a non-`None` image.
- [ ] **Step 2: Run each of the six cases once.** Use `python -m receipts run <instance> --patch
  gold|none|receipts/demo_patches/<file>.diff`.
  - Expected: real fix PROVEN; empty patch REFUTED or UNPROVEN; wrong patch REFUTED or UNPROVEN, never
    PROVEN.
  - A real fix that isn't PROVEN, or any PROVEN on an empty or wrong patch, removes the case.
- [ ] **Step 3: Record the results** (verdict, seconds, tokens) in the spec's Results section. Keep only the
  passing cases in `demo_cases.json`, then commit.

### Task 8: Docs

- [ ] **Step 1: Backend README:**
  - remove the Daytona text (Task 3);
  - add "Demo without sign-in" (the endpoints and caps);
  - add "Deploy (Cloud Run)": the commands, settings and secrets;
  - name Vercel as the frontend host.
- [ ] **Step 2: Frontend README:**
  - add "Deploy (Vercel)": project import, `VITE_EVENTS_URL`, replacing `CLOUD_RUN_URL` in `vercel.json`;
  - explain why `/api` goes through Vercel and the event stream goes direct.
- [ ] **Step 3: `RECEIPTS-ARCHITECTURE.md`:** hosting, the demo, Daytona gone, the Phase 1 status.
- [ ] **Step 4: Commit.**

### Task 9: Verify, push, hand over

- [ ] **Step 1: Full suites and build.**
  - Backend: `python -m pytest -q`.
  - Frontend: `npx vitest run && npm run build`.
- [ ] **Step 2: Review the whole diff.**
  - No secrets.
  - No leftover `daytona` or `SANDBOX_PROVIDER` outside `docs/`.
  - `vercel.json` placeholder present.
- [ ] **Step 3: Push both repositories.** `git push origin main`.
- [ ] **Step 4: Write the deploy guide for the owner:**
  - **Google Cloud:**
    1. `gcloud auth login`, choose the project, enable billing.
    2. Enable the APIs (Cloud Run, Cloud Build, Artifact Registry, Secret Manager).
    3. Create the secrets.
    4. `gcloud run deploy`, with every flag.
    5. Read the service URL.
    6. Continuous deploy from GitHub.
  - **Vercel:**
    1. Put the Cloud Run URL in `vercel.json`.
    2. Import the repository.
    3. Set `VITE_EVENTS_URL`.
    4. Deploy.
  - **Then:**
    1. Set `FRONTEND_URL` on Cloud Run.
    2. Neon Auth trusted domain.
    3. GitHub App setup and webhook URLs.
    4. Smoke checks.
