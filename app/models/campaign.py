"""
Campaign ORM model.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import String, DateTime, Float, Integer, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CampaignStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        SAEnum(CampaignStatus, name="campaign_status"),
        default=CampaignStatus.DRAFT,
        nullable=False,
    )

    # Pacing parameters
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(50), default="provider_a", nullable=False)

    # Rolling statistics used by predictive pacing (updated after each call)
    total_calls_initiated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_calls_answered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_talk_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Seed answer rate (overridden by rolling stats once enough data exists)
    seed_answer_rate: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def estimated_answer_rate(self) -> float:
        """Rolling answer rate, falling back to seed when data is sparse."""
        min_sample = 10
        if self.total_calls_initiated >= min_sample:
            return self.total_calls_answered / self.total_calls_initiated
        # Blend seed with available data
        weight = self.total_calls_initiated / min_sample
        rolling = (
            self.total_calls_answered / self.total_calls_initiated
            if self.total_calls_initiated > 0
            else self.seed_answer_rate
        )
        return (1 - weight) * self.seed_answer_rate + weight * rolling

    @property
    def estimated_avg_talk_seconds(self) -> float:
        """Average talk duration; default 120 s until we have data."""
        if self.total_calls_answered > 0:
            return self.total_talk_seconds / self.total_calls_answered
        return 120.0
