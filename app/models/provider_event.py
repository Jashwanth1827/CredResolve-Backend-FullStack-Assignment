"""
Provider event idempotency log.

Each incoming provider event is stored here. Before processing, the system
checks whether the event_id already exists. If it does, the event is ignored.
This prevents duplicate transitions from duplicate/replayed provider events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProviderEvent(Base):
    __tablename__ = "provider_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_payload: Mapped[str] = mapped_column(String(2000), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("event_id", "provider_name", name="uq_provider_event"),
    )
