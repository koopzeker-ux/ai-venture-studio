# Orchestrator

## Responsibility
Route work, enforce state transitions, budget constraints, permissions and approval gates.

## Initial workflow
collect -> research -> critique -> score -> notify

## Forbidden without approval
Material spend, production deployment with material risk, domains/DNS, contracts, critical secret changes, destructive data operations.

## Future scope / north star (added 2026-08-21, not yet implemented)
M4's orchestration core must not be designed as a software-development-only
orchestrator. It must be generic enough to later route business-agent
workflows too, without needing to be redesigned when those agents arrive:
Opportunity Research, Market/Country Research, Customer/Target Audience
Research, Competitor Research, Offer Creation, Brand, Website, Creative
Strategy, Creative Generation, Advertising, Experimentation, Performance
Analysis, Learning/Optimization.

Target end-to-end loop the architecture should stay compatible with:
market/data -> discover opportunities -> research -> determine
country + target audience -> assess commercial evidence -> select
opportunity -> build offer -> build website/creatives -> test -> measure
performance -> learn -> improve.

Over time the human's role narrows to: (1) set end goals, (2) give key
approvals, (3) judge end results — the orchestrator carries the rest,
subject to the existing approval gates (see "Forbidden without approval"
above and CLAUDE.md §8).

None of the business agents listed above are built yet, and this note is
a constraint on M4's architecture (keep routing/state/approval concerns
generic), not an expansion of what gets implemented now.
