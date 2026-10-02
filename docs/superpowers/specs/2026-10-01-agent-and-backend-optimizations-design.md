# Agent and backend optimizations: design

Date: 1 October 2026. Status: proposed, waiting for review.

## Goal

Make a check faster and cheaper without changing what a verdict means, and trim the backend's deploy weight.
The verdict rules, the asymmetry rule (anything uncertain is Unproven, never Refuted) and the writer's blindness
(issue and unpatched repository only, never the pull request) do not change.

### What the data says (before)

From 21 saved receipts and LangSmith traces of the live checks on 1 October:

- The blind test writer is 70 to 90 % of every check's time and tokens. A check takes 5 to 17 agent turns; each
  turn is about 1 to 2 s of model time plus 1.5 to 3 s of sandbox command. Input dominates: about 100K tokens in
  and 2K out for a 17-turn check, growing from 4.4K to 10K tokens per turn.
- Every turn re-sends the tool definitions: 12,342 characters (about 3,100 tokens) for 10 tools, against 2,515
  characters (about 630 tokens) of system prompt. `task` (spawns a sub-agent that our exploration budget does not
  cover) and `delete` are never needed by a writer of one test file.
- Before the agent starts, classification (3 to 7 s), the research brief, the stated cases and the writer's
  workspace run one after another.
- Pull request checks rebuild the repository's environment (download and `pip install`, 30 to 40 s) whenever the
  process restarted; Render's free tier sleeps when idle, so most cold checks pay it.
- The Docker image installs `datasets` (with pyarrow and pandas), which only the build step uses.

### Success criteria

1. Writer overhead per turn (system prompt plus tool definitions, measured offline with the same script as
   above) drops from about 3,700 tokens to at most 2,600.
2. A second check on the same issue and base reuses the blind test: no writer tokens, and on the demo it reaches
   a verdict in under 45 s (from 90 to 160 s today).
3. After a restart, a pull request check on an already built base commit skips the environment build.
4. The runtime image has no `datasets`, `pyarrow` or `pandas`.
5. All existing backend (230) and frontend tests pass, with new tests for each change; the six demo cases give
   the same verdicts as in their Phase 1 validation.

### Not in this round

Model changes, verdict rule changes, the 50-issue evaluation harness (Phase 2), the issue race (Phase 3), and
cleaning up old sandbox images.

## 1. Reuse the blind test

The test depends only on the issue and the unpatched code, never on the pull request, so it can be reused by any
check of the same issue at the same base. This is the roadmap's Phase 2 "blind-test caching per issue".

**Key.** `sha256(repo, base, issue text)`, where base is the instance id for SWE-bench (its image fixes the
commit) and the merge-base SHA for a pull request. A pull request without a linked issue uses its own title and
body as the claim, so it only matches itself.

**Storage.** Migration `003_blind_tests.sql`:

```sql
CREATE TABLE blind_tests (
  key        text PRIMARY KEY,   -- sha256 of repo, base and issue text
  repo       text NOT NULL,
  run_id     text NOT NULL,      -- the check whose writer wrote the test
  test_code  text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

`PgRuns` and `MemoryRuns` gain `get_blind_test(key)`, `save_blind_test(key, repo, run_id, test_code)` (first one
wins: `ON CONFLICT DO NOTHING`) and `forget_blind_test(key)`.

**Flow in `engine.check`.** The store and the run id are passed in by the server (`checks._execute`). The CLI and
`scripts/eval_prs.py` pass nothing, so evaluations always measure the writer.

1. Look up the key before classification.
2. Hit: once the environment is ready, run the stored test once on the base. If it still fails with an
   assertion (`repro_check`), it is the blind test: no research, no writer. If it doesn't, forget the key and
   write a new test as today.
3. Miss: everything as today. At the end, save the accepted test only if all base runs reproduced the same
   failure and no second opinion doubted the test.
4. A reused test that a second opinion doubts is forgotten, so it is not reused again.

**Evidence and live events.** `ev["writer"]` becomes `{"test_code", "attempts": 0, "reused_from": <run id>,
"reason": "reused", "tool_log": [], "submissions": []}` and the stream sends `test_reused {"from": <run id>}`
before `test_accepted {"attempts": 0, "reused_from": <run id>}`.

**Receipt (frontend).** The blind test line reads "reused" with the note "written from the issue alone in an
earlier check"; the evidence section says the same above the test code, with a link to the earlier receipt
("See how it was written"); the run page's progress step reads "reused from an earlier check".

**Concurrency.** Two checks of the same issue at once both miss and both write; the first save wins.

## 2. Leaner writer turns, with skills as middleware

The writer stops calling `create_deep_agent` and composes Deepagents' own middleware with LangChain's
`create_agent`, so it gets exactly what it needs:

- `FilesystemMiddleware(tools=["ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"],
  custom_tool_descriptions=...)`: no `task`, no `delete`, and one- or two-sentence tool descriptions.
- `SkillsMiddleware` with a compact template (a short header plus the list), not the stock one, which adds about
  500 tokens of generic instructions per turn.
- `PatchToolCallsMiddleware` and our `ExplorationBudget`, as today.

**Skills.** `receipts/skills/<name>/SKILL.md` with `name` and `description` front matter. The agent's backend
becomes a `CompositeBackend`: `/skills/` is served from that folder on the server (`FilesystemBackend`,
read-only use), everything else goes to the sandbox as today. Reading a skill is therefore not a sandbox
command, doesn't count against the look-around budget, and is recorded in `writer.skills_read`. Skills are
generic guidance; none of them knows anything about a pull request.

The always-on prompt keeps the core rules (work order, budget, blindness, assertion failures only, test only
what the issue states, cover every case, keep it small). Guidance that only some issues need moves to skills:

| Skill | When it applies |
|---|---|
| `exception-bugs` | the bug is an exception: catch it and fail with an assertion |
| `expected-values` | the issue writes an expected result as code: build it from that code, never retype printed output |
| `sympy` | sympy issues: `evaluate=False`, assumptions, exact values, printers |
| `arrays` | numpy, pandas or xarray results: the testing helpers that raise `AssertionError`, attrs and copies |
| `requests` | requests issues: build and prepare a request, never send it |
| `plotting` | matplotlib or seaborn issues: the Agg backend, no display, close figures |

**What this does to prompt size, measured honestly.** Our prompt is already small. Moving the two situational
rules out saves about 200 tokens, and the compact skills list costs about 250, so skills alone are roughly
neutral. Their value is that library know-how can grow without being sent on every turn. The cut comes from the
tools: dropping `task` and `delete` saves about 650 tokens per turn and the short descriptions save about 1,000
more. The offline measurement before and after goes in the results.

**Overlapped setup.** When there is no reusable test, the research brief, the stated cases and the writer's
workspace (a copy of the base without `.git`) start alongside classification instead of after it; a claim that
isn't a bug fix cancels them. The writer's one retry reuses the stated cases instead of asking again.

**Risk.** Shorter tool descriptions could make the writer use its tools worse. The six demo cases are the check
(criterion 5); if they regress, the descriptions go back to the stock ones.

## 3. Environments survive restarts

`RepoTarget.base_image()` keeps its in-process cache and adds a second level in Nebius Sandboxes itself: after
building, it tags the image `receipts-env/<owner>--<repo>:<base sha>`, lowercased (`tag_as`). On an
in-process miss it first tries `images.use(tag, strict=True)`; on a hit, one command lists the test files
(`find`), so neither the tarball nor `pip install` is needed. Any error in the tag path falls back to building as
today; the cache can make a check faster, never fail it. The tag format and the `use` lookup are tried against
the real service with a small image before the code relies on them.

## 4. Lean image and small cleanups

- Two-stage `Dockerfile`: the first stage installs `datasets` and writes `.cache/swebench_verified.json`; the
  runtime stage installs `requirements.txt` without `datasets` and copies only that file. `requirements.txt`
  keeps `datasets` for local development, where the cache may still need building.
- `/api/demo` makes one database query instead of two: today's count comes from the recent-runs list it already
  fetches (the daily cap, 20, is below the 50 rows fetched; the code takes `max(50, DEMO_RUNS_PER_DAY)`).
- The research brief's two searches run at the same time.
- The README's setup line becomes `pip install -r requirements.txt`.

## Error handling

Every new path degrades to today's behaviour: a failed cache lookup or save is logged and the writer runs; a stale
cached test is forgotten and a new one is written; a failed tag lookup builds the environment; skills that fail to
load leave the writer without skills. None of them can produce a verdict on its own.

## Testing

- Backend unit tests: the key; a cache hit skips research and the writer and records `reused_from`; a stale hit
  falls back to the writer; save only after consistent base runs and no doubting opinion; a doubted reuse is
  forgotten; the CLI path never reuses; `MemoryRuns` blind-test methods; the writer's tools (no `task`, no
  `delete`) and the skills list; skill reads exempt from the budget; overlapped setup cancelled by a non-fix
  claim; the tag hit and fallback with a fake Sandboxes client; the single-query demo overview; concurrent
  research searches.
- Frontend unit tests: the receipt reads `reused_from` from both live events and stored evidence.
- Offline: the token measurement script before and after (criterion 1).
- Live, on real services: the six demo cases once each, with the same issue run twice to show reuse; one pull
  request check on `receipts-demo-sympy` twice across a restart to show the environment tag. This spends Token
  Factory credit and sandbox time and is confirmed before it runs.

## Rollout

1. Apply migration 003 to Neon with `python -m receipts migrate` (additive: one new table), confirmed first.
2. Push the backend; Render rebuilds the smaller image and restarts.
3. Push the frontend; Vercel deploys the reuse display.
4. Record the measured results at the end of this document.

## Results (2 October 2026)

**Writer overhead per turn** (system prompt plus tool definitions, offline, stub model): 14,857 characters with
10 tools before; 9,898 characters with 8 tools after (-33%, about 3,700 to about 2,450 tokens). `task` and
`delete` are gone; the skills list adds about 1,000 characters.

**Credit, the six demo cases, writer only** (no reuse; Token Factory list prices from
tokenfactory.nebius.com/model-catalog.md: Nano $0.06/$0.24, Super $0.30/$0.90, Ultra $1/$3 per million tokens
in/out; `scripts/usage_report.py`):

| | Before (Phase 1 runs, 1 Oct) | After (2 Oct) | Change |
|---|---|---|---|
| Tokens, six checks | 355,938 | 169,862 | -52% |
| Model cost, six checks | $0.139 | $0.080 | -42% |
| Per check | 59.3K tokens, $0.023, 58 s | 28.3K tokens, $0.013, 48 s | |
| Writer (Super) input tokens | 299,287 | 119,366 | -60% |
| Verdicts | 2 PROVEN, 4 REFUTED | the same six | |

The writer read no skill in these six (simple issues; it used execute, ls, write_file and submit_test only), so
the saving comes from the leaner tools: smaller definitions on every turn and fewer turns (19 tool calls across
the six checks, against 40 shell commands before). Six runs per side is a small sample; model runs vary.

**Reuse** (local API on Neon): xarray 4629's real fix wrote its test (PROVEN, 42.9 s, 30.8K tokens); the empty
patch then reused it: REFUTED in 18.9 s with 7.7K tokens and $0.004 (Phase 1: 49.7 s, 55.2K tokens, $0.022),
no writer model call; the second opinion still ran, as Refuted requires. The receipt reads "Blind test: reused"
and links to the check that wrote it.

**Kept environments**: the same pull request (receipts-demo-sympy #30) checked in two processes; the first built
the environment (ready at 38.0 s), the second found it by tag (ready at 7.0 s). Both PROVEN.

**Not measured here**: the Docker image size (no Docker locally; Render builds it on deploy).
