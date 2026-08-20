from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.entities import Opportunity, OpportunityStatus
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
