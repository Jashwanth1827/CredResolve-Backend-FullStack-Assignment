"""
SmartDialer — Database configuration and session management.

Uses SQLite with WAL mode for better concurrency support.
check_same_thread=False is required for SQLite when sharing a connection
across threads, but we enforce proper session-per-request discipline.
"""

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from typing import Generator

DATABASE_URL = "sqlite:///./smartdialer.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": 30,  # seconds to wait on lock
    },
    # Use NullPool so each connection is fresh — important for tests
    poolclass=None,
    echo=False,
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode and foreign keys on every new connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 s busy-wait
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Called at application startup."""
    # Import all models so SQLAlchemy registers them before create_all
    import app.models.agent       # noqa: F401
    import app.models.campaign    # noqa: F401
    import app.models.call        # noqa: F401
    import app.models.borrower    # noqa: F401
    import app.models.provider_event  # noqa: F401
    Base.metadata.create_all(bind=engine)
