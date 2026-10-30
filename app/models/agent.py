"""
Agent state machine and ORM model.

Valid transitions:
  OFFLINE      → AVAILABLE
  AVAILABLE    → RESERVED | PAUSED | OFFLINE
  RESERVED     → DIALING  | AVAILABLE  (reservation released on failure)
  DIALING      → CONNECTED | AVAILABLE (call answered / failed before connect)
  CONNECTED    → WRAP_UP
  WRAP_UP      → AVAILABLE | OFFLINE
  PAUSED       → AVAILABLE | OFFLINE
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import String, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentStatus(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


# Allowed transitions: current_status → {set of valid next statuses}
AGENT_VALID_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.OFFLINE:    {AgentStatus.AVAILABLE},
    AgentStatus.AVAILABLE:  {AgentStatus.RESERVED, AgentStatus.PAUSED, AgentStatus.OFFLINE},
    AgentStatus.RESERVED:   {AgentStatus.DIALING, AgentStatus.CONNECTED, AgentStatus.AVAILABLE},
    AgentStatus.DIALING:    {AgentStatus.CONNECTED, AgentStatus.AVAILABLE},
    AgentStatus.CONNECTED:  {AgentStatus.WRAP_UP},
    AgentStatus.WRAP_UP:    {AgentStatus.AVAILABLE, AgentStatus.OFFLINE},
    AgentStatus.PAUSED:     {AgentStatus.AVAILABLE, AgentStatus.OFFLINE},
}


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        SAEnum(AgentStatus, name="agent_status"),
        default=AgentStatus.OFFLINE,
        nullable=False,
    )
    campaign_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    current_call_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # Lease expiry: if an agent stays RESERVED/DIALING past this time, recovery
    # can reclaim it.
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
