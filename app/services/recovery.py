"""
Recovery service — reclaims stale leases after worker crashes.

When a worker crashes mid-flight:
  - A RESERVED or INITIATED call will have a lease_expires_at timestamp.
  - If that timestamp is in the past, recovery can safely reclaim the call
    and release the agent.

This is analogous to a distributed lock with TTL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.agent import AgentStatus
from app.models.borrower import BorrowerStatus
from app.models.call import Call, CallStatus

logger = logging.getLogger(__name__)


class RecoveryService:
    """Reclaims expired reservations and stale in-flight calls."""

    def __init__(self, db: Session):
        self._db = db

    def recover_stale_calls(self) -> int:
        """
        Find calls with expired leases and release their resources.
        Returns the number of calls recovered.
        """
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()

        stale_calls = self._db.execute(
            select(Call)
            .where(Call.status.in_([CallStatus.RESERVED, CallStatus.INITIATED]))
            .where(Call.lease_expires_at.is_not(None))
            .where(Call.lease_expires_at < now)
        ).scalars().all()

        recovered = 0
        for call in stale_calls:
            try:
                logger.warning(
                    "Recovering stale call %s (status=%s, expired=%s)",
                    call.id, call.status, call.lease_expires_at,
                )

                # Fail the call
                self._db.execute(
                    text(
                        "UPDATE calls SET status = 'FAILED', "
                        "failure_reason = 'Recovered: stale lease', "
                        "updated_at = :now WHERE id = :id"
                    ),
                    {"now": now_str, "id": call.id},
                )

                # Release the agent
                if call.agent_id:
                    self._db.execute(
                        text(
                            "UPDATE agents SET status = 'AVAILABLE', "
                            "current_call_id = NULL, lease_expires_at = NULL, "
                            "updated_at = :now "
                            "WHERE id = :id AND status IN ('RESERVED','DIALING')"
                        ),
                        {"now": now_str, "id": call.agent_id},
                    )

                # Release the borrower for retry
                self._db.execute(
                    text(
                        "UPDATE borrowers SET status = 'PENDING', "
                        "retry_count = retry_count + 1, updated_at = :now "
                        "WHERE id = :bid AND status = 'RESERVED'"
                    ),
                    {"now": now_str, "bid": call.borrower_id},
                )

                self._db.commit()
                recovered += 1

            except Exception as e:
                self._db.rollback()
                logger.exception("Failed to recover call %s: %s", call.id, e)

        return recovered
