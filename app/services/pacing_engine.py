"""
Predictive Pacing Engine — explainable rule-based algorithm.

The pacing engine NEVER calls the provider directly.
It asks the Safety Controller for permission, which may reduce the count.

Algorithm:
  1. Measure current state: available agents, reserved agents, connected calls,
     ringing calls.
  2. Estimate answer rate (rolling, bounded, with conservative fallback).
  3. Calculate desired outstanding calls.
  4. Subtract existing outstanding calls.
  5. Request the delta from the Safety Controller.

Formula:
  estimated_answer_rate = rolling_rate (or seed if insufficient data)
  desired_outstanding = ceil(free_agent_capacity / estimated_answer_rate)
  requested_additional = max(0, desired_outstanding - existing_outstanding)

Safeguards:
  - Minimum answer rate floor (prevents division by very small numbers → explosion)
  - Maximum overdial increment per pacing cycle (prevents sudden rate spikes)
  - Fallback to progressive mode if provider health deteriorates
  - All final call counts are independently capped by the Safety Controller
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.call import Call, CallStatus
from app.models.campaign import Campaign
from app.services.safety_controller import SafetyController, SafetyDecision

logger = logging.getLogger(__name__)

# Pacing constants
MIN_ANSWER_RATE = 0.05          # Never assume answer rate below 5%
MAX_ANSWER_RATE = 0.95          # Cap to avoid under-dialing
MAX_OVERDIAL_INCREMENT = 5      # Max new calls per pacing cycle
PROGRESSIVE_FALLBACK_THRESHOLD = 0.10  # Fall back if answer rate drops below 10%


@dataclass
class PacingDecision:
    """Full explanation of a pacing engine decision."""
    requested_calls: int
    available_agents: int
    reserved_agents: int
    connected_calls: int
    ringing_calls: int
    existing_outstanding: int
    estimated_answer_rate: float
    desired_outstanding: int
    safety_decision: SafetyDecision
    explanation: str
    mode: str  # "progressive" | "predictive"


class PredictivePacingEngine:
    """
    Explainable rule-based predictive pacing engine.

    Separating the pacing engine from the safety controller is intentional:
    - The pacing engine optimises for efficiency (dial more when answer rates are high).
    - The safety controller enforces hard safety invariants (never exceed agent capacity).
    - This separation means a pacing bug cannot cause a safety violation.
    """

    def __init__(self, db: Session, safety_controller: Optional[SafetyController] = None):
        self._db = db
        self._safety = safety_controller or SafetyController(db)

    def compute_pacing(self, campaign: Campaign) -> PacingDecision:
        """
        Compute how many new calls to initiate for this campaign.

        Returns a PacingDecision with a full explanation and the Safety
        Controller's final approved count.
        """
        # --- Step 1: Snapshot current state ---
        available_agents = self._count_agents(campaign.id, AgentStatus.AVAILABLE)
        reserved_agents = self._count_agents(campaign.id, AgentStatus.RESERVED)
        dialing_agents = self._count_agents(campaign.id, AgentStatus.DIALING)
        connected_agents = self._count_agents(campaign.id, AgentStatus.CONNECTED)

        ringing_calls = self._count_calls(campaign.id, CallStatus.RINGING)
        initiated_calls = self._count_calls(campaign.id, CallStatus.INITIATED)
        answered_calls = self._count_calls(campaign.id, CallStatus.ANSWERED)
        connected_calls = self._count_calls(campaign.id, CallStatus.CONNECTED)

        # Outstanding = calls that are in-flight but not yet consuming agent time
        # (ringing + initiated, but not yet answered/connected)
        existing_outstanding = ringing_calls + initiated_calls

        # Agent capacity = agents currently free (not in a call)
        # Agents in RESERVED/DIALING/CONNECTED are already committed
        committed_agents = reserved_agents + dialing_agents + connected_agents
        free_agent_capacity = max(0, available_agents - 0)  # available means truly free

        # --- Step 2: Estimate answer rate ---
        raw_answer_rate = campaign.estimated_answer_rate
        answer_rate = max(MIN_ANSWER_RATE, min(MAX_ANSWER_RATE, raw_answer_rate))

        # --- Step 3: Choose mode ---
        # Fall back to progressive if answer rate is very low or provider is unhealthy
        provider = self._get_provider_health(campaign.provider_name)
        if answer_rate < PROGRESSIVE_FALLBACK_THRESHOLD or not provider:
            mode = "progressive"
            requested_calls = 1
            explanation = (
                f"Progressive fallback: answer_rate={answer_rate:.2%} "
                f"(threshold={PROGRESSIVE_FALLBACK_THRESHOLD:.2%}) or unhealthy provider"
            )
            safety_decision = self._safety.check_progressive(
                campaign.id, campaign.provider_name
            )
            return PacingDecision(
                requested_calls=requested_calls,
                available_agents=available_agents,
                reserved_agents=reserved_agents,
                connected_calls=connected_calls,
                ringing_calls=ringing_calls,
                existing_outstanding=existing_outstanding,
                estimated_answer_rate=answer_rate,
                desired_outstanding=1,
                safety_decision=safety_decision,
                explanation=explanation,
                mode=mode,
            )

        # --- Step 4: Predictive calculation ---
        # How many calls should be outstanding to keep all free agents busy?
        # desired_outstanding = ceil(free_agent_capacity / answer_rate)
        desired_outstanding = math.ceil(free_agent_capacity / answer_rate)

        # How many additional calls do we need to initiate?
        raw_additional = max(0, desired_outstanding - existing_outstanding)

        # Apply conservative increment cap
        capped_additional = min(raw_additional, MAX_OVERDIAL_INCREMENT)

        mode = "predictive"
        explanation = (
            f"Predictive pacing: "
            f"available_agents={available_agents}, "
            f"free_capacity={free_agent_capacity}, "
            f"answer_rate={answer_rate:.2%}, "
            f"desired_outstanding=ceil({free_agent_capacity}/{answer_rate:.2f})={desired_outstanding}, "
            f"existing_outstanding={existing_outstanding}, "
            f"raw_additional={raw_additional}, "
            f"capped_at={capped_additional} (max_increment={MAX_OVERDIAL_INCREMENT})"
        )

        # --- Step 5: Ask Safety Controller ---
        safety_decision = self._safety.check_predictive(
            campaign_id=campaign.id,
            provider_name=campaign.provider_name,
            requested_count=capped_additional,
            pacing_reason=explanation,
        )

        return PacingDecision(
            requested_calls=capped_additional,
            available_agents=available_agents,
            reserved_agents=reserved_agents,
            connected_calls=connected_calls,
            ringing_calls=ringing_calls,
            existing_outstanding=existing_outstanding,
            estimated_answer_rate=answer_rate,
            desired_outstanding=desired_outstanding,
            safety_decision=safety_decision,
            explanation=explanation,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_agents(self, campaign_id: str, status: AgentStatus) -> int:
        return self._db.execute(
            select(func.count(Agent.id))
            .where(Agent.campaign_id == campaign_id)
            .where(Agent.status == status)
        ).scalar_one()

    def _count_calls(self, campaign_id: str, status: CallStatus) -> int:
        return self._db.execute(
            select(func.count(Call.id))
            .where(Call.campaign_id == campaign_id)
            .where(Call.status == status)
        ).scalar_one()

    def _get_provider_health(self, provider_name: str) -> bool:
        try:
            from app.providers import get_provider
            return get_provider(provider_name).is_healthy
        except Exception:
            return False
