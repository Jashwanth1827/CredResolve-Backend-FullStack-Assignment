"""
FastAPI router: Campaigns and Borrowers
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    BorrowerCreate, BorrowerResponse,
    CampaignCreate, CampaignResponse,
)
from app.database import get_db
from app.models.borrower import Borrower, BorrowerStatus
from app.models.campaign import Campaign, CampaignStatus

router = APIRouter(tags=["campaigns"])


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@router.post("/campaigns", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name=payload.name,
        max_concurrent_calls=payload.max_concurrent_calls,
        provider_name=payload.provider_name,
        seed_answer_rate=payload.seed_answer_rate,
        status=CampaignStatus.DRAFT,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _campaign_to_response(campaign)


@router.get("/campaigns", response_model=List[CampaignResponse])
def list_campaigns(db: Session = Depends(get_db)):
    campaigns = db.execute(select(Campaign)).scalars().all()
    return [_campaign_to_response(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
def get_campaign(campaign_id: str, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _campaign_to_response(campaign)


@router.post("/campaigns/{campaign_id}/activate", response_model=CampaignResponse)
def activate_campaign(campaign_id: str, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.status = CampaignStatus.ACTIVE
    db.commit()
    db.refresh(campaign)
    return _campaign_to_response(campaign)


# ---------------------------------------------------------------------------
# Borrowers
# ---------------------------------------------------------------------------

@router.post(
    "/campaigns/{campaign_id}/borrowers",
    response_model=BorrowerResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_borrower(campaign_id: str, payload: BorrowerCreate, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    borrower = Borrower(
        id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        name=payload.name,
        phone_number=payload.phone_number,
        max_retries=payload.max_retries,
        status=BorrowerStatus.PENDING,
    )
    db.add(borrower)
    db.commit()
    db.refresh(borrower)
    return borrower


@router.get("/campaigns/{campaign_id}/borrowers", response_model=List[BorrowerResponse])
def list_borrowers(campaign_id: str, db: Session = Depends(get_db)):
    borrowers = db.execute(
        select(Borrower).where(Borrower.campaign_id == campaign_id)
    ).scalars().all()
    return list(borrowers)


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

from app.models.call import Call  # noqa: E402
from app.api.schemas import CallResponse  # noqa: E402


@router.get("/campaigns/{campaign_id}/calls", response_model=List[CallResponse])
def list_calls(campaign_id: str, db: Session = Depends(get_db)):
    calls = db.execute(
        select(Call).where(Call.campaign_id == campaign_id)
    ).scalars().all()
    return list(calls)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _campaign_to_response(c: Campaign) -> CampaignResponse:
    return CampaignResponse(
        id=c.id,
        name=c.name,
        status=c.status.value,
        max_concurrent_calls=c.max_concurrent_calls,
        provider_name=c.provider_name,
        total_calls_initiated=c.total_calls_initiated,
        total_calls_answered=c.total_calls_answered,
        total_talk_seconds=c.total_talk_seconds,
        estimated_answer_rate=c.estimated_answer_rate,
        estimated_avg_talk_seconds=c.estimated_avg_talk_seconds,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )
