"""
Campaign Manager — orchestrates campaign-level dialing decisions.

Provides:
  - Progressive dialing: exactly 1 call per available agent
  - Predictive dialing: pacing engine determines call count

Architecture:
  CampaignManager
    → PredictivePacingEngine  (how many calls?)
      → SafetyController      (is it actually safe?)
    → CallAllocator           (reserve agent + borrower, create call)
    → TelecomProvider         (initiate the actual call)

The campaign manager never calls the provider directly.
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from typing import Optional, List

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallStatus
from app.models.campaign import Campaign, CampaignStatus
from app.providers import get_provider, CallInitRequest
from app.services.call_allocator import CallAllocator
from app.services.pacing_engine import PredictivePacingEngine, PacingDecision
from app.services.safety_controller import SafetyController, SafetyDecision
from app.services.event_processor import EventProcessor

logger = logging.getLogger(__name__)


@dataclass
class DialResult:
    calls_initiated: int
    calls_attempted: int
    calls_failed: int
    pacing_decision: Optional[PacingDecision]
    safety_decision: Optional[SafetyDecision]
    errors: List[str]


class CampaignManager:
    """
    Top-level orchestrator for dialing campaigns.

    Implements the full call lifecycle from "select borrower" to "provider
    returns a call ID", and wires together pacing, safety, allocation, and
    provider call initiation.
    """

    def __init__(self, db: Session):
        self._db = db
        self._allocator = CallAllocator(db)
        self._safety = SafetyController(db)
        self._pacing = PredictivePacingEngine(db, self._safety)

    async def progressive_dial(self, campaign_id: str) -> DialResult:
        """
        Progressive dialing: one call per available agent.

        Selects eligible borrowers, atomically reserves agents and borrowers,
        creates call records, and initiates calls through the provider.
        """
        campaign = self._get_campaign(campaign_id)
        if campaign is None:
            return DialResult(0, 0, 0, None, None, [f"Campaign {campaign_id} not found"])
        if campaign.status != CampaignStatus.ACTIVE:
            return DialResult(0, 0, 0, None, None, [f"Campaign {campaign_id} is not ACTIVE"])

        # Return agents who finished wrap-up back to AVAILABLE
        self._release_wrap_up_agents(campaign_id)

        safety_decision = self._safety.check_progressive(campaign_id, campaign.provider_name)
        approved = safety_decision.approved_count

        if approved == 0:
            return DialResult(
                calls_initiated=0,
                calls_attempted=0,
                calls_failed=0,
                pacing_decision=None,
                safety_decision=safety_decision,
                errors=[safety_decision.reason],
            )

        return await self._dial_n_calls(campaign, approved, None, safety_decision)

    async def predictive_dial(self, campaign_id: str) -> DialResult:
        """
        Predictive dialing: pacing engine determines call count.
        Safety controller may reduce the approved count.
        """
        campaign = self._get_campaign(campaign_id)
        if campaign is None:
            return DialResult(0, 0, 0, None, None, [f"Campaign {campaign_id} not found"])
        if campaign.status != CampaignStatus.ACTIVE:
            return DialResult(0, 0, 0, None, None, [f"Campaign {campaign_id} is not ACTIVE"])

        # Return agents who finished wrap-up back to AVAILABLE
        self._release_wrap_up_agents(campaign_id)

        pacing_decision = self._pacing.compute_pacing(campaign)
        safety_decision = pacing_decision.safety_decision
        approved = safety_decision.approved_count

        if safety_decision.fallback_to_progressive:
            logger.info("Falling back to progressive dialing for campaign %s", campaign_id)
            return await self.progressive_dial(campaign_id)

        if approved == 0:
            return DialResult(
                calls_initiated=0,
                calls_attempted=0,
                calls_failed=0,
                pacing_decision=pacing_decision,
                safety_decision=safety_decision,
                errors=[safety_decision.reason],
            )

        return await self._dial_n_calls(campaign, approved, pacing_decision, safety_decision)

    async def _dial_n_calls(
        self,
        campaign: Campaign,
        n: int,
        pacing_decision: Optional[PacingDecision],
        safety_decision: SafetyDecision,
    ) -> DialResult:
        """
        Attempt to initiate up to N calls for a campaign.

        For each call:
          1. Select an eligible borrower.
          2. Check borrower eligibility via safety controller.
          3. Atomically allocate (reserve agent + borrower + create call).
          4. Initiate call via provider.
          5. On provider failure, release all resources safely.
          6. On provider success, advance call to INITIATED.
        """
        initiated = 0
        attempted = 0
        failed = 0
        errors = []

        for _ in range(n):
            borrower = self._select_borrower(campaign.id)
            if borrower is None:
                errors.append("No eligible borrowers remaining")
                break

            # Check for duplicate active calls for this borrower
            if not self._safety.can_call_borrower(borrower.id):
                errors.append(f"Borrower {borrower.id} already has an active call")
                continue

            attempted += 1

            # Atomic allocation
            allocation = self._allocator.allocate(campaign.id, borrower)
            if not allocation.success:
                errors.append(f"Allocation failed: {allocation.error}")
                failed += 1
                continue

            # Check agent still exists (disappearance race)
            agent = self._db.get(Agent, allocation.agent_id)
            if agent is None:
                self._allocator.fail_call_and_release(
                    allocation.call_id,
                    allocation.agent_id,
                    allocation.borrower_id,
                    "Agent disappeared during setup",
                )
                errors.append(f"Agent {allocation.agent_id} disappeared during setup")
                failed += 1
                continue

            # Initiate call through provider
            try:
                provider = get_provider(campaign.provider_name)
                provider_request = CallInitRequest(
                    call_id=allocation.call_id,
                    campaign_id=campaign.id,
                    borrower_id=allocation.borrower_id,
                    phone_number=borrower.phone_number,
                    agent_id=allocation.agent_id,
                )
                response = await provider.initiate_call(provider_request)
            except Exception as exc:
                self._allocator.fail_call_and_release(
                    allocation.call_id,
                    allocation.agent_id,
                    allocation.borrower_id,
                    f"Provider exception: {exc}",
                )
                errors.append(f"Provider exception: {exc}")
                failed += 1
                continue

            if not response.success:
                self._allocator.fail_call_and_release(
                    allocation.call_id,
                    allocation.agent_id,
                    allocation.borrower_id,
                    f"Provider failure: {response.error}",
                )
                errors.append(f"Provider failure: {response.error}")
                failed += 1
                continue

            # Advance to INITIATED
            advanced = self._allocator.advance_call_to_initiated(
                call_id=allocation.call_id,
                agent_id=allocation.agent_id,
                provider_name=campaign.provider_name,
                provider_call_id=response.provider_call_id,
            )
            if not advanced:
                # Call may have been failed by a concurrent process
                errors.append(f"Could not advance call {allocation.call_id} to INITIATED")
                failed += 1
                continue

            # Update campaign stats
            campaign.total_calls_initiated += 1
            self._db.commit()

            initiated += 1
            logger.info(
                "Call initiated: call=%s provider=%s agent=%s",
                allocation.call_id, response.provider_call_id, allocation.agent_id,
            )

        return DialResult(
            calls_initiated=initiated,
            calls_attempted=attempted,
            calls_failed=failed,
            pacing_decision=pacing_decision,
            safety_decision=safety_decision,
            errors=errors,
        )

    def _select_borrower(self, campaign_id: str) -> Optional[Borrower]:
        """Select the next eligible borrower: PENDING, retry_count < max_retries."""
        return self._db.execute(
            select(Borrower)
            .where(Borrower.campaign_id == campaign_id)
            .where(Borrower.status == BorrowerStatus.PENDING)
            .where(Borrower.retry_count < Borrower.max_retries)
            .order_by(Borrower.retry_count.asc(), Borrower.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()

    def _get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        return self._db.get(Campaign, campaign_id)

    def _release_wrap_up_agents(self, campaign_id: str) -> int:
        """Automatically return agents in WRAP_UP to AVAILABLE for the next cycle."""
        from sqlalchemy import text
        from datetime import datetime, timezone
        result = self._db.execute(
            text(
                "UPDATE agents SET status = 'AVAILABLE', current_call_id = NULL, "
                "lease_expires_at = NULL, updated_at = :now "
                "WHERE campaign_id = :cid AND status = 'WRAP_UP'"
            ),
            {"cid": campaign_id, "now": datetime.now(timezone.utc).isoformat()},
        )
        self._db.commit()
        return result.rowcount
