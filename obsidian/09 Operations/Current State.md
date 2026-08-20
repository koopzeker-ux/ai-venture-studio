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

## Current milestone: M1 (started 2026-08-20)
Goal: Docker -> PostgreSQL -> FastAPI -> opportunity scoring -> Telegram
alert fully working end-to-end.

Multi-agent Git-worktree setup in place (`../avs-builder`,
`../avs-intelligence`, `../avs-reviewer` on branches `agent/builder`,
`agent/intelligence`, `agent/reviewer`) working in parallel with
disjoint file ownership to avoid merge conflicts:

- BUILDER: Docker Compose / infra verification and hardening
  (docker-compose.yml, backend/Dockerfile, .env.example,
  scripts/smoke_test.sh, backend/app/core/config.py,
  backend/app/db/session.py, backend/app/main.py lifespan).
- INTELLIGENCE: opportunity + scoring domain and API
  (backend/app/models/entities.py, backend/app/services/scoring.py,
  backend/app/api/routes.py, related tests).
- REVIEWER: Telegram alert robustness + end-to-end integration tests
  (backend/app/services/telegram.py, new telegram/integration tests).

LEAD does not perform feature work during M1; LEAD reviews scope
adherence and determines integration order before anything merges to
`main`.
