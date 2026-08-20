# Current State

## Milestone M3.1 — IMPLEMENTATION INTEGRATED, INDEPENDENT REVIEWER VALIDATION PENDING (2026-08-20)
Not complete yet. Goal (M3.1 only — first slice of the "Opportunity
Research & Prioritization Engine" milestone): replace the M2.2 volume
cap's first-come-first-served ordering with deterministic,
commercial-priority pre-ranking. No LLM in this slice — see the M3
plan for the fully-designed but not-yet-approved M3.2 (Researcher) and
M3.3 (Critic + automatic Evidence Confidence + Telegram gate).

Trigger: live M2.2 data showed all 20 real candidates were pure
`traction_signal` (viral historical news — "Steve Jobs has passed
away", "Stephen Hawking has died" — zero commercial signal), and which
20 of 30 traction-qualifying signals got in was pure luck of Algolia's
response order, not quality.

**Integrated (2026-08-20):**
- `agent/builder` (commit `056b459`, merge `fd03a8a`): every collector
  run now persists exactly one `AgentRun` row (existing schema fields
  only — no migration), and a failed pipeline run is logged and
  reported instead of crashing. Added a read-only
  `GET /api/opportunities/{id}` returning the opportunity plus its
  full evidence list (needed so a human can review evidence before
  scoring — will matter more once M3.2's Researcher exists). No
  existing routes changed.
- `agent/intelligence` (commit `945402f`, merge `bd220e2`): added
  `compute_pre_rank_score()` (signal-diversity weighting + purchase-
  intent/alternative-seeking/pain-point bonuses + a log-scaled,
  **capped** engagement bonus so raw virality can't dominate) and
  restructured `process_raw_signals()` into two phases — collect all
  gate-passing candidates first, then sort by
  `(-pre_rank_score, source_url)` before applying the volume cap.
  Proven input-order-independent (3 shuffled orderings of the same
  candidate set produce identical selections) and proven that a
  modest-engagement (60) purchase-intent+traction candidate outranks a
  pure-traction candidate at engagement=6015 — the exact real M2.2
  scenario, reproduced as a test. An explicit test confirms
  `compute_pre_rank_score` never writes to `Opportunity.score` or
  `evidence_confidence`.
- Both merges clean, no conflicts (each commit verified against its
  own parent — both were based on current `main`, no staleness this
  round).
- Full backend pytest suite after merge: **109 passed, 0 failed** (92
  existing + 17 new ranking/detail-endpoint tests).
- Diff scanned for secrets: none found. No schema migration, no LLM
  code, no new dependency, no new secret.

**What's still open:** REVIEWER has not yet produced independent
validation for the pre-ranking fix or the new detail endpoint. No live
collector run against Docker/PostgreSQL has been done for M3.1 yet —
not required before REVIEWER's validation.

## Milestone M2.2 — COMPLETE (2026-08-20)
REVIEWER integrated (commit `da5e2e5`, merge `b623e01`), full suite
green (92/92), and — for the first time — verified live against real
Hacker News + Product Hunt data: the promotion gate correctly produced
**20 real Opportunity candidates** (all via `traction_signal`, e.g.
"Steve Jobs has passed away", "backdoor in upstream xz/liblzma",
"CrowdStrike update: Windows bluescreen"), while `product_launch_signal`
correctly promoted **zero** signals on its own out of the real batch
(8 HN `is_launch=True` signals, 50 Product Hunt signals). Full detail
below. M1's scoring -> Telegram path confirmed intact via a live,
real alert send. Genuine milestone close, not carried forward.

## Milestone M2.2 — integration history (superseded by COMPLETE above)
Not complete yet. Goal: turn the 0/80-candidate M2.1 result into real,
precision-first candidate detection without flooding the pipeline —
see the M2.1 diagnosis below for why 0/80 happened.

**Plan correction before implementation:** the first M2.2 draft let a
bare `product_launch_signal` promote to an Opportunity on its own.
Rejected before any code was written — Product Hunt's RSS feed marks
~50/80 live signals as `is_launch=True`, which would have flooded the
pipeline with candidates purely because products exist, not because
there's real evidence of demand. Also evaluated and **deferred**:
automatic evidence-enrichment via Jaccard title-overlap — a live test
against realistic short PH/HN-style titles showed 4 of 5 unrelated
title pairs crossing a 0.5 similarity threshold purely on shared
stopwords ("for", "the new", "ai", "api"), a real false-merge risk on
this exact kind of data. Not built.

**Integrated (2026-08-20):**
- `agent/builder` (commit `0d1ee1a`, merge `f472762`): `hackernews.py`
  now runs two independent Algolia queries — the existing
  `search_by_date` (recency) plus a new `search` with
  `numericFilters=points>50` (traction) — each error-handled
  separately, combined and deduplicated on `objectID` within the call.
  Both connectors now set a generic `metadata.is_launch: bool` (HN:
  only on a `"Show HN:"`/`"Launch HN:"` title prefix; RSS: always
  `True`). Only `hackernews.py` and `rss.py` touched — verified against
  its own parent commit, not against a stale `main` diff.
- `agent/intelligence` (commit `2c7205f`, merge `8fef6c3`): new
  `purchase_intent_signal` and `alternative_seeking_signal` triggers;
  a `STRONG_EVIDENCE_TYPES` classification that deliberately excludes
  `product_launch_signal` — promotion to an Opportunity now requires
  at least one strong trigger (`pipeline.passes_candidate_gate`); the
  existing volume cap (`MAX_NEW_OPPORTUNITIES_PER_RUN`, default 20)
  becomes an explicit secondary safety net, not the primary filter
  (`candidates_skipped_cap` reported separately). Verified: `pipeline.py`
  / `candidate_filter.py` / `normalize.py` still contain zero
  connector-specific logic.
- Both merges were clean, no git conflicts. **Two of REVIEWER's own
  M2.1 tests broke** as a side effect of these additive changes (not a
  functional regression): `test_hackernews_resilience.py` referenced
  the old single-query constant name and asserted a single fixed query
  URL; `test_collector_pipeline_e2e.py` asserted exact dict equality on
  `process_raw_signals()`'s return value, missing the new
  `candidates_skipped_cap` key. LEAD repaired both minimally (same
  assertions, updated to the new-but-compatible shape) rather than
  push with a red suite — documented in commit `94d83c5` for REVIEWER
  to see exactly what changed and why.
- Full backend pytest suite after merge + repair: **86 passed, 0
  failed** (60 existing + 10 new gate/trigger tests + 16 from expanded
  normalize/candidate_filter/dedupe coverage).
- Explicitly verified with named, passing tests: launch-only signal ->
  0 Opportunities (`test_launch_only_signal_creates_no_opportunity`);
  launch + a strong trigger -> 1 Opportunity with both Evidence types
  (`test_launch_plus_strong_signal_creates_one_opportunity_with_both_evidence`);
  purchase-intent and alternative-seeking each promote alone
  (`test_each_strong_type_can_promote_alone[...]`); volume guardrail
  caps correctly and reports skips
  (`test_volume_guardrail_caps_opportunities_and_reports_skipped`).
- Diff scanned for secrets: none found. No Reddit/YouTube/Google
  Trends, no LLM code, no new dependency, no new secret.

**REVIEWER integrated** (commit `da5e2e5`, merge `b623e01`): tests
only, no production code touched. Independent proof that a bulk
20-item Product-Hunt-shaped launch-only batch produces zero
Opportunities; that launch+traction and launch+pain each promote with
both Evidence types attached; that a low-traction "Show HN:" post
stays unpromoted; that the volume cap behaves correctly; and
independent confirmation that LEAD's `94d83c5` test-compatibility fix
was correct. Full suite after this merge: **92 passed, 0 failed**.

**Live verification against the real Docker/PostgreSQL stack
(2026-08-20):**
- `api` image rebuilt and container recreated with the M2.2 code;
  `docker compose ps` showed both services up, `db` healthy;
  `GET /api/health` OK; PostgreSQL reachable directly.
- Ran `python -m app.collectors.run_collectors` for real: **110 raw
  signals** (60 Hacker News — both the recency and the new
  points-filtered traction query, already deduplicated on `objectID`
  within the connector — + 50 Product Hunt/RSS). Of these, **62 were
  genuinely new** signals (60 HN, 2 RSS — the other 48 RSS items were
  already stored from the earlier M2.1 run, correctly deduplicated by
  `source_url`).
- **30 of the new Hacker News signals** had `engagement_score >= 50`
  (the traction query doing exactly what it was built for). **8 new HN
  signals** had `is_launch=True` (`"Show HN:"` titles); **all 50**
  Product Hunt signals had `is_launch=True`.
- **Promotion gate result: 20 real Opportunities created**, every one
  via `traction_signal` alone (real, high-engagement HN classics —
  "Steve Jobs has passed away" (4338 pts), "OpenAI's board has fired
  Sam Altman" (5771 pts), "backdoor in upstream xz/liblzma" (4549
  pts), "CrowdStrike update: Windows bluescreen and boot loops" (4489
  pts), among others). **Zero** of the 8 launch-flagged HN signals and
  **zero** of the 50 Product-Hunt-launch signals promoted on
  `product_launch_signal` alone — confirmed correct, not a detection
  gap: their content held no pain/purchase-intent/alternative-seeking
  language and (for the 7 ordinary Show HN posts) too little
  engagement.
- **Notable edge case, correctly handled but worth recording:** one
  "Show HN:" post ("This up votes itself") had genuinely gamed its way
  to 3531 points — both `product_launch_signal` and `traction_signal`
  fired on it, and it was eligible to promote. It did **not** get an
  Opportunity — not a gate failure, but the volume cap
  (`MAX_NEW_OPPORTUNITIES_PER_RUN=20`): 30 signals qualified via
  traction this run, only the first 20 processed were promoted
  (`candidates_skipped_cap = 10`), and this one landed just after the
  cap by processing order. The cap is documented as first-come-
  first-served, not score-ranked — this is expected behavior of that
  design, not a bug, and no threshold/logic was changed to "fix" it.
- **Dedupe proven live again:** re-ran the collector immediately after
  — still exactly 142 signals and 22 opportunities (2 pre-existing
  from M1 + 20 new), zero growth on either.
- **M1 regression, live:** inspected full provenance/evidence for one
  real candidate (correct source_url, evidence_type, confidence=0.3,
  independently_confirmed=false, thesis correctly labeled as
  unverified triage). Scored it manually via the live API — below
  threshold, no alert (correct). Scored a second real candidate with
  maxed factors as a deliberate mechanical test of the alert path —
  `score: 100`, `evidence_confidence: 80`, **`telegram_alert_sent:
  true`**, confirmed via `docker compose logs api` (clean 200 OK, no
  errors). M1's scoring/Telegram pipeline is unaffected by M2.2.
- No Reddit/YouTube/Google Trends, no LLM, no threshold/phrase tuning,
  no new secrets or dependencies.

M2.2 completion criteria (all met): REVIEWER integrated; full suite
green; live collectors ran for real; live promotion gate proven
correct (including the cap edge case); dedupe proven live twice now;
M1 scoring/Telegram intact.

---

## Milestone M2.1 diagnosis (context for the above, 2026-08-20)
Trigger: the live M2.1 run collected 80 real signals (30 Hacker News,
50 Product Hunt/RSS) but produced 0 candidates. Diagnosis (backed by
inspecting the real stored rows): (1) `hackernews.py` used Algolia's
`search_by_date` (recency-sorted), so points were 1-12 against a
traction threshold of 50 — structurally can't fire; (2) RSS/Product
Hunt entries are short marketing taglines with a fixed "Discussion |
Link" suffix, not organic complaint language, so the keyword triggers
structurally can't match, and RSS signals never carry an
`engagement_score` so the traction trigger can't fire either — RSS
signals could not produce a candidate under any circumstance in the
M2.1 design.

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

## Milestone M2.1 — COMPLETE (2026-08-20)
BUILDER, INTELLIGENCE and REVIEWER all merged; full test suite green;
verified live against the real Docker/PostgreSQL stack with real
Hacker News + Product Hunt (RSS) data.

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

**REVIEWER integrated** (commit `e0f6ca4`, merge on top of the above):
tests only, no production code touched — an end-to-end test proving
Hacker News- and RSS-shaped signals go through one `process_raw_signals()`
call via the identical code path (including a dedupe edge case: same
`source_url` claimed by two different `source` values still dedupes
correctly, proving dedupe keys off `source_url` not `source`), a
regression test that an M2.1-created Opportunity still flows through
M1's scoring endpoint to `telegram_alert_sent: true`, and mocked-HTTP
resilience tests for both `hackernews.py` and `rss.py`. Full suite
after this merge: **60 passed, 0 failed**.

**Live verification against the real Docker/PostgreSQL stack
(2026-08-20):**
- Schema-drift gap closed: checked `signals` for existing duplicate
  `source_url` values first (table was empty — 0 rows, 0 duplicates),
  then applied `ALTER TABLE signals ADD CONSTRAINT signals_source_url_key
  UNIQUE (source_url)` to the running dev DB. Verified live via `\d
  signals`. No destructive action needed or taken.
- `api` image rebuilt and container recreated (the previously-running
  container predated the collector code) — health check OK.
- Ran `python -m app.collectors.run_collectors` for real inside the
  `api` container: **80 real signals collected and stored** (30 from
  Hacker News via the Algolia Search API, 50 from Product Hunt's RSS
  feed), with correct provenance (real `news.ycombinator.com`-linked
  and Product-Hunt-linked URLs, real titles, real timestamps).
- Ran the collector a second time immediately after: still exactly 80
  rows in `signals` (30 + 50, no growth) — **dedupe proven live**, not
  just in tests.
- **No live candidate was created from this batch** — checked why
  before treating it as a non-issue: max `engagement_score` among the
  30 Hacker News items was 12 (the connector uses `search_by_date`,
  i.e. newest-first, so points haven't accumulated yet — the
  engagement threshold is 50), and 0 of the 80 titles/content matched
  any of the pain-point trigger phrases. This is an honest reflection
  of real, current data, not a pipeline defect — the trigger logic
  itself is proven correct by REVIEWER's and INTELLIGENCE's tests.
  Since no real candidate existed, the live Telegram-alert step (score
  a real M2.1 candidate above threshold) could not be exercised this
  round; that code path is nonetheless proven via the automated
  regression test above and via M1's own earlier live Telegram
  verification (unchanged code, confirmed still passing).
- Minor UX gap noted for later (not fixed now, out of scope for this
  integration pass): `run_collectors.py`'s `main()` calls
  `process_raw_signals()` but never prints its summary dict — verifying
  new/duplicate/candidate counts currently requires querying Postgres
  directly, as done above.

M2.1 completion criteria (all met): REVIEWER's tests integrated; live
DB unique constraint active; real Hacker News + RSS data successfully
stored; dedupe proven live; M1 regression intact.

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
