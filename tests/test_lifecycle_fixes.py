"""
Tests for Fix 2 & Fix 3: Lifecycle transitions, statistics accuracy, and idempotency.

Verifies:
  - ANSWERED -> CONNECTED -> COMPLETED for agent-bound calls
  - ANSWERED -> FAILED
  - Duplicate ANSWERED does not double-count statistics
  - Late events after terminal states are ignored
  - A call that is answered and then fails still counts as answered in campaign stats
"""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallStatus
from app.models.campaign import Campaign
from app.providers import ProviderEvent, ProviderEventType
from app.services.event_processor import EventProcessor
from tests.conftest import make_campaign, make_agent, make_borrower


def _make_agent_bound_call(db: Session, campaign_id: str) -> tuple[Call, Agent, Borrower]:
    agent = make_agent(db, campaign_id, status=AgentStatus.DIALING)
    borrower = make_borrower(db, campaign_id, status=BorrowerStatus.RESERVED)
    call = Call(
        id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        borrower_id=borrower.id,
        agent_id=agent.id,
        status=CallStatus.RINGING,
        provider_name="provider_a",
        provider_call_id=f"pA-{uuid.uuid4().hex[:12]}",
    )
    db.add(call)
    db.commit()
    return call, agent, borrower


def test_answered_connected_completed_lifecycle(db: Session):
    """
    Agent-bound call lifecycle:
    RINGING -> ANSWERED (moves call and agent to CONNECTED) -> COMPLETED (call completed, agent in WRAP_UP).
    """
    campaign = make_campaign(db)
    call, agent, borrower = _make_agent_bound_call(db, campaign.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    # 1. ANSWERED event arrives
    ans_evt = ProviderEvent("evt-ans-1", "provider_a", pcid, ProviderEventType.ANSWERED)
    res_ans = processor.process(ans_evt)
    assert res_ans is True

    db.expire_all()
    call_ans = db.get(Call, call.id)
    agent_ans = db.get(Agent, agent.id)
    camp_ans = db.get(Campaign, campaign.id)

    # Call and agent are now CONNECTED
    assert call_ans.status == CallStatus.CONNECTED
    assert call_ans.answered_at is not None
    assert call_ans.connected_at is not None
    assert agent_ans.status == AgentStatus.CONNECTED

    # Campaign total_calls_answered must be incremented upon answer
    assert camp_ans.total_calls_answered == 1

    # 2. COMPLETED event arrives
    comp_evt = ProviderEvent(
        "evt-comp-1", "provider_a", pcid, ProviderEventType.COMPLETED,
        metadata={"duration_seconds": 95},
    )
    res_comp = processor.process(comp_evt)
    assert res_comp is True

    db.expire_all()
    call_comp = db.get(Call, call.id)
    agent_comp = db.get(Agent, agent.id)
    borrower_comp = db.get(Borrower, borrower.id)
    camp_comp = db.get(Campaign, campaign.id)

    assert call_comp.status == CallStatus.COMPLETED
    assert call_comp.talk_seconds == 95
    assert agent_comp.status == AgentStatus.WRAP_UP
    assert borrower_comp.status == BorrowerStatus.COMPLETED

    # total_calls_answered must NOT have incremented again (stays 1)
    assert camp_comp.total_calls_answered == 1
    assert camp_comp.total_talk_seconds == 95


def test_answered_then_failed_lifecycle(db: Session):
    """
    A call that is answered and then fails:
    - Still counted as answered in campaign stats.
    - Call ends in FAILED.
    - Agent returns to AVAILABLE.
    - Borrower is returned to PENDING for retry.
    """
    campaign = make_campaign(db)
    call, agent, borrower = _make_agent_bound_call(db, campaign.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    # 1. ANSWERED
    processor.process(ProviderEvent("evt-ans-f", "provider_a", pcid, ProviderEventType.ANSWERED))

    db.expire_all()
    assert db.get(Campaign, campaign.id).total_calls_answered == 1

    # 2. FAILED (e.g. caller hung up abnormally)
    fail_evt = ProviderEvent(
        "evt-fail-f", "provider_a", pcid, ProviderEventType.FAILED,
        metadata={"reason": "connection-dropped"},
    )
    processor.process(fail_evt)

    db.expire_all()
    call_db = db.get(Call, call.id)
    agent_db = db.get(Agent, agent.id)
    borrower_db = db.get(Borrower, borrower.id)
    camp_db = db.get(Campaign, campaign.id)

    assert call_db.status == CallStatus.FAILED
    assert call_db.failure_reason == "connection-dropped"
    assert agent_db.status == AgentStatus.AVAILABLE
    assert borrower_db.status == BorrowerStatus.PENDING
    assert borrower_db.retry_count == 1

    # total_calls_answered remains 1
    assert camp_db.total_calls_answered == 1


def test_duplicate_answered_does_not_double_count_statistics(db: Session):
    """
    Duplicate ANSWERED events must not double increment campaign statistics.
    """
    campaign = make_campaign(db)
    call, agent, borrower = _make_agent_bound_call(db, campaign.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    # First ANSWERED
    processor.process(ProviderEvent("evt-dup-ans-1", "provider_a", pcid, ProviderEventType.ANSWERED))

    # Duplicate ANSWERED with same event ID
    processor.process(ProviderEvent("evt-dup-ans-1", "provider_a", pcid, ProviderEventType.ANSWERED))

    # Duplicate ANSWERED with different event ID
    processor.process(ProviderEvent("evt-dup-ans-2", "provider_a", pcid, ProviderEventType.ANSWERED))

    db.expire_all()
    camp_db = db.get(Campaign, campaign.id)
    assert camp_db.total_calls_answered == 1, (
        f"Expected total_calls_answered=1, got {camp_db.total_calls_answered}"
    )


def test_late_events_after_terminal_states_ignored(db: Session):
    """
    Once a call reaches COMPLETED or FAILED, late RINGING, ANSWERED, or CONNECTED events
    are dropped and cause no state regressions.
    """
    campaign = make_campaign(db)
    call, agent, borrower = _make_agent_bound_call(db, campaign.id)
    pcid = call.provider_call_id

    processor = EventProcessor(db)

    # Progress to COMPLETED
    processor.process(ProviderEvent("evt-seq-1", "provider_a", pcid, ProviderEventType.ANSWERED))
    processor.process(ProviderEvent("evt-seq-2", "provider_a", pcid, ProviderEventType.COMPLETED))

    db.expire_all()
    assert db.get(Call, call.id).status == CallStatus.COMPLETED

    # Late out-of-order events
    processor.process(ProviderEvent("evt-late-ring", "provider_a", pcid, ProviderEventType.RINGING))
    processor.process(ProviderEvent("evt-late-ans", "provider_a", pcid, ProviderEventType.ANSWERED))
    processor.process(ProviderEvent("evt-late-fail", "provider_a", pcid, ProviderEventType.FAILED))

    db.expire_all()
    call_final = db.get(Call, call.id)
    assert call_final.status == CallStatus.COMPLETED
