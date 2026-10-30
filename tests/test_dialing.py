"""
Tests: Progressive dialer capacity enforcement.

Requirements tested:
  3. Progressive dialing capacity — N available agents → ≤ N agent-bound calls.
  4. Predictive pacing calculation.
  5. Safety Controller rejecting excessive requests.
"""

from __future__ import annotations

import math
import pytest
from sqlalchemy.orm import Session

from app.models.agent import AgentStatus
from app.models.call import CallStatus
from app.services.safety_controller import SafetyController
from app.services.pacing_engine import PredictivePacingEngine
from tests.conftest import make_campaign, make_agent, make_borrower


# ---------------------------------------------------------------------------
# Test 3: Progressive mode — capacity = number of available agents
# ---------------------------------------------------------------------------

def test_progressive_capacity_matches_available_agents(db: Session):
    """Safety controller approves exactly N calls for N available agents."""
    campaign = make_campaign(db)
    for _ in range(3):
        make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    controller = SafetyController(db)
    decision = controller.check_progressive(campaign.id, "provider_a")

    assert decision.approved_count == 3
    assert decision.provider_healthy is True


def test_progressive_capacity_accounts_for_active_calls(db: Session):
    """If 2 of 5 agents already have active calls, only 3 slots open."""
    campaign = make_campaign(db)

    # 5 agents
    for _ in range(5):
        make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    # 2 already have agent-bound active calls
    import uuid
    from app.models.call import Call
    from datetime import datetime, timezone
    for i in range(2):
        agent = make_agent(db, campaign.id, status=AgentStatus.DIALING)
        borrower = make_borrower(db, campaign.id)
        call = Call(
            id=str(uuid.uuid4()),
            campaign_id=campaign.id,
            borrower_id=borrower.id,
            agent_id=agent.id,
            status=CallStatus.INITIATED,
        )
        db.add(call)
    db.commit()

    controller = SafetyController(db)
    decision = controller.check_progressive(campaign.id, "provider_a")

    # 5 available + 0 dialing_but_available (the DIALING agents are not AVAILABLE)
    # But active agent-bound calls = 2 → safety sees available=5, active=2 → 3 slots
    assert decision.approved_count == 3


def test_progressive_no_capacity_when_all_agents_busy(db: Session):
    """Safety controller approves 0 when all agents are occupied."""
    campaign = make_campaign(db)
    make_agent(db, campaign.id, status=AgentStatus.CONNECTED)
    make_agent(db, campaign.id, status=AgentStatus.DIALING)

    controller = SafetyController(db)
    decision = controller.check_progressive(campaign.id, "provider_a")

    assert decision.approved_count == 0


# ---------------------------------------------------------------------------
# Test 4: Predictive pacing calculation
# ---------------------------------------------------------------------------

def test_predictive_pacing_calculation(db: Session):
    """
    Pacing engine calculates desired_outstanding = ceil(free_capacity / answer_rate).
    With 10 available agents and 50% answer rate: desired = ceil(10/0.5) = 20.
    But safety caps at 10 (available agents).
    """
    campaign = make_campaign(db, seed_answer_rate=0.5)
    for _ in range(10):
        make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    pacing = PredictivePacingEngine(db)
    decision = pacing.compute_pacing(campaign)

    # With 10 available and 50% answer rate, desired_outstanding = 20
    # Safety caps at 10 (no active calls, so 10 slots)
    assert decision.desired_outstanding == math.ceil(10 / 0.5)
    assert decision.safety_decision.approved_count <= 10
    assert "predictive" in decision.mode or "progressive" in decision.mode


def test_predictive_pacing_uses_estimated_answer_rate(db: Session):
    """Pacing uses rolling answer rate once enough calls are made."""
    campaign = make_campaign(db, seed_answer_rate=0.3)
    # Simulate rolling stats: 100 initiated, 70 answered → 70% answer rate
    campaign.total_calls_initiated = 100
    campaign.total_calls_answered = 70
    db.commit()

    for _ in range(5):
        make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    pacing = PredictivePacingEngine(db)
    decision = pacing.compute_pacing(campaign)

    # With 70% answer rate, desired_outstanding = ceil(5 / 0.7) = 8
    assert decision.estimated_answer_rate == pytest.approx(0.70, abs=0.01)
    assert decision.desired_outstanding == math.ceil(5 / 0.70)


def test_predictive_falls_back_to_progressive_on_low_answer_rate(db: Session):
    """Pacing falls back to progressive mode when answer rate < 10%."""
    campaign = make_campaign(db, seed_answer_rate=0.05)
    campaign.total_calls_initiated = 100
    campaign.total_calls_answered = 3  # 3% answer rate
    db.commit()
    make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    pacing = PredictivePacingEngine(db)
    decision = pacing.compute_pacing(campaign)

    # Should fall back to progressive (mode == "progressive")
    # Answer rate 3% < 5% floor, gets clamped to 5%, still < 10% threshold
    assert decision.mode == "progressive"


# ---------------------------------------------------------------------------
# Test 5: Safety Controller rejecting excessive requests
# ---------------------------------------------------------------------------

def test_safety_controller_rejects_when_zero_capacity(db: Session):
    """Safety Controller approves 0 when no agent slots are free."""
    campaign = make_campaign(db)
    # No agents at all

    controller = SafetyController(db)
    decision = controller.check_predictive(
        campaign_id=campaign.id,
        provider_name="provider_a",
        requested_count=10,
        pacing_reason="test",
    )

    assert decision.approved_count == 0
    assert "REJECT" in decision.reason or decision.approved_count == 0


def test_safety_controller_reduces_excessive_request(db: Session):
    """Safety Controller reduces 20-call request when only 5 agent slots exist."""
    campaign = make_campaign(db)
    for _ in range(5):
        make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    controller = SafetyController(db)
    decision = controller.check_predictive(
        campaign_id=campaign.id,
        provider_name="provider_a",
        requested_count=20,
        pacing_reason="pacing wants 20",
    )

    assert decision.requested_count == 20
    assert decision.approved_count == 5  # Capped at available agents
    assert "REDUCE" in decision.reason


def test_safety_controller_unhealthy_provider(db: Session):
    """Safety Controller rejects all calls when provider is unhealthy."""
    from app.providers import get_provider
    campaign = make_campaign(db, provider_name="provider_a")
    for _ in range(5):
        make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    provider = get_provider("provider_a")
    provider.set_outage(True)
    try:
        controller = SafetyController(db)
        decision = controller.check_predictive(
            campaign_id=campaign.id,
            provider_name="provider_a",
            requested_count=5,
            pacing_reason="test",
        )
        assert decision.approved_count == 0
        assert not decision.provider_healthy
    finally:
        provider.set_outage(False)


def test_safety_controller_prevents_borrower_double_call(db: Session):
    """Safety controller detects an active call for a borrower."""
    import uuid
    from app.models.call import Call

    campaign = make_campaign(db)
    make_agent(db, campaign.id, status=AgentStatus.DIALING)
    borrower = make_borrower(db, campaign.id)

    # Create an active call for this borrower
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        borrower_id=borrower.id,
        agent_id=None,
        status=CallStatus.RINGING,
    )
    db.add(call)
    db.commit()

    controller = SafetyController(db)
    can_call = controller.can_call_borrower(borrower.id)
    assert can_call is False
