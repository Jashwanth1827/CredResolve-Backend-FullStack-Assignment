"""
Shared test fixtures and configuration.
"""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient

from app.database import Base, init_db
from app.main import app as fastapi_app
from app.providers import clear_event_handlers
from sqlalchemy.pool import StaticPool
import app.models.agent       # noqa: F401
import app.models.campaign    # noqa: F401
import app.models.call        # noqa: F401
import app.models.borrower    # noqa: F401
import app.models.provider_event  # noqa: F401


# ---------------------------------------------------------------------------
# In-memory SQLite engine for tests
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite://"  # In-memory


def make_engine(url: str = TEST_DATABASE_URL):
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def set_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture(scope="function")
def engine():
    """Fresh in-memory DB engine per test."""
    eng = make_engine()
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture(scope="function")
def db(engine) -> Session:
    """DB session backed by the test engine."""
    Session_ = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session_()
    yield session
    session.close()


@pytest.fixture(scope="function")
def db2(engine) -> Session:
    """Second independent DB session — simulates second worker/process."""
    Session_ = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session_()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# FastAPI test client with isolated DB
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def client(engine):
    """FastAPI test client using isolated test DB."""
    from app.database import get_db

    Session_ = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = Session_()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    clear_event_handlers()

    with TestClient(fastapi_app) as c:
        yield c

    fastapi_app.dependency_overrides.clear()
    clear_event_handlers()


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def make_campaign(db: Session, **kwargs) -> "Campaign":
    from app.models.campaign import Campaign, CampaignStatus
    defaults = dict(
        id=str(uuid.uuid4()),
        name="Test Campaign",
        status=CampaignStatus.ACTIVE,
        provider_name="provider_a",
        seed_answer_rate=0.3,
        max_concurrent_calls=10,
    )
    defaults.update(kwargs)
    c = Campaign(**defaults)
    db.add(c)
    db.commit()
    return c


def make_agent(db: Session, campaign_id: str, **kwargs) -> "Agent":
    from app.models.agent import Agent, AgentStatus
    defaults = dict(
        id=str(uuid.uuid4()),
        name="Test Agent",
        campaign_id=campaign_id,
        status=AgentStatus.AVAILABLE,
    )
    defaults.update(kwargs)
    a = Agent(**defaults)
    db.add(a)
    db.commit()
    return a


def make_borrower(db: Session, campaign_id: str, **kwargs) -> "Borrower":
    from app.models.borrower import Borrower, BorrowerStatus
    defaults = dict(
        id=str(uuid.uuid4()),
        campaign_id=campaign_id,
        name="Test Borrower",
        phone_number="+15551234567",
        status=BorrowerStatus.PENDING,
        retry_count=0,
        max_retries=3,
    )
    defaults.update(kwargs)
    b = Borrower(**defaults)
    db.add(b)
    db.commit()
    return b
