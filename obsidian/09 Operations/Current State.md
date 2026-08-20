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
Run the stack, verify PostgreSQL + API, configure Telegram, then implement the first real Market Intelligence collector.

## Current milestone: M1 (started 2026-08-20) — IN PROGRESS, NOT COMPLETE
Goal: Docker -> PostgreSQL -> FastAPI -> opportunity scoring -> Telegram
alert fully working end-to-end.

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

**What's still open before M1 can be called done:**
- The actual Docker Compose stack (`db` + `api` containers) has never
  been run on this machine — Docker/WSL is not installed. Everything
  verified so far is at the Python/SQLite test level, not against a
  real running PostgreSQL + containerized FastAPI.
- `scripts/smoke_test.sh` has not been executed against a live stack.
- Telegram alert has not been exercised against the real Telegram API
  (only mocked in tests) — needs a configured bot token/chat id.

Per explicit instruction: Docker/WSL is NOT to be installed and no
production deployment happens as part of closing this out
autonomously — the real end-to-end run needs the user's machine/
approval.

LEAD performed no feature work during M1; LEAD reviewed both branches,
confirmed scope was respected, resolved the merge conflict, and merged
after the full suite passed.
