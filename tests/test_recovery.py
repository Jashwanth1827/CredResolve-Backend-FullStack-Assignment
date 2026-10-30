"""
Tests: Recovery, agent disappearance, borrower retry, and provider outage.

Requirements tested:
  9.  Worker crash and recovery (stale lease recovery).
  10. Agent disappearance during call setup.
  11. Borrower reservation and retry behavior.
  12. Provider outage and fallback behavior.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallStatus
from app.services.call_allocator import CallAllocator
from app.services.recovery import RecoveryService
from tests.conftest import make_campaign, make_agent, make_borrower


# ---------------------------------------------------------------------------
# Test 9: Worker crash and stale lease recovery
# ---------------------------------------------------------------------------

def test_stale_lease_recovery(db: Session):
    """
    A call with an expired lease (simulating worker crash) is reclaimed.
    The agent is released to AVAILABLE, the borrower returned to PENDING.
    """
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id, status=AgentStatus.RESERVED)
    borrower = make_borrower(db, campaign.id, status=BorrowerStatus.RESERVED)

    # Create an INITIATED call with an expired lease
    expired_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        agent_id=agent.id,
        status=CallStatus.INITIATED,
        lease_expires_at=expired_time,
        provider_name="provider_a",
        provider_call_id=f"pA-{uuid.uuid4().hex[:12]}",
    )
    db.add(call)
    db.commit()

    recovery = RecoveryService(db)
    recovered = recovery.recover_stale_calls()

    assert recovered == 1, "Exactly one stale call should be recovered"

    db.expire_all()
    call_db = db.get(Call, call.id)
    assert call_db.status == CallStatus.FAILED
    assert "stale" in call_db.failure_reason.lower()

    agent_db = db.get(Agent, agent.id)
    assert agent_db.status == AgentStatus.AVAILABLE

    borrower_db = db.get(Borrower, borrower.id)
    assert borrower_db.status == BorrowerStatus.PENDING
    assert borrower_db.retry_count == 1


def test_recovery_does_not_affect_healthy_calls(db: Session):
    """Calls with future leases (not crashed) are not reclaimed."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id, status=AgentStatus.RESERVED)
    borrower = make_borrower(db, campaign.id, status=BorrowerStatus.RESERVED)

    future_time = datetime.now(timezone.utc) + timedelta(seconds=60)
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        agent_id=agent.id,
        status=CallStatus.INITIATED,
        lease_expires_at=future_time,
        provider_name="provider_a",
        provider_call_id=f"pA-{uuid.uuid4().hex[:12]}",
    )
    db.add(call)
    db.commit()

    recovery = RecoveryService(db)
    recovered = recovery.recover_stale_calls()

    assert recovered == 0

    db.expire_all()
    call_db = db.get(Call, call.id)
    assert call_db.status == CallStatus.INITIATED  # Unchanged


def test_recovery_handles_reserved_calls_too(db: Session):
    """RESERVED calls with expired leases are also recovered."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id, status=AgentStatus.RESERVED)
    borrower = make_borrower(db, campaign.id, status=BorrowerStatus.RESERVED)

    expired_time = datetime.now(timezone.utc) - timedelta(seconds=30)
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        agent_id=agent.id,
        status=CallStatus.RESERVED,
        lease_expires_at=expired_time,
    )
    db.add(call)
    db.commit()

    recovery = RecoveryService(db)
    recovered = recovery.recover_stale_calls()
    assert recovered == 1

    db.expire_all()
    assert db.get(Call, call.id).status == CallStatus.FAILED
    assert db.get(Agent, agent.id).status == AgentStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Test 10: Agent disappearance during call setup
# ---------------------------------------------------------------------------

def test_agent_disappearance_releases_resources(db: Session):
    """
    If an agent disappears after reservation but before initiation, the call
    fails cleanly and the borrower is returned to PENDING.
    """
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)
    borrower = make_borrower(db, campaign.id)

    allocator = CallAllocator(db)
    allocation = allocator.allocate(campaign.id, borrower)
    assert allocation.success

    # Simulate agent disappearance by deleting it
    db.delete(db.get(Agent, allocation.agent_id))
    db.commit()

    # The allocator should detect disappearance and fail the call
    allocator.fail_call_and_release(
        allocation.call_id,
        allocation.agent_id,
        allocation.borrower_id,
        "Agent disappeared during setup",
    )

    db.expire_all()
    call_db = db.get(Call, allocation.call_id)
    assert call_db.status == CallStatus.FAILED

    borrower_db = db.get(Borrower, borrower.id)
    assert borrower_db.status == BorrowerStatus.PENDING  # Released for retry


# ---------------------------------------------------------------------------
# Test 11: Borrower reservation and retry behavior
# ---------------------------------------------------------------------------

def test_borrower_retry_increments_on_failure(db: Session):
    """Failed calls increment borrower.retry_count and return to PENDING."""
    from app.providers import ProviderEvent, ProviderEventType
    from app.services.event_processor import EventProcessor

    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id, max_retries=3)

    # Create an active call
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        agent_id=agent.id,
        status=CallStatus.RINGING,
        provider_name="provider_a",
        provider_call_id=f"pA-{uuid.uuid4().hex[:12]}",
    )
    db.add(call)
    # Mark borrower as RESERVED
    db.execute(
        __import__("sqlalchemy").text(
            "UPDATE borrowers SET status='RESERVED' WHERE id=:id"
        ),
        {"id": borrower.id},
    )
    db.commit()

    processor = EventProcessor(db)
    processor.process(ProviderEvent(
        "e-fail-1", "provider_a", call.provider_call_id, ProviderEventType.FAILED,
        metadata={"reason": "no-answer"},
    ))

    db.expire_all()
    b = db.get(Borrower, borrower.id)
    assert b.retry_count == 1
    assert b.status == BorrowerStatus.PENDING  # Back to pending for retry


def test_borrower_exhausted_retries_marked_failed(db: Session):
    """Borrower is marked FAILED when retry_count reaches max_retries."""
    from app.providers import ProviderEvent, ProviderEventType
    from app.services.event_processor import EventProcessor

    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id, max_retries=1, retry_count=0)

    # Place borrower at retry limit - 1 call
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        agent_id=agent.id,
        status=CallStatus.RINGING,
        provider_name="provider_a",
        provider_call_id=f"pA-{uuid.uuid4().hex[:12]}",
    )
    db.add(call)
    db.execute(
        __import__("sqlalchemy").text(
            "UPDATE borrowers SET status='RESERVED' WHERE id=:id"
        ),
        {"id": borrower.id},
    )
    db.commit()

    processor = EventProcessor(db)
    processor.process(ProviderEvent(
        "e-exhaust", "provider_a", call.provider_call_id, ProviderEventType.FAILED,
        metadata={"reason": "no-answer"},
    ))

    db.expire_all()
    b = db.get(Borrower, borrower.id)
    # retry_count becomes 1 = max_retries → status = FAILED
    assert b.retry_count == 1
    assert b.status == BorrowerStatus.FAILED


def test_borrower_not_selected_when_reserved(db: Session):
    """The dialer should not select a RESERVED borrower."""
    from sqlalchemy import select
    from app.models.borrower import BorrowerStatus

    campaign = make_campaign(db)
    # Create a RESERVED borrower (already in a call)
    reserved_b = make_borrower(db, campaign.id, status=BorrowerStatus.RESERVED)
    # Create a PENDING borrower
    pending_b = make_borrower(db, campaign.id, status=BorrowerStatus.PENDING)

    from app.services.campaign_manager import CampaignManager
    manager = CampaignManager(db)
    selected = manager._select_borrower(campaign.id)

    assert selected is not None
    assert selected.id == pending_b.id
    assert selected.id != reserved_b.id


# ---------------------------------------------------------------------------
# Test 12: Provider outage and fallback behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_outage_triggers_safety_rejection(db: Session):
    """Safety controller rejects calls during provider outage."""
    from app.providers import get_provider
    from app.services.safety_controller import SafetyController

    campaign = make_campaign(db, provider_name="provider_a")
    for _ in range(3):
        make_agent(db, campaign.id)

    provider = get_provider("provider_a")
    provider.set_outage(True)
    try:
        controller = SafetyController(db)
        decision = controller.check_progressive(campaign.id, "provider_a")
        assert decision.approved_count == 0
        assert not decision.provider_healthy
    finally:
        provider.set_outage(False)


@pytest.mark.asyncio
async def test_predictive_fallback_to_progressive_on_outage(db: Session):
    """Predictive pacing falls back to progressive when provider is unhealthy."""
    from app.providers import get_provider
    from app.services.pacing_engine import PredictivePacingEngine

    campaign = make_campaign(db, provider_name="provider_a", seed_answer_rate=0.5)
    for _ in range(5):
        make_agent(db, campaign.id)

    provider = get_provider("provider_a")
    provider.set_outage(True)
    try:
        pacing = PredictivePacingEngine(db)
        decision = pacing.compute_pacing(campaign)
        # Provider unhealthy → fallback to progressive
        assert decision.safety_decision.fallback_to_progressive or decision.safety_decision.approved_count == 0
    finally:
        provider.set_outage(False)
