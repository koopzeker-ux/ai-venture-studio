# Decision Timeline

- MVP starts as one Linux VPS + Docker Compose + PostgreSQL.
- Telegram is the executive alert channel.
- Obsidian is the human-readable Company Brain; PostgreSQL is operational truth.
- Start with a small number of logical roles instead of a large agent swarm.
- 2026-08-20: Adopted `CLAUDE.md` as the permanent governance document for
  Claude Code work in this repo. Why: needed explicit, durable rules for
  autonomy boundaries (safe low-risk actions vs. human approval gates),
  cost budgets, git workflow, research integrity, and market-intelligence
  scope, so future sessions don't need these re-explained. Approved by
  project owner after a two-round review (initial proposal + 11 revisions
  covering git autonomy, MCP read/low-risk/high-risk write tiers, budget-
  based cost control, broadened Telegram scope, research integrity,
  market-intelligence scope, agent/model-agnostic architecture, explicit
  approval-gate list, and an explicit autonomy principle).
- 2026-08-20: Started milestone M1 (Docker -> PostgreSQL -> FastAPI ->
  opportunity scoring -> Telegram alert, end-to-end) using the existing
  multi-agent Git-worktree setup (BUILDER/INTELLIGENCE/REVIEWER on
  separate branches). Why: most of the code already existed but was
  unverified end-to-end; splitting infra, domain/scoring, and
  Telegram/integration-testing into disjoint file ownership lets the
  three agents work in parallel without merge conflicts. LEAD does no
  feature work during M1 and merges nothing to `main` until REVIEWER
  has checked results. Approved by project owner.
