"""
FastAPI router: Provider events
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import ProviderEventRequest, ProviderEventResponse
from app.database import get_db
from app.providers import ProviderEvent as PEvent, ProviderEventType
from app.services.event_processor import EventProcessor

router = APIRouter(tags=["provider"])


@router.post("/provider/events", response_model=ProviderEventResponse)
def receive_provider_event(
    payload: ProviderEventRequest,
    db: Session = Depends(get_db),
):
    """
    Receive a provider event (INITIATED, RINGING, ANSWERED, COMPLETED, FAILED).

    Events are processed idempotently — duplicates are detected via the
    provider_events table unique constraint and ignored safely.
    Out-of-order events that would regress a terminal state are also ignored.
    """
    try:
        event_type = ProviderEventType(payload.event_type)
    except ValueError:
        return ProviderEventResponse(
            processed=False,
            message=f"Unknown event type: {payload.event_type}",
        )

    event = PEvent(
        event_id=payload.event_id,
        provider_name=payload.provider_name,
        provider_call_id=payload.provider_call_id,
        event_type=event_type,
        metadata=payload.metadata,
    )

    processor = EventProcessor(db)
    processed = processor.process(event)

    return ProviderEventResponse(
        processed=processed,
        message="processed" if processed else "duplicate or ignored",
    )
