"""
Safety Controller — the most important component.

Responsibilities:
  1. Receive requested call counts from the pacing engine.
  2. Calculate actual safe capacity.
  3. Approve, reduce, reject, or fall back to progressive dialing.
  4. Prevent agent over-allocation.
  5. Prevent duplicate borrower calls.
  6. Enforce provider health restrictions.
  7. Refuse to let predictive logic disable safety.

Key distinction:
  - "Ringing without an assigned agent": A call that was sent to the provider
    but has not yet been attached to a specific agent. In predictive dialers
    this is the "abandoned call" risk zone.
  - "Agent-bound call": A call where an agent has already been RESERVED.
    These directly consume agent capacity.

In this implementation (progressive + predictive), every call that passes
through the Safety Controller is agent-bound before it reaches the provider.
The controller enforces: active_agent_bound_calls + requested ≤ available_agents.

This is a conservative approach that prevents abandoned calls entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.call import Call, CallStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.providers import get_provider

logger = logging.getLogger(__name__)


@dataclass
class SafetyDecision:
    """The output of the Safety Controller for a given request."""
    approved_count: int           # How many calls the controller permits
    requested_count: int          # What was requested
    reason: str                   # Human-readable explanation
    fallback_to_progressive: bool = False  # Pacing engine should use progressive mode
    provider_healthy: bool = True


class SafetyController:
    """
    Independent safety layer between the pacing engine and the call allocator.

    This component must NEVER be bypassed. The predictive pacing engine
    asks for N calls; the controller decides how many are actually safe.

    The controller uses the database as the source of truth — not in-memory
    state — to handle multi-worker scenarios correctly (though SQLite's single-
    writer model is the ultimate concurrency guarantee at this prototype scale).
    """

    # Hard ceiling: never allow more outstanding calls than available agent slots
    MAX_CALLS_PER_AVAILABLE_AGENT = 1  # progressive mode guarantee
    # In predictive mode we allow a small over-dial ratio
    MAX_PREDICTIVE_OVERDIAL_RATIO = 1.5

    def __init__(self, db: Session):
        self._db = db

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check_progressive(
        self,
        campaign_id: str,
        provider_name: str,
    ) -> SafetyDecision:
        """
        Progressive mode: approve exactly 1 call per available agent.
        Returns the number of safe new calls (available agents - already-active
        agent-bound calls).
        """
        provider = get_provider(provider_name)
        if not provider.is_healthy:
            return SafetyDecision(
                approved_count=0,
                requested_count=1,
                reason="Provider unhealthy - no new calls permitted",
                provider_healthy=False,
            )

        available, agent_bound_active = self._capacity_snapshot(campaign_id)
        # Progressive: total agent-bound calls must not exceed available agents
        safe_slots = max(0, available - agent_bound_active)

        return SafetyDecision(
            approved_count=safe_slots,
            requested_count=1,
            reason=(
                f"Progressive: {available} available agents, "
                f"{agent_bound_active} active agent-bound calls -> "
                f"{safe_slots} slot(s) open"
            ),
            provider_healthy=True,
        )

    def check_predictive(
        self,
        campaign_id: str,
        provider_name: str,
        requested_count: int,
        pacing_reason: str,
    ) -> SafetyDecision:
        """
        Predictive mode: the pacing engine requests N calls.
        The controller independently re-verifies capacity and may reduce or
        reject the request.
        """
        provider = get_provider(provider_name)
        if not provider.is_healthy:
            logger.warning("Provider %s unhealthy; falling back to progressive", provider_name)
            return SafetyDecision(
                approved_count=0,
                requested_count=requested_count,
                reason=f"Provider {provider_name} unhealthy -> fallback to progressive",
                fallback_to_progressive=True,
                provider_healthy=False,
            )

        available, agent_bound_active = self._capacity_snapshot(campaign_id)

        # Hard limit: total agent-bound calls (existing + new) <= available agents
        max_new = max(0, available - agent_bound_active)

        # Apply overdial cap only if we have enough data
        capped = min(requested_count, max_new)

        if capped == 0:
            reason = (
                f"Safety REJECT: pacing requested {requested_count}, "
                f"but {available} available agents - {agent_bound_active} active = "
                f"0 free slots. Pacing said: [{pacing_reason}]"
            )
        elif capped < requested_count:
            reason = (
                f"Safety REDUCE: pacing requested {requested_count} -> "
                f"approved {capped}. "
                f"({available} avail agents - {agent_bound_active} active = {max_new} slots). "
                f"Pacing said: [{pacing_reason}]"
            )
        else:
            reason = (
                f"Safety APPROVE: {requested_count} call(s). "
                f"({available} avail - {agent_bound_active} active = {max_new} slots). "
                f"Pacing said: [{pacing_reason}]"
            )

        logger.info(reason)
        return SafetyDecision(
            approved_count=capped,
            requested_count=requested_count,
            reason=reason,
            provider_healthy=True,
        )

    def can_call_borrower(self, borrower_id: str) -> bool:
        """
        Check whether this borrower has an active call already.
        Prevents duplicate calls to the same borrower.
        """
        active_statuses = [
            CallStatus.RESERVED,
            CallStatus.INITIATED,
            CallStatus.RINGING,
            CallStatus.ANSWERED,
            CallStatus.CONNECTED,
        ]
        existing = self._db.execute(
            select(func.count(Call.id))
            .where(Call.borrower_id == borrower_id)
            .where(Call.status.in_(active_statuses))
        ).scalar_one()
        return existing == 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _capacity_snapshot(self, campaign_id: str) -> tuple[int, int]:
        """
        Returns (available_agents, agent_bound_active_calls) for the campaign.

        available_agents: agents in AVAILABLE state assigned to this campaign.
        agent_bound_active_calls: calls in RESERVED/INITIATED/RINGING/ANSWERED/CONNECTED
            state that already have an agent_id assigned (agent-bound).
        """
        available = self._db.execute(
            select(func.count(Agent.id))
            .where(Agent.campaign_id == campaign_id)
            .where(Agent.status == AgentStatus.AVAILABLE)
        ).scalar_one()

        agent_bound_statuses = [
            CallStatus.RESERVED,
            CallStatus.INITIATED,
            CallStatus.RINGING,
            CallStatus.ANSWERED,
            CallStatus.CONNECTED,
        ]
        agent_bound_active = self._db.execute(
            select(func.count(Call.id))
            .where(Call.campaign_id == campaign_id)
            .where(Call.agent_id.is_not(None))
            .where(Call.status.in_(agent_bound_statuses))
        ).scalar_one()

        return available, agent_bound_active
