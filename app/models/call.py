"""
Call state machine and ORM model.

State transitions:
  QUEUED     → RESERVED | CANCELLED
  RESERVED   → INITIATED | CANCELLED | FAILED
  INITIATED  → RINGING   | FAILED
  RINGING    → ANSWERED  | FAILED | CANCELLED
  ANSWERED   → CONNECTED | FAILED
  CONNECTED  → COMPLETED | FAILED
  COMPLETED  → (terminal)
  FAILED     → (terminal)
  CANCELLED  → (terminal)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import String, DateTime, Integer, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CallStatus(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_CALL_STATES = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.CANCELLED}

CALL_VALID_TRANSITIONS: dict[CallStatus, set[CallStatus]] = {
    CallStatus.QUEUED:     {CallStatus.RESERVED, CallStatus.CANCELLED},
    CallStatus.RESERVED:   {CallStatus.INITIATED, CallStatus.CANCELLED, CallStatus.FAILED},
    CallStatus.INITIATED:  {CallStatus.RINGING, CallStatus.FAILED},
    CallStatus.RINGING:    {CallStatus.ANSWERED, CallStatus.CONNECTED, CallStatus.FAILED, CallStatus.CANCELLED},
    CallStatus.ANSWERED:   {CallStatus.CONNECTED, CallStatus.COMPLETED, CallStatus.FAILED},
    CallStatus.CONNECTED:  {CallStatus.COMPLETED, CallStatus.FAILED},
    CallStatus.COMPLETED:  set(),
    CallStatus.FAILED:     set(),
    CallStatus.CANCELLED:  set(),
}


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False)
    borrower_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    status: Mapped[CallStatus] = mapped_column(
        SAEnum(CallStatus, name="call_status"),
        default=CallStatus.QUEUED,
        nullable=False,
    )

    # Provider tracking
    provider_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    provider_call_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Timing
    initiated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Lease: if the call stays in INITIATED/RINGING past this, recovery can reclaim
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Duration in seconds (filled on COMPLETED)
    talk_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    failure_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
