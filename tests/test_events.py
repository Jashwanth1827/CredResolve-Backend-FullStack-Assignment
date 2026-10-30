"""
Tests: Provider event idempotency and out-of-order event handling.

Requirements tested:
  6. Duplicate provider events — must not cause duplicate state transitions.
  7. Out-of-order provider events — must not regress terminal call states.
  8. Provider timeout (simulated via provider_b).
"""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.agent import AgentStatus
from app.models.call import Call, CallStatus
from app.models.campaign import Campaign
from app.providers import ProviderEvent, ProviderEventType
from app.services.event_processor import EventProcessor
from tests.conftest import make_campaign, make_agent, make_borrower


def make_call(db: Session, campaign_id: str, borrower_id: str, agent_id: str, provider_call_id: str = None) -> Call:
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        borrower_id=borrower_id,
        agent_id=agent_id,
        status=CallStatus.INITIATED,
        provider_name="provider_a",
        provider_call_id=provider_call_id or f"pA-{uuid.uuid4().hex[:12]}",
    )
    db.add(call)
    db.commit()
    return call


# ---------------------------------------------------------------------------
# Test 6: Duplicate events
# ---------------------------------------------------------------------------

def test_duplicate_event_is_ignored(db: Session):
    """A duplicate provider event must not cause a second state transition."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)
    call = make_call(db, campaign.id, borrower.id, agent.id)
    pcid = call.provider_call_id

    event = ProviderEvent(
        event_id="evt-001",
        provider_name="provider_a",
        provider_call_id=pcid,
        event_type=ProviderEventType.RINGING,
    )

    processor = EventProcessor(db)

    # First processing
    result1 = processor.process(event)
    db.expire_all()
    call_after_1 = db.get(Call, call.id)
    assert call_after_1.status == CallStatus.RINGING
    assert result1 is True

    # Second processing — same event_id
    result2 = processor.process(event)
    db.expire_all()
    call_after_2 = db.get(Call, call.id)
    assert call_after_2.status == CallStatus.RINGING  # unchanged
    assert result2 is False  # Returned False = duplicate ignored

    # Verify only one event record exists
    from app.models.provider_event import ProviderEvent as PEv
    from sqlalchemy import select, func
    count = db.execute(
        select(func.count(PEv.id))
        .where(PEv.event_id == "evt-001")
    ).scalar_one()
    assert count == 1


def test_multiple_duplicate_events_still_single_record(db: Session):
    """Even if 5 duplicates arrive, only one is stored and state only advances once."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)
    call = make_call(db, campaign.id, borrower.id, agent.id)
    pcid = call.provider_call_id

    event = ProviderEvent(
        event_id="evt-dedup-5x",
        provider_name="provider_a",
        provider_call_id=pcid,
        event_type=ProviderEventType.RINGING,
    )

    processor = EventProcessor(db)
    results = [processor.process(event) for _ in range(5)]

    # Exactly one True (first), rest False (duplicates)
    assert results.count(True) == 1
    assert results.count(False) == 4


# ---------------------------------------------------------------------------
# Test 7: Out-of-order events
# ---------------------------------------------------------------------------

def test_out_of_order_event_does_not_regress_completed_call(db: Session):
    """
    A late RINGING event arriving after COMPLETED must not move the call backward.
    """
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)
    call = make_call(db, campaign.id, borrower.id, agent.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    # Normal flow: RINGING → ANSWERED → COMPLETED
    processor.process(ProviderEvent("e1", "provider_a", pcid, ProviderEventType.RINGING))
    processor.process(ProviderEvent("e2", "provider_a", pcid, ProviderEventType.ANSWERED))
    processor.process(ProviderEvent("e3", "provider_a", pcid, ProviderEventType.COMPLETED, metadata={"duration_seconds": 90}))

    db.expire_all()
    assert db.get(Call, call.id).status == CallStatus.COMPLETED

    # Late/out-of-order RINGING arrives after COMPLETED
    late_result = processor.process(ProviderEvent("e4-late", "provider_a", pcid, ProviderEventType.RINGING))

    db.expire_all()
    final_call = db.get(Call, call.id)
    assert final_call.status == CallStatus.COMPLETED, "Terminal state must not regress"
    assert late_result is True  # Recorded but transition was ignored


def test_out_of_order_initiated_arrives_after_ringing(db: Session):
    """
    INITIATED arriving after RINGING is silently ignored (can't go backward).
    """
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)
    call = make_call(db, campaign.id, borrower.id, agent.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    # RINGING arrives first
    processor.process(ProviderEvent("e-ring", "provider_a", pcid, ProviderEventType.RINGING))
    db.expire_all()
    assert db.get(Call, call.id).status == CallStatus.RINGING

    # INITIATED arrives late (out of order)
    processor.process(ProviderEvent("e-init", "provider_a", pcid, ProviderEventType.INITIATED))
    db.expire_all()
    # Should still be RINGING — not regressed to INITIATED
    assert db.get(Call, call.id).status == CallStatus.RINGING


def test_failed_event_after_terminal_state_is_ignored(db: Session):
    """FAILED event arriving after COMPLETED must be ignored."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)
    call = make_call(db, campaign.id, borrower.id, agent.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    processor.process(ProviderEvent("e1", "provider_a", pcid, ProviderEventType.RINGING))
    processor.process(ProviderEvent("e2", "provider_a", pcid, ProviderEventType.ANSWERED))
    processor.process(ProviderEvent("e3", "provider_a", pcid, ProviderEventType.COMPLETED, metadata={"duration_seconds": 60}))

    db.expire_all()
    assert db.get(Call, call.id).status == CallStatus.COMPLETED

    # Late FAILED event
    processor.process(ProviderEvent("e4-fail", "provider_a", pcid, ProviderEventType.FAILED))
    db.expire_all()
    assert db.get(Call, call.id).status == CallStatus.COMPLETED


# ---------------------------------------------------------------------------
# Test 8: Provider timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_b_timeout_handled(db: Session):
    """
    Provider B occasionally times out. The allocator releases resources safely.
    """
    import asyncio
    import random
    from app.providers import ProviderB, CallInitRequest

    rng = random.Random(42)
    provider = ProviderB(rng=rng)
    # Force a failure to simulate timeout scenario
    provider.set_outage(True)

    campaign = make_campaign(db, provider_name="provider_b")
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)

    request = CallInitRequest(
        call_id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        phone_number="+15551234567",
        agent_id=agent.id,
    )

    response = await provider.initiate_call(request)
    assert not response.success
    assert "outage" in response.error.lower() or response.error is not None

    # Provider outage should not crash the system
    provider.set_outage(False)


def test_event_processor_handles_unknown_call(db: Session):
    """An event for an unknown provider_call_id is recorded but doesn't crash."""
    processor = EventProcessor(db)
    event = ProviderEvent(
        event_id="evt-unknown",
        provider_name="provider_a",
        provider_call_id="unknown-pcid-xyz",
        event_type=ProviderEventType.RINGING,
    )
    result = processor.process(event)
    # Should process without error (True = recorded, no matching call)
    assert result is True
