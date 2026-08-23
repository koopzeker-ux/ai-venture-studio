import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class OpportunityStatus(str, enum.Enum):
    discovered = "discovered"
    researching = "researching"
    scored = "scored"
    validation = "validation"
    rejected = "rejected"
    approved = "approved"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    source_url: Mapped[str | None] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(250))
    thesis: Mapped[str] = mapped_column(Text)
    business_model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[OpportunityStatus] = mapped_column(Enum(OpportunityStatus), default=OpportunityStatus.discovered)
    score: Mapped[float | None] = mapped_column(Float)
    evidence_confidence: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    # M3.2: free-text researcher synthesis of the opportunity's evidence
    # dossier. Nullable -- only populated once a research pass has run.
    research_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    evidence: Mapped[list["Evidence"]] = relationship(back_populates="opportunity", cascade="all, delete-orphan")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), index=True)
    claim: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(String(80))
    # M3.2: application-level enum (FACT, INFERENCE, ESTIMATE, UNKNOWN), not a
    # DB-level enum -- keeps the vocabulary extensible without a migration.
    # Nullable: only populated once a research pass has classified the claim.
    claim_type: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(Text)
    # M3.2: application-level enum (SUPPORTS, CONTRADICTS). Deliberate,
    # previously-corrected design decision: this is ALWAYS relative to
    # `claim` on this same row, NEVER relative to Opportunity.thesis. A row
    # never states whether the opportunity's thesis holds -- only whether
    # this row's own evidence supports or contradicts its own claim text.
    stance: Mapped[str | None] = mapped_column(String(20))
    # M3.2: publication date of the source, distinct from `created_at` (when
    # we recorded the row). Nullable: not every source exposes one.
    found_at: Mapped[datetime | None] = mapped_column(DateTime)
    # M3.2: application-level enum (HIGH, MEDIUM, LOW, UNKNOWN). Deliberately
    # categorical, not Float -- a numeric score would imply a precision of
    # measurement research assessment doesn't actually have.
    source_reliability: Mapped[str | None] = mapped_column(String(20))
    # LEAD decision (M3.2 REVIEWER finding 1, MEDIUM): was previously
    # non-nullable with a Python-level default=0.5, which made a
    # genuinely-estimated 0.5 indistinguishable from "no responsible
    # confidence could be assigned" -- the only place that distinction
    # existed was free-text in AgentRun.output_summary, keyed by JSON-array
    # position, not the Evidence row itself. Made nullable via Alembic
    # revision a943ce8ca51f (additive: DROP NOT NULL, no backfill, no data
    # loss); NULL now means "not assessed", never a stand-in for a real
    # number. No writer in the codebase relied on the old implicit default
    # -- every current writer (collectors, researcher) always sets this
    # explicitly.
    confidence: Mapped[float | None] = mapped_column(Float)
    independently_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # M3.2: self-FK marking this row as a duplicate observation of another
    # Evidence row for the same claim. NULL means this row counts toward the
    # claim group's independent-confirmation count; non-NULL means it does
    # not (see GET /api/opportunities/{id}/dossier).
    duplicate_of_evidence_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    opportunity: Mapped[Opportunity] = relationship(back_populates="evidence")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(120), index=True)
    task_type: Mapped[str] = mapped_column(String(120), index=True)
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    critical_assumption: Mapped[str] = mapped_column(Text)
    cheapest_test: Mapped[str] = mapped_column(Text)
    budget_eur: Mapped[float] = mapped_column(Float, default=0.0)
    success_criteria: Mapped[str] = mapped_column(Text)
    stop_criteria: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[int] = mapped_column(Integer, default=4)
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.pending)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Nullable: most Approval rows so far are not task-scoped. Added for M4.1
    # so an APPROVAL_REQUIRED task transition (see app.orchestration, added by
    # INTELLIGENCE) can be traced back to the task that created it.
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CostEvent(Base):
    __tablename__ = "cost_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    provider: Mapped[str | None] = mapped_column(String(100))
    amount_eur: Mapped[float] = mapped_column(Float)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Nullable for the same reason as Approval.task_id above.
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Task(Base):
    """A unit of orchestrated work (M4.1).

    Deliberately role-agnostic: `role` and `status` are plain strings, not
    hardcoded to software-development concepts, so a future business-agent
    task (research, offer, creative, ...) fits the same table without a
    schema change. `status` is intentionally a plain string column here, not
    a DB-level enum tied to app.orchestration.state_machine's TaskState --
    that module (owned by INTELLIGENCE) may not exist yet in every worktree,
    and persistence must not import orchestration logic. Wiring the two
    together (validating status transitions before writing) is orchestrator
    integration work, not part of this table's definition.
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(120), index=True)
    instructions: Mapped[str | None] = mapped_column(Text)
    allowed_resources: Mapped[list] = mapped_column(JSON, default=list)
    forbidden_actions: Mapped[list] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text)
    # List of Task.id values this task depends on. A simple JSON array, not a
    # join table -- promote to one only if real fan-out/fan-in graphs show up
    # in a later M4.x slice (CLAUDE.md SS15, no speculative abstraction).
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="PLANNED", index=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    budget_eur: Mapped[float | None] = mapped_column(Float)
    required_approvals: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TaskAttempt(Base):
    """One bounded worker invocation for a Task (M4.1).

    Distinct from AgentRun: AgentRun is a flat, unlinked log of completed
    collector runs (M2/M3), with no attempt/retry concept or FK to a task.
    TaskAttempt is what makes the bounded fix-loop (worker -> tests ->
    reviewer -> fix, up to Task.max_attempts) representable at all.
    """

    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    worktree_path: Mapped[str | None] = mapped_column(String(500))
    branch: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    session_id: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    files_changed: Mapped[dict | None] = mapped_column(JSON)
    tests_passed: Mapped[bool | None] = mapped_column(Boolean)
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    findings: Mapped[dict | None] = mapped_column(JSON)
    blockers: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskEvent(Base):
    """Append-only state-transition / audit log for a Task (M4.1).

    This is what makes crash recovery and observability possible: on
    orchestrator restart, re-reading non-terminal Tasks plus their
    TaskEvent history is enough to resume without guessing prior state.
    Never updated or deleted, only inserted.
    """

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("task_attempts.id", ondelete="SET NULL"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
