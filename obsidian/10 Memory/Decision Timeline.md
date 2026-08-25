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
- 2026-08-20: Milestone M2.2 marked COMPLETE. Scope: fix the M2.1
  detection gap (wrong HN query for traction; RSS content structurally
  unable to trigger any heuristic) with a source-agnostic
  `is_launch` metadata field, new purchase-intent/alternative-seeking
  triggers, and a promotion gate. Key decision made before any code was
  written: `product_launch_signal` must never promote an Opportunity
  alone — an earlier draft would have let it, which given Product
  Hunt's feed marking ~50/80 live signals as launches would have
  flooded the pipeline with candidates purely because products exist.
  Also decided: defer automatic evidence-enrichment via Jaccard
  title-overlap — a live test against realistic short PH/HN titles
  showed 4/5 unrelated pairs crossing a 0.5 threshold on shared
  stopwords alone, a real false-merge risk, not a hypothetical one.
  Why complete: full suite green (92/92, including REVIEWER's
  independent negative-case proof), and live verification against real
  Docker/PostgreSQL produced 20 real Opportunities (all via genuine HN
  traction, e.g. "Steve Jobs has passed away", "backdoor in upstream
  xz/liblzma") while zero launch-only signals promoted on their own
  out of a real batch of 58 (8 HN + 50 PH). One edge case recorded but
  not "fixed": a single Show-HN post that gamed both triggers landed
  just past the volume cap (first-come-first-served by design, not
  score-ranked) — documented, not silently patched. M1 scoring/
  Telegram confirmed intact with a real, live alert send. No Reddit/
  YouTube/Google Trends, no LLM, no threshold tuning without approval.
  Approved by project owner.
- 2026-08-20: Milestone M3.1 marked COMPLETE — first slice of the
  planned M3 "Opportunity Research & Prioritization Engine". Replaces
  M2.2's first-come-first-served volume cap with deterministic
  pre-ranking (signal-diversity + purchase-intent/alternative-seeking/
  pain weighting + a log-scaled, capped engagement bonus), proven
  input-order-independent by both INTELLIGENCE and REVIEWER
  (REVIEWER's version deliberately placed the winning candidate last
  in the input to specifically probe for position bias). Also adds
  AgentRun logging per collector run and a read-only
  GET /api/opportunities/{id} detail endpoint (evidence visibility,
  needed ahead of M3.2). Why complete: full suite green (120/120), and
  live verification against real Docker/PostgreSQL confirmed dedupe,
  AgentRun, and the detail endpoint all work correctly — though the
  live batch that day happened to contain zero gate-passing candidates
  (explained: fresh low-engagement HN posts and duplicate high-
  engagement classics from the same-day M2.2 run), so the
  "commercial-evidence-beats-engagement" property was proven live only
  via the automated tests' real engagement values (up to 6015), not
  from a populated live cap-boundary table that day — recorded
  honestly, no threshold/weight changed to force a different outcome.
  M3.2 (Researcher) and M3.3 (Critic + automatic Evidence Confidence +
  Telegram gate) remain fully designed in the M3 plan but NOT started —
  both require a separate, explicit LLM-provider/budget approval before
  any implementation. Approved by project owner.
- 2026-08-21: Set a north-star architectural constraint for the
  upcoming M4 "Agent Orchestration" milestone, ahead of any design work:
  the orchestration core must not be built as a software-development-only
  orchestrator. It must generalize to future business-agent workflows
  (Opportunity/Market/Country/Customer/Competitor Research, Offer
  Creation, Brand, Website, Creative Strategy, Creative Generation,
  Advertising, Experimentation, Performance Analysis, Learning/
  Optimization) without needing a redesign when those agents are added
  later. Why: the long-term vision is a market/data -> discover ->
  research -> select country+audience -> assess evidence -> select
  opportunity -> build offer -> build website/creatives -> test -> measure
  -> learn -> improve loop, where the human's role narrows to setting end
  goals, giving key approvals, and judging end results — building M4
  narrowly for code-agent orchestration now would force a rework later.
  Recorded in `07 Agents/Orchestrator.md`. None of the business agents are
  built yet; this is a constraint on M4's architecture, not new scope to
  implement now — M4 implementation itself has still not started. Set by
  project owner.
- 2026-08-21: Full M4 architecture plan produced (23 sections: state
  machine, worker contract, Claude Code headless-automation research,
  OpenAI comparison research, provider strategy, DB impact, git/worktree
  strategy, bounded fix-loop, approval policy, Telegram control plane,
  observability, threat model, costs, M4.1-M4.6 roadmap with acceptance
  criteria, dogfood test, risks, explicit non-scope) and approved by
  project owner. Two decisions deliberately left open pending real
  measurement rather than guessed: Claude subscription/OAuth-token vs.
  metered API key for future workers, and Claude Agent SDK vs. CLI
  subprocess. Approved by project owner.
- 2026-08-21: Milestone M4.1 marked COMPLETE — Task/TaskAttempt/TaskEvent
  persistence (Alembic adopted for the first time) + a pure-Python,
  role-agnostic task state machine. Why complete: full suite green (238/238
  after REVIEWER's 45 independent tests + 3 real findings), all 3 REVIEWER
  findings explicitly decided and resolved using the approved M4 plan as
  sole authority (no new architecture invented) — RUNNING -> FAILED
  removed in favor of the plan's intended retry-first route,
  NEEDS_FIX/INTEGRATING -> BLOCKED widened to include HUMAN per the plan's
  generic active-state rule, and a Dockerfile gap (alembic.ini/alembic/
  never copied into the image) found and fixed while executing the
  deploy-order-safe live-migration procedure. Live Postgres taken from
  pre-Alembic to migration head with a backup first, read-only drift-check
  before stamping, and the long-running `api` container recreated only
  after `alembic current == head` was confirmed — existing M1-M3.1 data
  (22 opportunities, 180 signals, 2 agent_runs, 20 evidence) verified
  byte-identical before/after via both `psql` and the live API, `/api/health`
  green. Two LEAD correction rounds against the approved plan are logged in
  detail in `09 Operations/Current State.md`. No LLM/API calls, no new
  paid provider, no worker execution anywhere in M4.1. M4.2 (a real
  dispatched worker) not started. Approved by project owner.
- 2026-08-23: Milestone M4.2 ("One Local Worker") marked COMPLETE. Why
  complete: a real Claude Code worker, automatically dispatched via
  `dispatch_task()` with zero manual prompt-copying, completed a real
  bounded task end-to-end in a real isolated git worktree, was independently
  verified (not trusted) by a real pytest run and a real git-status scope
  check, and landed the task at `REVIEW_PENDING` with correct
  Task/TaskAttempt/TaskEvent audit history and correct cost bookkeeping —
  proven live on 2026-08-23, not just by tests. Two real problems were
  found and fixed via live use, not by review alone: (1) `--bare` (used
  since the first BUILDER/INTELLIGENCE handoff) turned out to restrict
  Claude Code auth to `ANTHROPIC_API_KEY`/`apiKeyHelper` only, excluding
  OAuth — discovered because the first live dogfood attempt failed fast and
  safely on "Not logged in" despite a valid interactive login; fixed by
  swapping to `--safe-mode`, which the installed CLI's own `--help` confirms
  keeps "Auth, model selection, built-in tools, and permissions" working
  normally while still disabling the same ambient-customization surface
  `--bare` did. LEAD independently re-verified this against the real
  installed binary before merging, not just the handoff's summary. (2)
  REVIEWER's independent adversarial review (own fixtures, real disposable
  git repos) found a CRITICAL scope-check bypass: `_git_changed_files()`
  used only `git diff --name-only`, which never reports untracked files by
  design, so any new out-of-scope file a worker's Write tool created was
  invisible to the layer-2 `allowed_resources` check, unconditionally, for
  every task whose goal required creating a file — fixed by unioning the
  existing diff with `git status --porcelain -z --untracked-files=all`,
  both fail-closed on any git error. Full backend suite: 238 -> 296 (M4.2
  handoff) -> 368 (REVIEWER) -> 381 (LEAD CRITICAL+MEDIUM+LOW fixes) -> 387
  (auth-fix). No LLM/API calls in any test, no new paid provider beyond the
  existing Claude Code subscription, no scheduler/polling loop, no
  multi-worker concurrency, no reviewer/fix-loop, no auto-merge/push — the
  bounded fix-loop and Integrator are M4.3/M4.4, not started. Full detail
  in `09 Operations/Current State.md`. Approved by project owner.
- 2026-08-25: Milestone M3.2 ("Researcher") marked COMPLETE. Why: the one
  live Researcher run this milestone's approved €2-equivalent budget
  allowed (Opportunity #21, "Google Search Is Dying") succeeded end-to-end
  — real cost $0.3988726, 16 evidence-backed rows persisted, a substantive
  8-section dossier including a genuine red-team argument against testing
  the opportunity as originally framed — and LEAD independently opened
  every one of the 15 sourced Evidence rows' URLs by hand rather than
  trusting the technical success alone: 8 fully VERIFIED, 7
  PARTIALLY_VERIFIED (real, on-topic sources whose claims blend a confirmed
  figure with 1-2 adjacent details not actually found on that page — e.g. a
  Perplexity ARR figure the cited page didn't contain), 0 NOT_VERIFIED, 0
  BROKEN_SOURCE, and one statistic the Researcher itself correctly left
  UNKNOWN rather than inventing a source for. Getting here took two live
  attempts and two LEAD/REVIEWER rounds on real production bugs, not just
  design review: attempt 1 (2026-08-23) completed a real paid model call
  and then failed at persistence — `Evidence.source` is a DB `String(120)`
  column, the parser only capped it at 500, a mismatch introduced in an
  earlier LEAD round that never checked the actual column length; the
  transaction rolled back correctly (no half-written dossier), but the
  completed call's real USD cost was never logged anywhere, only the
  failure. Fixed (`09a7659`, re-reviewed clean by REVIEWER at `826e34e`):
  the source cap is now read live off the actual SQLAlchemy column
  (`Evidence.__table__.columns["source"].type.length`) instead of a second
  hardcoded number, and known cost/usage now survives into all three
  `dispatch_research()` failure branches. Separately, REVIEWER's own
  adversarial round found `Evidence.confidence`'s non-nullable Float
  default (0.5) made a genuinely-estimated 0.5 indistinguishable from "the
  researcher declined to estimate" once persisted — LEAD's decision
  (`a5e9e14`) was the smallest correct fix: make the column nullable
  (migration `a943ce8ca51f`, additive, no writer relied on the old
  default) rather than build any compensating architecture. One finding
  deliberately left open, not fixed: `independently_confirmed` read `false`
  for every row in the successful live run even where evidence was
  genuinely well-corroborated from independent primary sources, because the
  mechanism requires an exact normalized-claim-text match and a real LLM
  researcher phrases each extracted point in its own words — a structural
  limitation of the current design, not a fabrication risk, left for a
  future milestone rather than patched under time pressure to force a
  cleaner number. Full backend suite: 465 -> 611 across the milestone (see
  `09 Operations/Current State.md` for the exact per-round counts). No new
  provider, no new dependency, no economics engine, no Critic agent, no
  Telegram automation, no autonomous TEST/WATCH/REJECT decision — M3.2
  produces a dossier for human review, nothing more; M3.3 not started.
  Approved by project owner.
