# Current State

## Version
MVP v0.1 foundation

## Built
- Docker Compose foundation
- FastAPI service
- PostgreSQL schema
- Opportunity scoring
- Telegram notification integration
- Obsidian Company Brain structure
- Git repository initialized and pushed to `koopzeker-ux/ai-venture-studio` (main)
- `CLAUDE.md` governance document (autonomy boundaries, security, git
  workflow, cost budgets, human approval gates, research integrity,
  market-intelligence scope)

## Not built yet
- Source collectors
- AI model router
- Researcher/Critic model calls
- MCP gateway
- Experiment runner
- Mission Control UI
- Production VPS hardening

## Next milestone
M2.1 — first real Reddit signal collector (planned, see below).

## Milestone M1 — COMPLETE (2026-08-20)
Goal: Docker -> PostgreSQL -> FastAPI -> opportunity scoring -> Telegram
alert fully working end-to-end. **Achieved and verified against the real
stack**, not just at the test level.

Parallel work via Git worktrees (`../avs-builder`, `../avs-intelligence`,
`../avs-reviewer` on branches `agent/builder`, `agent/intelligence`,
`agent/reviewer`) with disjoint file ownership. Status per track:

- BUILDER: infra reviewed, no code changes needed. Could NOT actually
  run/verify the Docker Compose stack — Docker/WSL is not installed on
  this Windows machine. Nothing merged (no diff to merge).
- INTELLIGENCE (merged, commit `f7f1330`): opportunity create/list/score
  API flow verified end-to-end at the code layer via FastAPI TestClient
  + in-memory SQLite, plus scoring edge-case tests (clamping, missing
  factors, score/evidence_confidence independence). 9 tests.
- REVIEWER (merged, commit `1553229`): fixed `send_telegram_message()`
  to catch `httpx.HTTPError` and return `False` (logged) instead of
  raising, so a Telegram outage can no longer break the scoring
  endpoint. Added Telegram unit tests + an alert-flow integration test
  suite. 9 tests.

Integration: both branches merged into `main` by LEAD (merge commits
`e6f10db`, `1c36ad3`, final `1c36ad3`). One real conflict, in
`backend/tests/conftest.py` (both agents independently added the same
shared TestClient/SQLite fixture) — resolved by hand-merging the two
versions (explicit model import + drop_all/try-finally teardown from
the reviewer version). No other files overlapped. Full backend test
suite after merge: **18 passed, 0 failed**. Diff scanned for secrets
before push — none found (only fake test tokens like `"123:abc"`).

**Real end-to-end verification (2026-08-20, after Docker Desktop + WSL2
were installed and confirmed working by the user):**
- Telegram bot token configured in local `.env`; original token was
  briefly exposed in a local tool-output during setup due to a shell
  `.env`-parsing bug and was immediately rotated via BotFather as a
  precaution before any further use. New token verified via Telegram
  `getMe`. Chat id resolved via `getUpdates` (exactly one private chat
  found) and written to local `.env`. Neither the token nor the chat id
  were ever committed, logged to this repo, or shown in any report.
- `api` container force-recreated (db untouched) so the new env vars
  loaded; `docker compose ps` showed both `db` (healthy) and `api` (up);
  `GET /api/health` returned `{"status":"ok"}`.
- A real opportunity was created and scored via the live API
  (`POST /api/opportunities`, `POST /api/opportunities/{id}/score`)
  with factors/evidence_confidence set to clear the alert threshold.
  Response: `score: 100`, `telegram_alert_sent: true`. Verified this
  ran through the actual container (via `docker compose logs api`,
  clean 200s, no errors) — not a standalone Python call outside Docker.
- Data confirmed persisted in the real PostgreSQL container via
  `docker compose exec db psql` (not SQLite/in-memory).
- Full chain now proven live: Docker -> PostgreSQL -> FastAPI ->
  Opportunity -> Scoring -> Telegram.

**Known gap, not required for M1:** `scripts/smoke_test.sh` was not
re-run in this final pass (health endpoint was checked directly
instead); worth running once as a formality.

LEAD performed no feature work during M1 beyond docs/config and
conflict resolution; LEAD reviewed both branches, confirmed scope was
respected, resolved the one merge conflict, merged after the full test
suite passed, and then verified the live end-to-end chain before
marking M1 complete.

## Milestone M2.1 — IMPLEMENTATION INTEGRATED, REVIEWER VALIDATION PENDING (2026-08-20)
Not complete yet. BUILDER's and INTELLIGENCE's work is merged into
`main` and the full backend test suite passes, but REVIEWER has not
yet produced the end-to-end/resilience tests, and the pipeline has not
been verified against the live Docker/PostgreSQL stack.

**Integrated:**
- `agent/builder` (commit `4417b34`, merge `1f8485b`): Hacker News
  connector (Algolia HN Search API, no auth) + generic RSS/Atom
  connector (fetch via `httpx`, parse via `feedparser`) + generic
  multi-connector CLI entrypoint (`run_collectors.py`) + non-secret
  config (`HACKERNEWS_ENABLED`, `RSS_ENABLED`, `RSS_FEED_URLS`).
- `agent/intelligence` (commit `a4fd742`, merge `98d2fb1`):
  source-agnostic `normalize.py` + `candidate_filter.py` (keyword OR
  engagement-threshold triggers) + `pipeline.py` (dedupe via a DB
  unique constraint on `Signal.source_url`, then `Opportunity`+
  `Evidence` creation for matches). Proven source-agnostic in tests
  using fictitious source names, not tied to hackernews/rss.
- Both merges were clean fast, no-conflict 3-way merges (each agent's
  own commit only touched files in their assigned scope — the large
  diffs seen when comparing branch-vs-current-main were a staleness
  artifact from branching off an older `main`, not actual edits to
  M1/REVIEWER files; verified before merging by diffing each commit
  against its own parent).
- Full backend pytest suite after merge: **42 passed, 0 failed** (18
  from M1 + 24 new: candidate_filter, normalize, pipeline_dedupe).
- Diff scanned for secrets: none found. No Reddit code. No new secrets
  or paid providers introduced.

**Known gap found during integration review, blocking live
verification:** the live PostgreSQL `signals` table (already created
during M1) does **not** yet have the new unique constraint on
`source_url` — `Base.metadata.create_all()` only creates missing
tables, it does not alter existing ones. Until this is applied (no
Alembic yet — either a manual, reviewed `ALTER TABLE signals ADD
CONSTRAINT ... UNIQUE (source_url)` on the local dev DB, or a fresh
`db` volume), the live dedupe behavior will not actually work even
though all tests pass against a fresh in-memory SQLite schema.

**What REVIEWER should test:**
- End-to-end pipeline test mixing both connectors' fixtures through
  one `process_raw_signals()` call (objective proof of
  source-agnosticism), plus resilience tests for `hackernews.py` and
  `rss.py` (mocked HTTP only, no real network calls) — per the M2.1
  task prompt.
- The M1 regression check: score a pipeline-created candidate above
  threshold via the live endpoint and confirm `telegram_alert_sent`
  still works unchanged.
- Live verification against Docker: apply the `signals.source_url`
  unique constraint to the running dev database first (see gap above),
  then run `python -m app.collectors.run_collectors` inside the `api`
  container and confirm real Signal/Opportunity/Evidence rows appear
  with correct provenance, and that a second run does not duplicate.

M2.1 will only be marked complete after REVIEWER's validation and a
live run against the real stack, per the project owner's instruction.

## Milestone M2.1 planning history (superseded, revised 2026-08-20)
Goal: first real vertical slice of Market Intelligence — collect real
signals from source-agnostic connectors, normalize/dedupe them, and
turn matching ones into opportunity candidates (status `discovered`)
with attached evidence, reusing the existing M1 scoring + Telegram-
alert pipeline unchanged.

**Revised scope (superseding the original Reddit-only plan below):**
the Reddit-only design was corrected after review of Reddit's Data API
Terms — commercial use may require a separate agreement, which AI
Venture Studio's opportunity-discovery purpose likely triggers. Reddit
is now an optional future connector, gated behind explicit confirmed
permission for our use case; it is NOT part of M2.1.

M2.1 sources instead: **Hacker News** (official Firebase API / Algolia
HN Search API — free, no auth, no commercial-use restriction found)
and a **generic RSS/Atom connector** (seeded with Product Hunt's
official public feed). Both require zero new secrets. Google Trends
(no public self-serve API, only unofficial scraping — excluded) and
YouTube Data API (ToS ambiguity around indefinite storage of API data
for a research database — deferred pending explicit compliance review)
were researched and excluded for now; see Decision Timeline for detail.

The core pipeline (normalize -> dedupe -> candidate detection ->
Opportunity/Evidence) is now explicitly source-agnostic: it only
consumes a generic raw-signal contract and must contain no
connector-specific logic, so additional sources (RSS feeds, and later
Reddit/YouTube once cleared) can be added without touching it. No
LLM-based auto-scoring yet — candidates stay `score = NULL` until a
human scores them via the already-built `/opportunities/{id}/score`
endpoint.

Work split (parallel, disjoint file ownership, same worktree pattern
as M1 — BUILDER/INTELLIGENCE/REVIEWER push to their `agent/*` branches,
no self-merge to `main`):
- BUILDER: Hacker News client + generic RSS/Atom client + generic
  multi-connector CLI entrypoint + config (no secrets needed).
- INTELLIGENCE: source-agnostic normalize + dedupe (DB-level unique
  constraint) + generic candidate-detection (keyword trigger OR
  engagement-threshold trigger) + candidate/evidence creation.
- REVIEWER: end-to-end pipeline tests mixing fixtures from both
  connectors through the same pipeline call (proves genericity) +
  resilience tests for both clients (mocked, no real network calls) +
  regression check that M1's scoring/Telegram path still works
  unchanged on a resulting candidate.

Full revised plan (architecture, source research, acceptance criteria,
exact agent prompts) given to the project owner for review; nothing
implemented yet.
