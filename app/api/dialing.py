"""
FastAPI router: Dialing endpoints
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import DialResponse
from app.database import get_db
from app.services.campaign_manager import CampaignManager

router = APIRouter(tags=["dialing"])


def _format_dial_response(result) -> DialResponse:
    pacing_explanation = None
    safety_str = None
    if result.pacing_decision:
        pacing_explanation = result.pacing_decision.explanation
    if result.safety_decision:
        safety_str = result.safety_decision.reason
    return DialResponse(
        calls_initiated=result.calls_initiated,
        calls_attempted=result.calls_attempted,
        calls_failed=result.calls_failed,
        pacing_explanation=pacing_explanation,
        safety_decision=safety_str,
        errors=result.errors,
    )


@router.post("/campaigns/{campaign_id}/dial", response_model=DialResponse)
async def progressive_dial(campaign_id: str, db: Session = Depends(get_db)):
    """
    Progressive dialing: initiate one call per available agent.
    All calls pass through the Safety Controller.
    """
    manager = CampaignManager(db)
    result = await manager.progressive_dial(campaign_id)
    return _format_dial_response(result)


@router.post("/campaigns/{campaign_id}/predictive-dial", response_model=DialResponse)
async def predictive_dial(campaign_id: str, db: Session = Depends(get_db)):
    """
    Predictive dialing: pacing engine calculates call count.
    Safety Controller independently caps the approved count.
    """
    manager = CampaignManager(db)
    result = await manager.predictive_dial(campaign_id)
    return _format_dial_response(result)
