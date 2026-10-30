"""
FastAPI router: Simulation
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import SimulationScenario, SimulationResult
from app.database import get_db, SessionLocal
from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.campaign import Campaign, CampaignStatus
from app.providers import get_provider, ProviderA, ProviderB, ProviderEvent, ProviderEventType
from app.providers import dispatch_event, register_event_handler, clear_event_handlers
from app.services.campaign_manager import CampaignManager
from app.services.event_processor import EventProcessor
from app.services.metrics import AgentUtilizationTracker

router = APIRouter(tags=["simulation"])


@router.post("/simulation/run", response_model=SimulationResult)
async def run_simulation(scenario: SimulationScenario, db: Session = Depends(get_db)):
    """
    Run a deterministic simulation scenario.

    Creates a fresh campaign, agents, and borrowers, then runs progressive or
    predictive dialing and simulates the provider call flow.
    """
    rng = random.Random(scenario.random_seed)

    # --- Setup campaign ---
    campaign = Campaign(
        id=str(uuid.uuid4()),
        name=f"sim-{scenario.name}-{uuid.uuid4().hex[:6]}",
        status=CampaignStatus.ACTIVE,
        provider_name=scenario.provider_name,
        seed_answer_rate=scenario.answer_rate,
        max_concurrent_calls=scenario.num_agents,
    )
    db.add(campaign)

    # --- Setup agents ---
    agents = []
    for i in range(scenario.num_agents):
        agent = Agent(
            id=str(uuid.uuid4()),
            name=f"Agent-{i+1}",
            campaign_id=campaign.id,
            status=AgentStatus.AVAILABLE,
        )
        db.add(agent)
        agents.append(agent)

    # --- Setup borrowers ---
    borrowers = []
    for i in range(scenario.num_borrowers):
        b = Borrower(
            id=str(uuid.uuid4()),
            campaign_id=campaign.id,
            name=f"Borrower-{i+1}",
            phone_number=f"+1555{rng.randint(1000000,9999999)}",
            status=BorrowerStatus.PENDING,
        )
        db.add(b)
        borrowers.append(b)

    db.commit()

    # --- Configure provider for scenario ---
    provider = get_provider(scenario.provider_name)
    original_outage = not provider.is_healthy
    provider.configure(
        answer_rate=scenario.answer_rate,
        avg_talk_seconds=scenario.avg_talk_seconds,
    )

    pacing_log: List[str] = []
    safety_log: List[str] = []
    tracker = AgentUtilizationTracker(total_agents=scenario.num_agents)

    # --- Run dialing cycles ---
    calls_initiated_total = 0
    manager = CampaignManager(db)

    for cycle in range(scenario.num_borrowers):
        # Sample agent busy occupancy
        busy_count = db.execute(
            select(func.count(Agent.id))
            .where(Agent.campaign_id == campaign.id)
            .where(Agent.status.in_([AgentStatus.RESERVED, AgentStatus.DIALING, AgentStatus.CONNECTED, AgentStatus.WRAP_UP]))
        ).scalar_one()
        tracker.record_cycle(busy_count)

        # Dynamic outage / capacity events for outage scenarios
        if scenario.simulate_provider_outage:
            if cycle == 2:
                provider.set_outage(True)
                pacing_log.append(f"[Cycle {cycle}] Provider outage triggered")
            elif cycle == 4:
                provider.set_outage(False)
                pacing_log.append(f"[Cycle {cycle}] Provider recovered - Outage lifted")
            elif cycle == 6:
                avail_to_pause = db.execute(
                    select(Agent)
                    .where(Agent.campaign_id == campaign.id)
                    .where(Agent.status == AgentStatus.AVAILABLE)
                    .limit(2)
                ).scalars().all()
                for ag in avail_to_pause:
                    ag.status = AgentStatus.PAUSED
                db.commit()
                pacing_log.append(f"[Cycle {cycle}] Agent capacity dropped: {len(avail_to_pause)} agent(s) paused")
            elif cycle == 8:
                paused = db.execute(
                    select(Agent)
                    .where(Agent.campaign_id == campaign.id)
                    .where(Agent.status == AgentStatus.PAUSED)
                ).scalars().all()
                for ag in paused:
                    ag.status = AgentStatus.AVAILABLE
                db.commit()
                pacing_log.append(f"[Cycle {cycle}] Agent capacity restored: {len(paused)} agent(s) returned")
            elif cycle == 10:
                new_rate = 0.15
                provider.configure(answer_rate=new_rate)
                campaign.seed_answer_rate = new_rate
                db.commit()
                pacing_log.append(f"[Cycle {cycle}] Answer rate changed to {new_rate:.2f}")

        if scenario.mode == "predictive":
            result = await manager.predictive_dial(campaign.id)
        else:
            result = await manager.progressive_dial(campaign.id)

        calls_initiated_total += result.calls_initiated

        if result.pacing_decision:
            pacing_log.append(
                f"[Cycle {cycle}] {result.pacing_decision.mode}: "
                f"requested={result.pacing_decision.requested_calls}, "
                f"approved={result.pacing_decision.safety_decision.approved_count}, "
                f"initiated={result.calls_initiated}"
            )
        if result.safety_decision:
            safety_log.append(f"[Cycle {cycle}] {result.safety_decision.reason}")

        # Small sleep to allow async provider events to fire
        await asyncio.sleep(0.08)

        # Check if all borrowers processed
        remaining = db.execute(
            select(func.count(Borrower.id))
            .where(Borrower.campaign_id == campaign.id)
            .where(Borrower.status == BorrowerStatus.PENDING)
        ).scalar_one()
        if remaining == 0:
            pacing_log.append(f"[Cycle {cycle}] All borrowers processed, stopping")
            break

    # Wait a bit for provider events to finish
    await asyncio.sleep(1.0)

    # Restore provider state
    provider.set_outage(original_outage)

    # --- Collect statistics ---
    def count_calls(stat: CallStatus) -> int:
        return db.execute(
            select(func.count(Call.id))
            .where(Call.campaign_id == campaign.id)
            .where(Call.status == stat)
        ).scalar_one()

    initiated = db.execute(
        select(func.count(Call.id)).where(Call.campaign_id == campaign.id)
    ).scalar_one()
    answered = db.execute(
        select(func.count(Call.id))
        .where(Call.campaign_id == campaign.id)
        .where(Call.answered_at.is_not(None))
    ).scalar_one()
    connected = count_calls(CallStatus.CONNECTED) + count_calls(CallStatus.COMPLETED)
    completed = count_calls(CallStatus.COMPLETED)
    failed = count_calls(CallStatus.FAILED)

    utilization = tracker.compute_duty_cycle_utilization()

    summary = (
        f"Scenario '{scenario.name}': "
        f"answer_rate={scenario.answer_rate:.0%}, "
        f"avg_talk={scenario.avg_talk_seconds}s, "
        f"initiated={initiated}, answered={answered}, "
        f"completed={completed}, failed={failed}"
    )

    return SimulationResult(
        scenario_name=scenario.name,
        total_agents=scenario.num_agents,
        calls_initiated=initiated,
        calls_answered=answered,
        calls_connected=connected,
        calls_completed=completed,
        calls_failed=failed,
        agent_utilization_pct=round(utilization, 1),
        pacing_decisions=pacing_log[:20],   # cap for response size
        safety_decisions=safety_log[:20],
        summary=summary,
    )
