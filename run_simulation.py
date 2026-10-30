"""
SmartDialer — Deterministic Simulation Runner.

Runs scenarios A, B, C, and D as required by the specification:
  Scenario A: Answer rate 20%, average talk time 120s
  Scenario B: Answer rate 50%, average talk time 90s
  Scenario C: Answer rate 70%, average talk time 180s
  Scenario D: Changing answer rate, provider latency, provider failures

Outputs:
  - Total agents
  - Available agents
  - Calls initiated
  - Calls answered
  - Calls connected
  - Calls completed
  - Calls failed
  - Agent utilization %
  - Pacing decisions
  - Safety Controller decisions
"""

from __future__ import annotations

import asyncio
import random
import uuid
import sys
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallStatus
from app.models.campaign import Campaign, CampaignStatus
from app.providers import (
    ProviderA, ProviderB, ProviderEvent, ProviderEventType,
    register_event_handler, clear_event_handlers
)
from app.services.campaign_manager import CampaignManager
from app.services.event_processor import EventProcessor
from app.services.metrics import AgentUtilizationTracker


@dataclass
class ScenarioConfig:
    name: str
    answer_rate: float
    avg_talk_seconds: int
    num_agents: int = 5
    num_borrowers: int = 20
    provider_type: str = "provider_a"  # "provider_a" or "provider_b"
    mode: str = "progressive"          # "progressive" or "predictive"
    simulate_outage: bool = False
    outage_after_calls: Optional[int] = None
    seed: int = 42


async def run_scenario(config: ScenarioConfig) -> dict:
    """Execute a single simulation scenario deterministically."""
    rng = random.Random(config.seed)

    # In-memory SQLite engine with StaticPool for simulation isolation
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session_ = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session_()

    clear_event_handlers()

    # Wire up provider with configured scenario parameters
    if config.provider_type == "provider_b":
        provider = ProviderB(
            rng=random.Random(config.seed),
            answer_rate=config.answer_rate,
            avg_talk_seconds=config.avg_talk_seconds,
        )
    else:
        provider = ProviderA(
            rng=random.Random(config.seed),
            answer_rate=config.answer_rate,
            avg_talk_seconds=config.avg_talk_seconds,
        )

    # Temporarily monkeypatch get_provider in the module for this test
    import app.providers
    original_providers = app.providers._PROVIDERS.copy()
    app.providers._PROVIDERS[config.provider_type] = provider

    # Register event handler that processes provider events into the DB
    async def sim_event_handler(event: ProviderEvent):
        # We need a session bound to the same engine
        handler_db = Session_()
        try:
            processor = EventProcessor(handler_db)
            processor.process(event)
        finally:
            handler_db.close()

    register_event_handler(sim_event_handler)

    try:
        # Create campaign
        campaign = Campaign(
            id=str(uuid.uuid4()),
            name=f"sim-{config.name}",
            status=CampaignStatus.ACTIVE,
            provider_name=config.provider_type,
            seed_answer_rate=config.answer_rate,
            max_concurrent_calls=config.num_agents,
        )
        db.add(campaign)

        # Create agents
        for i in range(config.num_agents):
            agent = Agent(
                id=str(uuid.uuid4()),
                name=f"Agent-{i+1}",
                campaign_id=campaign.id,
                status=AgentStatus.AVAILABLE,
            )
            db.add(agent)

        # Create borrowers
        for i in range(config.num_borrowers):
            b = Borrower(
                id=str(uuid.uuid4()),
                campaign_id=campaign.id,
                name=f"Borrower-{i+1}",
                phone_number=f"+1555{rng.randint(1000000, 9999999)}",
                status=BorrowerStatus.PENDING,
            )
            db.add(b)
        db.commit()

        # Run dialing cycles
        manager = CampaignManager(db)
        tracker = AgentUtilizationTracker(total_agents=config.num_agents)
        pacing_decisions: List[str] = []
        safety_decisions: List[str] = []

        calls_initiated_total = 0

        # Run cycles until all borrowers are processed or iterations cap
        max_cycles = config.num_borrowers * 2
        for cycle in range(max_cycles):
            # Sample agent busy occupancy for continuous duty-cycle utilization
            busy_count = db.execute(
                select(func.count(Agent.id))
                .where(Agent.campaign_id == campaign.id)
                .where(Agent.status.in_([AgentStatus.RESERVED, AgentStatus.DIALING, AgentStatus.CONNECTED, AgentStatus.WRAP_UP]))
            ).scalar_one()
            tracker.record_cycle(busy_count)

            # Scenario D dynamic events demonstration
            if config.simulate_outage:
                if cycle == 2:
                    provider.set_outage(True)
                    pacing_decisions.append(f"[Cycle {cycle}] Provider OUTAGE triggered")
                elif cycle == 4:
                    provider.set_outage(False)
                    pacing_decisions.append(f"[Cycle {cycle}] Provider RECOVERED - Outage lifted")
                elif cycle == 6:
                    # Drop agent availability
                    avail_to_pause = db.execute(
                        select(Agent)
                        .where(Agent.campaign_id == campaign.id)
                        .where(Agent.status == AgentStatus.AVAILABLE)
                        .limit(2)
                    ).scalars().all()
                    for ag in avail_to_pause:
                        ag.status = AgentStatus.PAUSED
                    db.commit()
                    pacing_decisions.append(f"[Cycle {cycle}] Agent capacity DROPPED: {len(avail_to_pause)} agent(s) paused")
                elif cycle == 8:
                    # Restore agent availability
                    paused_agents = db.execute(
                        select(Agent)
                        .where(Agent.campaign_id == campaign.id)
                        .where(Agent.status == AgentStatus.PAUSED)
                    ).scalars().all()
                    for ag in paused_agents:
                        ag.status = AgentStatus.AVAILABLE
                    db.commit()
                    pacing_decisions.append(f"[Cycle {cycle}] Agent capacity RESTORED: {len(paused_agents)} agent(s) returned")
                elif cycle == 10:
                    # Change answer rate mid-simulation
                    new_rate = 0.15
                    provider.configure(answer_rate=new_rate)
                    campaign.seed_answer_rate = new_rate
                    db.commit()
                    pacing_decisions.append(f"[Cycle {cycle}] Answer rate CHANGED from {config.answer_rate:.2f} to {new_rate:.2f}")

            # Check if all borrowers handled
            pending_count = db.execute(
                select(func.count(Borrower.id))
                .where(Borrower.campaign_id == campaign.id)
                .where(Borrower.status == BorrowerStatus.PENDING)
            ).scalar_one()

            if pending_count == 0 and calls_initiated_total > 0:
                break

            # Dial
            if config.mode == "predictive":
                dial_result = await manager.predictive_dial(campaign.id)
            else:
                dial_result = await manager.progressive_dial(campaign.id)

            calls_initiated_total += dial_result.calls_initiated

            if dial_result.pacing_decision:
                pacing_decisions.append(
                    f"[Cycle {cycle}] Mode={dial_result.pacing_decision.mode} "
                    f"Req={dial_result.pacing_decision.requested_calls} "
                    f"EstAnsRate={dial_result.pacing_decision.estimated_answer_rate:.2f} "
                    f"DesOut={dial_result.pacing_decision.desired_outstanding}"
                )

            if dial_result.safety_decision:
                safety_decisions.append(
                    f"[Cycle {cycle}] Apprv={dial_result.safety_decision.approved_count} "
                    f"Reason={dial_result.safety_decision.reason}"
                )

            # Let async provider events execute
            await asyncio.sleep(0.08)

        # Allow final provider events to finish
        await asyncio.sleep(1.0)

        # Gather final metrics
        def count_calls(status: CallStatus) -> int:
            return db.execute(
                select(func.count(Call.id))
                .where(Call.campaign_id == campaign.id)
                .where(Call.status == status)
            ).scalar_one()

        total_calls = db.execute(
            select(func.count(Call.id)).where(Call.campaign_id == campaign.id)
        ).scalar_one()

        answered_count = db.execute(
            select(func.count(Call.id))
            .where(Call.campaign_id == campaign.id)
            .where(Call.answered_at.is_not(None))
        ).scalar_one()

        connected_count = count_calls(CallStatus.CONNECTED) + count_calls(CallStatus.COMPLETED)
        completed_count = count_calls(CallStatus.COMPLETED)
        failed_count = count_calls(CallStatus.FAILED)

        available_agents = db.execute(
            select(func.count(Agent.id))
            .where(Agent.campaign_id == campaign.id)
            .where(Agent.status == AgentStatus.AVAILABLE)
        ).scalar_one()

        # Fix 5: Continuous time-averaged duty-cycle utilization
        utilization = tracker.compute_duty_cycle_utilization()

        return {
            "scenario": config.name,
            "mode": config.mode,
            "total_agents": config.num_agents,
            "available_agents": available_agents,
            "calls_initiated": total_calls,
            "calls_answered": answered_count,
            "calls_connected": connected_count,
            "calls_completed": completed_count,
            "calls_failed": failed_count,
            "agent_utilization_pct": round(utilization, 1),
            "pacing_decisions": pacing_decisions,
            "safety_decisions": safety_decisions,
        }

    finally:
        clear_event_handlers()
        app.providers._PROVIDERS = original_providers
        db.close()
        engine.dispose()


def print_results(results: dict):
    print("=" * 70)
    print(f" SIMULATION RESULTS: {results['scenario']}")
    print("=" * 70)
    print(f"  Mode:                  {results['mode']}")
    print(f"  Total Agents:          {results['total_agents']}")
    print(f"  Available Agents:      {results['available_agents']}")
    print(f"  Calls Initiated:       {results['calls_initiated']}")
    print(f"  Calls Answered:        {results['calls_answered']}")
    print(f"  Calls Connected:       {results['calls_connected']}")
    print(f"  Calls Completed:       {results['calls_completed']}")
    print(f"  Calls Failed:          {results['calls_failed']}")
    print(f"  Agent Utilization:     {results['agent_utilization_pct']}%")
    # Print decisions (up to 15 to capture full event timelines like Scenario D)
    sample_limit = 15 if "Scenario D" in results['scenario'] else 5
    print("\n  Pacing Decisions (sample):")
    for d in results['pacing_decisions'][:sample_limit]:
        print(f"    * {d}")
    print("\n  Safety Controller Decisions (sample):")
    for s in results['safety_decisions'][:sample_limit]:
        print(f"    * {s}")
    print("=" * 70 + "\n")


async def main():
    scenarios = [
        ScenarioConfig(
            name="Scenario A (Answer rate 20%, Talk time 120s)",
            answer_rate=0.20,
            avg_talk_seconds=120,
            num_agents=5,
            num_borrowers=20,
            provider_type="provider_a",
            mode="progressive",
            seed=42,
        ),
        ScenarioConfig(
            name="Scenario B (Answer rate 50%, Talk time 90s)",
            answer_rate=0.50,
            avg_talk_seconds=90,
            num_agents=5,
            num_borrowers=20,
            provider_type="provider_a",
            mode="predictive",
            seed=42,
        ),
        ScenarioConfig(
            name="Scenario C (Answer rate 70%, Talk time 180s)",
            answer_rate=0.70,
            avg_talk_seconds=180,
            num_agents=5,
            num_borrowers=20,
            provider_type="provider_a",
            mode="predictive",
            seed=42,
        ),
        ScenarioConfig(
            name="Scenario D (Changing rate, Provider latency & Failures)",
            answer_rate=0.35,
            avg_talk_seconds=120,
            num_agents=5,
            num_borrowers=25,
            provider_type="provider_b",
            mode="predictive",
            simulate_outage=True,
            outage_after_calls=8,
            seed=99,
        ),
    ]

    print("\n--- Starting SmartDialer Simulation Suite ---\n")
    for sc in scenarios:
        res = await run_scenario(sc)
        print_results(res)


if __name__ == "__main__":
    asyncio.run(main())
