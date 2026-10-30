"""
Tests: Atomic agent reservation.

Requirements tested:
  1. Atomic agent reservation — the UPDATE WHERE status='AVAILABLE' pattern.
  2. Two workers attempting to reserve the same agent (concurrency).
"""

from __future__ import annotations

import threading
import uuid
from typing import List

import pytest
from sqlalchemy import text, create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from app.database import Base
from app.models.agent import Agent, AgentStatus
from app.models.campaign import Campaign, CampaignStatus
from app.services.call_allocator import CallAllocator
from tests.conftest import make_campaign, make_agent, make_borrower


# ---------------------------------------------------------------------------
# Test 1: Basic atomic reservation succeeds when agent is AVAILABLE
# ---------------------------------------------------------------------------

def test_atomic_reservation_succeeds(db: Session):
    """The conditional UPDATE reserves exactly one agent when available."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id, status=AgentStatus.AVAILABLE)

    result = db.execute(
        text(
            "UPDATE agents SET status = 'RESERVED', updated_at = datetime('now') "
            "WHERE id = :id AND status = 'AVAILABLE'"
        ),
        {"id": agent.id},
    )
    db.commit()

    assert result.rowcount == 1, "Should update exactly one row"

    db.expire(agent)
    refreshed = db.get(Agent, agent.id)
    assert refreshed.status == AgentStatus.RESERVED


# ---------------------------------------------------------------------------
# Test 2: Reservation fails when agent is already reserved
# ---------------------------------------------------------------------------

def test_atomic_reservation_fails_if_already_reserved(db: Session):
    """Conditional UPDATE returns rowcount=0 when status is not AVAILABLE."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id, status=AgentStatus.RESERVED)

    result = db.execute(
        text(
            "UPDATE agents SET status = 'RESERVED', updated_at = datetime('now') "
            "WHERE id = :id AND status = 'AVAILABLE'"
        ),
        {"id": agent.id},
    )
    db.commit()

    assert result.rowcount == 0, "Should not update — agent is already RESERVED"


# ---------------------------------------------------------------------------
# Test 3: Two workers race to reserve the same agent
# ---------------------------------------------------------------------------

def test_two_workers_cannot_double_reserve_same_agent():
    """
    Two independent DB sessions race to reserve the same agent.
    Exactly one must win; the other must see rowcount=0.

    Uses a shared file-based SQLite DB (not in-memory, so two connections
    can actually contend). WAL mode and busy_timeout ensure they don't
    deadlock — one waits and then sees the already-committed status.
    """
    # Create a file-based test DB to allow real two-connection contention
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        url = f"sqlite:///{db_path}"
        engine1 = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})
        engine2 = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})

        for eng in (engine1, engine2):
            @event.listens_for(eng, "connect")
            def set_pragmas(dbapi_conn, _):
                c = dbapi_conn.cursor()
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA busy_timeout=10000")
                c.close()

        Base.metadata.create_all(bind=engine1)

        S1 = sessionmaker(bind=engine1, autocommit=False, autoflush=False)
        S2 = sessionmaker(bind=engine2, autocommit=False, autoflush=False)

        # Seed data using session 1
        with S1() as setup_db:
            campaign = Campaign(
                id=str(uuid.uuid4()),
                name="Race Test Campaign",
                status=CampaignStatus.ACTIVE,
                provider_name="provider_a",
                seed_answer_rate=0.3,
                max_concurrent_calls=1,
            )
            setup_db.add(campaign)
            agent = Agent(
                id=str(uuid.uuid4()),
                name="Contested Agent",
                campaign_id=campaign.id,
                status=AgentStatus.AVAILABLE,
            )
            setup_db.add(agent)
            setup_db.commit()
            agent_id = agent.id
            campaign_id = campaign.id

        results: List[int] = []
        errors: List[str] = []

        def worker(session_factory, worker_id: int):
            with session_factory() as s:
                try:
                    r = s.execute(
                        text(
                            "UPDATE agents SET status = 'RESERVED', "
                            "updated_at = datetime('now') "
                            "WHERE id = :id AND status = 'AVAILABLE'"
                        ),
                        {"id": agent_id},
                    )
                    s.commit()
                    results.append(r.rowcount)
                except Exception as e:
                    errors.append(str(e))
                    results.append(0)

        t1 = threading.Thread(target=worker, args=(S1, 1))
        t2 = threading.Thread(target=worker, args=(S2, 2))

        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 2

        total_reserved = sum(results)
        assert total_reserved == 1, (
            f"Exactly one worker should reserve the agent. "
            f"rowcounts={results}, total={total_reserved}"
        )

        # Verify DB state
        with S1() as verify_db:
            final_agent = verify_db.get(Agent, agent_id)
            assert final_agent.status == AgentStatus.RESERVED

    finally:
        engine1.dispose()
        engine2.dispose()
        try:
            if os.path.exists(db_path):
                os.unlink(db_path)
            for extra in [db_path + "-wal", db_path + "-shm"]:
                if os.path.exists(extra):
                    os.unlink(extra)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test 4: CallAllocator.allocate uses atomic reservation
# ---------------------------------------------------------------------------

def test_allocator_atomic_reservation(db: Session):
    """CallAllocator.allocate() atomically reserves agent and borrower."""
    campaign = make_campaign(db)
    agent = make_agent(db, campaign.id)
    borrower = make_borrower(db, campaign.id)

    allocator = CallAllocator(db)
    result = allocator.allocate(campaign.id, borrower)

    assert result.success
    assert result.agent_id == agent.id
    assert result.borrower_id == borrower.id
    assert result.call_id is not None

    # Verify DB state
    db.expire_all()
    agent_db = db.get(Agent, agent.id)
    assert agent_db.status == AgentStatus.RESERVED

    from app.models.borrower import Borrower as B, BorrowerStatus
    borrower_db = db.get(B, borrower.id)
    assert borrower_db.status == BorrowerStatus.RESERVED

    from app.models.call import Call, CallStatus
    call_db = db.get(Call, result.call_id)
    assert call_db.status == CallStatus.RESERVED
    assert call_db.agent_id == agent.id


# ---------------------------------------------------------------------------
# Test 5: Allocator fails gracefully when no agents available
# ---------------------------------------------------------------------------

def test_allocator_no_agents(db: Session):
    """Allocation fails cleanly when all agents are busy."""
    campaign = make_campaign(db)
    make_agent(db, campaign.id, status=AgentStatus.CONNECTED)  # not AVAILABLE
    borrower = make_borrower(db, campaign.id)

    allocator = CallAllocator(db)
    result = allocator.allocate(campaign.id, borrower)

    assert not result.success
    assert "No available agents" in result.error

    # Borrower must remain PENDING (not silently lost)
    from app.models.borrower import BorrowerStatus
    db.expire_all()
    b = db.get(type(borrower), borrower.id)
    assert b.status == BorrowerStatus.PENDING
