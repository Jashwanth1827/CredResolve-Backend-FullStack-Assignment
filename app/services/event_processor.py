"""
Event Processor — handles incoming provider events idempotently.

Idempotency mechanism:
  1. Before processing any event, attempt to INSERT into provider_events
     with the (event_id, provider_name) unique constraint.
  2. If the INSERT fails with a unique constraint violation, the event has
     already been processed → discard silently.
  3. Only if the INSERT succeeds do we update the call/agent state.

Out-of-order event handling:
  - Terminal states (COMPLETED, FAILED, CANCELLED) cannot be regressed.
  - Events that would move a call *backward* in the state machine are ignored.
  - The valid transition table enforces forward-only progression.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallStatus, CALL_VALID_TRANSITIONS, TERMINAL_CALL_STATES
from app.models.campaign import Campaign
from app.models.provider_event import ProviderEvent
from app.providers import ProviderEvent as PEvent, ProviderEventType

logger = logging.getLogger(__name__)


class EventProcessor:
    """
    Processes provider events idempotently and advances call/agent state.
    """

    def __init__(self, db: Session):
        self._db = db

    def process(self, event: PEvent) -> bool:
        """
        Process one provider event.
        Returns True if the event was processed, False if it was a duplicate.
        """
        # Step 1: Idempotency check — try to record this event
        recorded = self._record_event(event)
        if not recorded:
            logger.info(
                "Duplicate event ignored: event_id=%s provider=%s type=%s",
                event.event_id, event.provider_name, event.event_type,
            )
            return False

        # Step 2: Find the call by provider_call_id (or metadata call_id fallback)
        call_id = event.metadata.get("call_id") if event.metadata else None
        call = self._find_call(event.provider_call_id, event.provider_name, call_id)
        if call is None:
            logger.warning(
                "No call found for provider_call_id=%s (event_id=%s)",
                event.provider_call_id, event.event_id,
            )
            return True  # Recorded but nothing to update

        # Step 3: Map event type to desired call state
        desired_status = self._event_to_call_status(event.event_type, call)
        if desired_status is None:
            return True  # Unknown event type; nothing to do

        # Step 4: Validate transition (prevents out-of-order regression)
        if call.status in TERMINAL_CALL_STATES:
            logger.info(
                "Ignoring event %s: call %s is already in terminal state %s",
                event.event_type, call.id, call.status,
            )
            return True

        valid_next = CALL_VALID_TRANSITIONS.get(call.status, set())
        if desired_status not in valid_next:
            logger.info(
                "Ignoring out-of-order event %s → %s: call %s currently in %s (allowed: %s)",
                event.event_type, desired_status, call.id, call.status, valid_next,
            )
            return True

        # Step 5: Apply state transition
        try:
            self._apply_transition(call, desired_status, event)
            self._db.commit()
            logger.info(
                "Call %s: %s → %s (event=%s)",
                call.id, call.status, desired_status, event.event_id,
            )
            return True
        except Exception as e:
            self._db.rollback()
            logger.exception("Failed to apply transition: %s", e)
            return False

    def _record_event(self, event: PEvent) -> bool:
        """
        Insert the event into provider_events.
        Returns True if inserted (new event), False if already exists (duplicate).
        """
        try:
            db_event = ProviderEvent(
                event_id=event.event_id,
                provider_name=event.provider_name,
                provider_call_id=event.provider_call_id,
                event_type=event.event_type.value,
                raw_payload=json.dumps(event.metadata),
            )
            self._db.add(db_event)
            self._db.commit()  # Durably record event for idempotency
            return True
        except IntegrityError:
            self._db.rollback()
            return False

    def _find_call(
        self, provider_call_id: str, provider_name: str, call_id: Optional[str] = None
    ) -> Optional[Call]:
        call = self._db.execute(
            select(Call)
            .where(Call.provider_call_id == provider_call_id)
            .where(Call.provider_name == provider_name)
        ).scalar_one_or_none()

        if call is None and call_id:
            call = self._db.get(Call, call_id)
            if call and not call.provider_call_id:
                call.provider_call_id = provider_call_id
                call.provider_name = provider_name
                self._db.flush()

        return call

    def _event_to_call_status(
        self, event_type: ProviderEventType, call: Optional[Call] = None
    ) -> Optional[CallStatus]:
        if event_type == ProviderEventType.INITIATED:
            return CallStatus.INITIATED
        elif event_type == ProviderEventType.RINGING:
            return CallStatus.RINGING
        elif event_type == ProviderEventType.ANSWERED:
            # For an agent-bound call, answering immediately connects to the agent
            return CallStatus.CONNECTED if (call and call.agent_id) else CallStatus.ANSWERED
        elif event_type == ProviderEventType.CONNECTED:
            return CallStatus.CONNECTED
        elif event_type == ProviderEventType.COMPLETED:
            return CallStatus.COMPLETED
        elif event_type == ProviderEventType.FAILED:
            return CallStatus.FAILED
        return None

    def _apply_transition(
        self, call: Call, new_status: CallStatus, event: PEvent
    ) -> None:
        """Apply the state transition to the call and update related entities."""
        now = datetime.now(timezone.utc)
        old_status = call.status
        call.status = new_status
        call.updated_at = now

        # Fix 2: Increment total_calls_answered once upon answer, even if call later fails
        if event.event_type == ProviderEventType.ANSWERED or new_status in {CallStatus.ANSWERED, CallStatus.CONNECTED}:
            if not call.answered_at:
                call.answered_at = now
                campaign = self._db.get(Campaign, call.campaign_id)
                if campaign:
                    campaign.total_calls_answered += 1

        if new_status == CallStatus.RINGING:
            pass  # No extra fields needed

        elif new_status == CallStatus.ANSWERED:
            pass  # Unbound call answered, waiting for agent

        elif new_status == CallStatus.CONNECTED:
            call.connected_at = now
            # Update agent to CONNECTED
            if call.agent_id:
                agent = self._db.get(Agent, call.agent_id)
                if agent and agent.status in {AgentStatus.RESERVED, AgentStatus.DIALING}:
                    agent.status = AgentStatus.CONNECTED
                    agent.updated_at = now

        elif new_status == CallStatus.COMPLETED:
            call.completed_at = now
            duration = event.metadata.get("duration_seconds")
            if duration:
                call.talk_seconds = int(duration)
            # Update campaign stats (total_calls_answered was already incremented at ANSWERED!)
            campaign = self._db.get(Campaign, call.campaign_id)
            if campaign and call.talk_seconds:
                campaign.total_talk_seconds += call.talk_seconds
            # Free up agent to WRAP_UP
            self._release_agent(call.agent_id, AgentStatus.WRAP_UP)
            # Mark borrower completed
            self._complete_borrower(call.borrower_id)

        elif new_status == CallStatus.FAILED:
            call.failure_reason = event.metadata.get("reason", "provider failure")
            # Free up agent back to AVAILABLE
            self._release_agent(call.agent_id, AgentStatus.AVAILABLE)
            # Release borrower for retry
            self._release_borrower_for_retry(call.borrower_id)

    def _release_agent(self, agent_id: Optional[str], new_status: AgentStatus) -> None:
        if not agent_id:
            return
        agent = self._db.get(Agent, agent_id)
        if agent and agent.status not in {AgentStatus.OFFLINE, AgentStatus.AVAILABLE}:
            agent.status = new_status
            agent.current_call_id = None
            agent.lease_expires_at = None
            agent.updated_at = datetime.now(timezone.utc)

    def _complete_borrower(self, borrower_id: str) -> None:
        borrower = self._db.get(Borrower, borrower_id)
        if borrower:
            borrower.status = BorrowerStatus.COMPLETED
            borrower.updated_at = datetime.now(timezone.utc)

    def _release_borrower_for_retry(self, borrower_id: str) -> None:
        borrower = self._db.get(Borrower, borrower_id)
        if borrower and borrower.status == BorrowerStatus.RESERVED:
            borrower.retry_count += 1
            if borrower.retry_count >= borrower.max_retries:
                borrower.status = BorrowerStatus.FAILED
            else:
                borrower.status = BorrowerStatus.PENDING
            borrower.updated_at = datetime.now(timezone.utc)
