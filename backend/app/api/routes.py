from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.entities import Opportunity, OpportunityStatus, Task, TaskAttempt, TaskEvent
from app.orchestration.state_machine import TaskState, dependencies_satisfied
from app.services.scoring import calculate_score, should_alert
from app.services.telegram import send_telegram_message

router = APIRouter()


class OpportunityCreate(BaseModel):
    slug: str
    title: str
    thesis: str
    business_model: str | None = None


class ScoreRequest(BaseModel):
    factors: dict[str, float]
    evidence_confidence: float = Field(ge=0, le=100)


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1)
    role: str = Field(min_length=1)
    instructions: str | None = None
    allowed_resources: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: str | None = None
    depends_on: list[int] = Field(default_factory=list)
    timeout_seconds: int | None = None
    max_attempts: int = 2
    budget_eur: float | None = None
    provider: str | None = None
    model: str | None = None


class TaskSummary(BaseModel):
    id: int
    goal: str
    role: str
    status: str
    created_at: datetime


class TaskDetail(BaseModel):
    id: int
    goal: str
    role: str
    instructions: str | None
    allowed_resources: list
    forbidden_actions: list
    acceptance_criteria: str | None
    depends_on: list
    status: str
    timeout_seconds: int | None
    max_attempts: int
    budget_eur: float | None
    required_approvals: list
    provider: str | None
    model: str | None
    created_at: datetime
    updated_at: datetime
    attempt_count: int


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/opportunities")
def create_opportunity(payload: OpportunityCreate, db: Session = Depends(get_db)):
    existing = db.scalar(select(Opportunity).where(Opportunity.slug == payload.slug))
    if existing:
        raise HTTPException(status_code=409, detail="slug already exists")
    item = Opportunity(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "slug": item.slug, "status": item.status}


@router.get("/opportunities")
def list_opportunities(db: Session = Depends(get_db)):
    items = db.scalars(select(Opportunity).order_by(Opportunity.created_at.desc())).all()
    return [
        {
            "id": x.id,
            "slug": x.slug,
            "title": x.title,
            "status": x.status,
            "score": x.score,
            "evidence_confidence": x.evidence_confidence,
        }
        for x in items
    ]


@router.get("/opportunities/{opportunity_id}")
def get_opportunity(opportunity_id: int, db: Session = Depends(get_db)):
    item = db.get(Opportunity, opportunity_id)
    if not item:
        raise HTTPException(status_code=404, detail="opportunity not found")

    return {
        "id": item.id,
        "slug": item.slug,
        "title": item.title,
        "thesis": item.thesis,
        "business_model": item.business_model,
        "status": item.status,
        "score": item.score,
        "evidence_confidence": item.evidence_confidence,
        "score_breakdown": item.score_breakdown,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "evidence": [
            {
                "id": e.id,
                "claim": e.claim,
                "evidence_type": e.evidence_type,
                "source": e.source,
                "source_url": e.source_url,
                "confidence": e.confidence,
                "independently_confirmed": e.independently_confirmed,
                "created_at": e.created_at,
            }
            for e in item.evidence
        ],
    }


@router.post("/opportunities/{opportunity_id}/score")
async def score_opportunity(opportunity_id: int, payload: ScoreRequest, db: Session = Depends(get_db)):
    item = db.get(Opportunity, opportunity_id)
    if not item:
        raise HTTPException(status_code=404, detail="opportunity not found")

    score, breakdown = calculate_score(payload.factors)
    item.score = score
    item.evidence_confidence = payload.evidence_confidence
    item.score_breakdown = breakdown
    item.status = OpportunityStatus.scored
    db.commit()

    alerted = False
    if should_alert(score, payload.evidence_confidence):
        message = (
            "🏆 HIGH-CONFIDENCE OPPORTUNITY\n\n"
            f"{item.title}\n"
            f"Score: {score}/100\n"
            f"Evidence confidence: {payload.evidence_confidence}/100\n\n"
            f"Thesis: {item.thesis}\n\n"
            "Recommended: REVIEW FOR VALIDATION"
        )
        alerted = await send_telegram_message(message)

    return {
        "id": item.id,
        "score": score,
        "evidence_confidence": payload.evidence_confidence,
        "breakdown": breakdown,
        "telegram_alert_sent": alerted,
    }


def _task_detail(task: Task, db: Session) -> TaskDetail:
    attempt_count = (
        db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.task_id == task.id)) or 0
    )
    return TaskDetail(
        id=task.id,
        goal=task.goal,
        role=task.role,
        instructions=task.instructions,
        allowed_resources=task.allowed_resources,
        forbidden_actions=task.forbidden_actions,
        acceptance_criteria=task.acceptance_criteria,
        depends_on=task.depends_on,
        status=task.status,
        timeout_seconds=task.timeout_seconds,
        max_attempts=task.max_attempts,
        budget_eur=task.budget_eur,
        required_approvals=task.required_approvals,
        provider=task.provider,
        model=task.model,
        created_at=task.created_at,
        updated_at=task.updated_at,
        attempt_count=attempt_count,
    )


@router.post("/tasks", response_model=TaskDetail, status_code=201)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    """Task row + its creation TaskEvent + an optional immediate PLANNED->READY
    TaskEvent are one atomic unit: a single commit() at the end, everything
    before it flushed (not committed) to get task.id. A crash/exception
    anywhere in between rolls back and leaves nothing behind -- never a Task
    row with zero TaskEvent history, which would violate TaskEvent's own
    documented crash-recovery contract (see entities.py). REVIEWER finding
    (caa30d7): the previous three-separate-commits version could leave
    exactly that half-written state if the process died between commits.
    """
    try:
        task = Task(
            goal=payload.goal,
            role=payload.role,
            instructions=payload.instructions,
            allowed_resources=payload.allowed_resources,
            forbidden_actions=payload.forbidden_actions,
            acceptance_criteria=payload.acceptance_criteria,
            depends_on=payload.depends_on,
            timeout_seconds=payload.timeout_seconds,
            max_attempts=payload.max_attempts,
            budget_eur=payload.budget_eur,
            provider=payload.provider,
            model=payload.model,
            status=TaskState.PLANNED.value,
        )
        db.add(task)
        db.flush()  # assigns task.id within the still-open transaction, no commit yet

        db.add(
            TaskEvent(
                task_id=task.id,
                from_state=None,
                to_state=TaskState.PLANNED.value,
                actor="human",
            )
        )

        dependency_tasks = (
            db.scalars(select(Task).where(Task.id.in_(payload.depends_on))).all() if payload.depends_on else []
        )
        found_ids = {t.id for t in dependency_tasks}
        ready = found_ids == set(payload.depends_on) and dependencies_satisfied(
            TaskState(t.status) for t in dependency_tasks
        )

        if ready:
            task.status = TaskState.READY.value
            db.add(
                TaskEvent(
                    task_id=task.id,
                    from_state=TaskState.PLANNED.value,
                    to_state=TaskState.READY.value,
                    actor="orchestrator",
                )
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(task)
    return _task_detail(task, db)


@router.get("/tasks", response_model=list[TaskSummary])
def list_tasks(db: Session = Depends(get_db)):
    items = db.scalars(select(Task).order_by(Task.created_at.desc())).all()
    return [
        TaskSummary(id=t.id, goal=t.goal, role=t.role, status=t.status, created_at=t.created_at) for t in items
    ]


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_detail(task, db)
