"""
Call Allocator — atomic agent reservation and call creation.

This is the layer between the Safety Controller approval and the actual
telecom provider call. It:

1. Atomically reserves an available agent (UPDATE ... WHERE status='AVAILABLE').
2. Atomically reserves the borrower.
3. Creates the call record.
4. Hands off to the provider via the Safety Controller path.

The atomic reservation uses a single UPDATE statement with a WHERE clause
on the expected current status. The row count is checked — if 0 rows were
updated, the reservation failed (another worker grabbed the agent first).

This is the correct approach. A read-then-write check (SELECT + UPDATE in
separate statements) would have a TOCTOU race condition.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text, select, func
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentStatus
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallStatus
from app.models.campaign import Campaign

logger = logging.getLogger(__name__)

# How long a reservation lease lasts before recovery can reclaim it
RESERVATION_LEASE_SECONDS = 60


@dataclass
class AllocationResult:
    success: bool
    call_id: Optional[str] = None
    agent_id: Optional[str] = None
    borrower_id: Optional[str] = None
    error: Optional[str] = None


class CallAllocator:
    """
    Handles atomic agent and borrower reservation plus call record creation.

    Concurrency guarantee:
    The UPDATE ... WHERE status='AVAILABLE' statement is atomic in SQLite
    (single writer). If two workers race, exactly one will see rowcount=1;
    the other sees rowcount=0 and backs off. No separate read is needed.
    """

    def __init__(self, db: Session):
        self._db = db

    def allocate(
        self,
        campaign_id: str,
        borrower: Borrower,
    ) -> AllocationResult:
        """
        Full allocation sequence:
          1. Atomically reserve an AVAILABLE agent.
          2. Atomically reserve the borrower.
          3. Create a RESERVED call record.

        All three steps run inside a single database transaction.
        If any step fails, the transaction rolls back.
        """
        try:
            # Step 1: Atomic agent reservation
            agent_id = self._reserve_agent(campaign_id)
            if agent_id is None:
                return AllocationResult(success=False, error="No available agents")

            # Step 2: Atomic borrower reservation
            reserved = self._reserve_borrower(borrower.id)
            if not reserved:
                # Release the agent we just reserved
                self._release_agent(agent_id)
                return AllocationResult(
                    success=False,
                    error=f"Borrower {borrower.id} already reserved by another worker",
                )

            # Step 3: Create call record
            lease_expires = datetime.now(timezone.utc) + timedelta(seconds=RESERVATION_LEASE_SECONDS)
            call = Call(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                borrower_id=borrower.id,
                agent_id=agent_id,
                status=CallStatus.RESERVED,
                lease_expires_at=lease_expires,
            )
            self._db.add(call)

            # Update agent with call reference
            agent = self._db.get(Agent, agent_id)
            if agent:
                agent.current_call_id = call.id
                agent.lease_expires_at = lease_expires

            self._db.commit()

            logger.info(
                "Allocated: call=%s agent=%s borrower=%s",
                call.id, agent_id, borrower.id,
            )
            return AllocationResult(
                success=True,
                call_id=call.id,
                agent_id=agent_id,
                borrower_id=borrower.id,
            )

        except Exception as e:
            self._db.rollback()
            logger.exception("Allocation failed: %s", e)
            return AllocationResult(success=False, error=str(e))

    def _reserve_agent(self, campaign_id: str) -> Optional[str]:
        """
        Atomically reserve one AVAILABLE agent using a conditional UPDATE.

        Returns the agent_id if successful, None if no agent was available.

        The UPDATE only matches rows where status='AVAILABLE'. If the row
        was grabbed by another worker between our query and the update,
        the WHERE clause fails and rowcount is 0.
        """
        # Find a candidate agent ID
        row = self._db.execute(
            select(Agent.id)
            .where(Agent.campaign_id == campaign_id)
            .where(Agent.status == AgentStatus.AVAILABLE)
            .limit(1)
            .with_for_update(skip_locked=True)  # Skip agents locked by other txns
        ).first()

        if row is None:
            return None

        candidate_id = row[0]

        # Atomic conditional update — the WHERE on status is the safety lock.
        result = self._db.execute(
            text(
                "UPDATE agents SET status = :new_status, updated_at = :now "
                "WHERE id = :id AND status = :expected_status"
            ),
            {
                "new_status": AgentStatus.RESERVED.value,
                "now": datetime.now(timezone.utc).isoformat(),
                "id": candidate_id,
                "expected_status": AgentStatus.AVAILABLE.value,
            },
        )

        if result.rowcount != 1:
            # Another worker beat us — no agent reserved
            return None

        return candidate_id

    def _reserve_borrower(self, borrower_id: str) -> bool:
        """
        Atomically reserve a PENDING borrower.

        Returns True if exactly one row was updated.
        """
        result = self._db.execute(
            text(
                "UPDATE borrowers SET status = :new_status, updated_at = :now "
                "WHERE id = :id AND status = :expected_status"
            ),
            {
                "new_status": BorrowerStatus.RESERVED.value,
                "now": datetime.now(timezone.utc).isoformat(),
                "id": borrower_id,
                "expected_status": BorrowerStatus.PENDING.value,
            },
        )
        return result.rowcount == 1

    def _release_agent(self, agent_id: str) -> None:
        """Release a RESERVED agent back to AVAILABLE."""
        try:
            self._db.execute(
                text(
                    "UPDATE agents SET status = :status, current_call_id = NULL, "
                    "lease_expires_at = NULL, updated_at = :now "
                    "WHERE id = :id AND status = :from_status"
                ),
                {
                    "status": AgentStatus.AVAILABLE.value,
                    "now": datetime.now(timezone.utc).isoformat(),
                    "id": agent_id,
                    "from_status": AgentStatus.RESERVED.value,
                },
            )
            self._db.commit()
        except Exception as e:
            logger.error("Failed to release agent %s: %s", agent_id, e)

    def advance_call_to_initiated(
        self,
        call_id: str,
        agent_id: str,
        provider_name: str,
        provider_call_id: str,
    ) -> bool:
        """
        Move a RESERVED call to INITIATED and agent to DIALING.
        Returns True on success.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            result = self._db.execute(
                text(
                    "UPDATE calls SET status = :new_status, provider_name = :pname, "
                    "provider_call_id = :pcid, initiated_at = :now, updated_at = :now "
                    "WHERE id = :id AND status = :expected"
                ),
                {
                    "new_status": CallStatus.INITIATED.value,
                    "pname": provider_name,
                    "pcid": provider_call_id,
                    "now": now,
                    "id": call_id,
                    "expected": CallStatus.RESERVED.value,
                },
            )
            if result.rowcount != 1:
                return False

            self._db.execute(
                text(
                    "UPDATE agents SET status = :status, updated_at = :now "
                    "WHERE id = :id AND status = :from_status"
                ),
                {
                    "status": AgentStatus.DIALING.value,
                    "now": now,
                    "id": agent_id,
                    "from_status": AgentStatus.RESERVED.value,
                },
            )
            self._db.commit()
            return True
        except Exception as e:
            self._db.rollback()
            logger.exception("advance_call_to_initiated failed: %s", e)
            return False

    def fail_call_and_release(
        self,
        call_id: str,
        agent_id: Optional[str],
        borrower_id: str,
        reason: str,
    ) -> None:
        """
        Fail a call and release both the agent and borrower.
        Called when provider initiation fails or the agent disappears.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()

            # Fail the call (from any non-terminal state)
            self._db.execute(
                text(
                    "UPDATE calls SET status = :status, failure_reason = :reason, "
                    "updated_at = :now WHERE id = :id "
                    "AND status NOT IN ('COMPLETED','FAILED','CANCELLED')"
                ),
                {"status": CallStatus.FAILED.value, "reason": reason, "now": now, "id": call_id},
            )

            # Release the agent back to AVAILABLE (from RESERVED or DIALING)
            if agent_id:
                self._db.execute(
                    text(
                        "UPDATE agents SET status = :status, current_call_id = NULL, "
                        "lease_expires_at = NULL, updated_at = :now "
                        "WHERE id = :id AND status IN ('RESERVED','DIALING')"
                    ),
                    {"status": AgentStatus.AVAILABLE.value, "now": now, "id": agent_id},
                )

            # Release the borrower back to PENDING for retry
            self._db.execute(
                text(
                    "UPDATE borrowers SET status = :status, retry_count = retry_count + 1, "
                    "updated_at = :now WHERE id = :bid AND status = 'RESERVED'"
                ),
                {"status": BorrowerStatus.PENDING.value, "now": now, "bid": borrower_id},
            )

            self._db.commit()
            logger.info("Released call=%s agent=%s borrower=%s: %s", call_id, agent_id, borrower_id, reason)
        except Exception as e:
            self._db.rollback()
            logger.exception("fail_call_and_release failed: %s", e)
