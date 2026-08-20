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
- 2026-08-20: Security incident during M1 Telegram setup — the original
  `TELEGRAM_BOT_TOKEN` was briefly exposed in local tool output because
  `source .env` mis-parsed the file's CRLF line endings. No commit, push,
  or external transmission of the token occurred. Why logged: per
  CLAUDE.md §4, secrets must never appear in logs/output; even a local,
  non-persisted exposure is treated as a compromise. Response: token
  rotated immediately via BotFather before any further use; all
  subsequent `.env` handling switched to an in-process Python parser
  that never prints values. Decided/handled by LEAD, confirmed by
  project owner ("gereed").
- 2026-08-20: Milestone M1 marked COMPLETE. Why: the full chain (Docker
  -> PostgreSQL -> FastAPI -> Opportunity -> Scoring -> Telegram) was
  verified live against the real stack — `api` container recreated with
  new Telegram credentials, `/api/health` OK, a real opportunity scored
  above the alert threshold produced `telegram_alert_sent: true` via the
  running container (confirmed via container logs, not a standalone
  script), and the resulting row was confirmed persisted in the real
  PostgreSQL container. This closes the gap noted in the earlier M1
  integration entry, where only Python/SQLite-level verification existed
  because Docker/WSL wasn't installed yet. Approved by project owner
  (who installed and confirmed Docker Desktop + WSL2 themselves).
- 2026-08-20: Planned milestone M2.1 scope — Reddit only, official
  OAuth API (client_credentials, read-only), no LLM-based auto-scoring.
  Why: the requested full flow (sources -> signals -> normalize ->
  cluster/dedupe -> evidence -> candidate -> scoring -> Telegram) is
  large; picking one ToS-compliant source and reusing M1's existing
  scoring/Telegram pipeline unchanged keeps the first vertical slice
  small and provable with real data, per CLAUDE.md §15 (no
  overengineering) and §9 (no new paid provider without approval — an
  LLM-based scorer would need one, so candidates stay unscored/
  human-reviewed for now). YouTube/web-search/reviews/competitor
  sources and real semantic clustering deferred to later M2.x slices.
  Work split across BUILDER/INTELLIGENCE/REVIEWER on disjoint files,
  same worktree pattern as M1. Presented to project owner as a plan
  (architecture, acceptance criteria, exact agent prompts) for review
  before any implementation starts.
- 2026-08-20: SUPERSEDES the previous entry — M2.1's Reddit-only scope
  was withdrawn before any implementation started. Why: the project
  owner checked Reddit's actual Data API Terms and found that
  commercial use may require a separate agreement/explicit permission;
  AI Venture Studio's opportunity-discovery purpose is commercial, so
  "Reddit = free official source" could not be assumed as an
  architecture decision. Response: redesigned M2.1 as a source-agnostic
  pipeline (no connector-specific logic allowed in normalize/dedupe/
  candidate-detection), Reddit demoted to an optional future connector
  gated behind explicit confirmed permission. Researched alternatives
  (Google Trends, YouTube Data API, RSS/Atom, Hacker News API, Product
  Hunt API) before choosing again. Selected for M2.1: Hacker News
  (official Firebase/Algolia API, free, no auth, no commercial-use
  restriction found) and a generic RSS/Atom connector seeded with
  Product Hunt's official public feed — both need zero new secrets.
  Excluded: Google Trends (no public self-serve API as of 2026, only
  unofficial scraping — would violate the no-scraping rule), YouTube
  Data API (ToS is ambiguous/restrictive on indefinite storage of API
  data for a research database — needs a dedicated compliance review
  before use, not assumed safe). Decided by LEAD after explicit
  correction from the project owner; revised plan presented for review
  before implementation.
- 2026-08-20: Milestone M2.1 marked COMPLETE. Why: all three agent
  branches merged cleanly into `main` (no real conflicts — apparent
  conflicts from stale branch bases were verified as artifacts, not
  actual edits, before merging), full backend suite passed at every
  step (18 -> 42 -> 60 tests), and the pipeline was verified live
  against the real Docker/PostgreSQL stack: a schema-drift gap
  (missing unique constraint on `signals.source_url` on the
  already-existing table) was found, checked for existing duplicates
  (none), and closed with a reviewed `ALTER TABLE ... UNIQUE`; real
  Hacker News (30) and Product Hunt RSS (50) signals were collected and
  stored with correct provenance; a second run proved dedupe live (0
  new rows). No real candidate matched the heuristic triggers on this
  batch (explained by fresh/low-engagement HN data and no keyword
  matches, not a defect) — the score->Telegram path itself stays
  proven via REVIEWER's automated regression test and M1's earlier live
  verification. No Reddit/YouTube/Google Trends added, no secrets, no
  production deploy. Approved by project owner.
