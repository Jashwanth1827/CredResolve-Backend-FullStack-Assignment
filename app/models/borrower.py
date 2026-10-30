"""
Borrower ORM model.

A borrower is a contact in a campaign to be dialled.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import String, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BorrowerStatus(str, Enum):
    PENDING = "PENDING"       # Not yet called
    RESERVED = "RESERVED"     # Locked for an in-flight call attempt
    IN_CALL = "IN_CALL"       # Currently connected
    COMPLETED = "COMPLETED"   # Reached successfully
    FAILED = "FAILED"         # Exhausted retries / unrecoverable
    DO_NOT_CALL = "DO_NOT_CALL"


class Borrower(Base):
    __tablename__ = "borrowers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[BorrowerStatus] = mapped_column(
        SAEnum(BorrowerStatus, name="borrower_status"),
        default=BorrowerStatus.PENDING,
        nullable=False,
    )
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(default=3, nullable=False)
    last_attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
