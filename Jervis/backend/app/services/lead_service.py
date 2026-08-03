import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead
from app.models.lead_attempt import LeadAttempt

logger = logging.getLogger(__name__)

# Claim/alarm query guarded by status + SKIP LOCKED so two schedulers (or a
# retry racing a manual dial) can never both pick up the same lead.
_CLAIM_SQL = """
WITH picked AS (
    SELECT id
    FROM leads
    WHERE tenant_id = :tid
      AND status = 'pending'
      AND (next_retry_at IS NULL OR next_retry_at <= :now)
    ORDER BY created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE leads
SET status = 'dialing',
    attempt_count = attempt_count + 1,
    last_dialed_at = :now,
    updated_at = :now
WHERE id IN (SELECT id FROM picked)
RETURNING id, customer_name, phone, purpose, service, attempt_count
"""


class LeadService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_lead(
        self,
        tenant_id: str,
        customer_name: str,
        phone: str,
        purpose: str,
    ) -> Lead:
        from app.agents.conversation_state import extract_service

        lead = Lead(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            customer_name=customer_name,
            phone=phone,
            purpose=purpose,
            service=extract_service(purpose) if purpose else None,
            status="pending",
            attempt_count=0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(lead)
        await self.db.flush()
        return lead

    async def claim_pending(self, tenant_id: str) -> Optional[dict]:
        """Atomically claim and return the next pending+due lead (or None)."""
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            text(_CLAIM_SQL),
            {"tid": uuid.UUID(tenant_id), "now": now},
        )
        row = result.first()
        if not row:
            return None
        return {
            "id": str(row.id),
            "customer_name": row.customer_name,
            "phone": row.phone,
            "purpose": row.purpose,
            "service": row.service,
            "attempt_count": row.attempt_count,
        }

    async def find_pending(self, tenant_id: str) -> Optional[dict]:
        """Return the next due pending lead WITHOUT claiming it.

        The actual claim happens inside ``start_outbound_call``; this read-only
        scan is safe to run repeatedly while calls are in flight.
        """
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            text(
                "SELECT id, customer_name, phone, purpose, service, attempt_count "
                "FROM leads "
                "WHERE tenant_id = :tid AND status = 'pending' "
                "AND (next_retry_at IS NULL OR next_retry_at <= :now) "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"tid": uuid.UUID(tenant_id), "now": now},
        )
        row = result.first()
        if not row:
            return None
        return {
            "id": str(row.id),
            "customer_name": row.customer_name,
            "phone": row.phone,
            "purpose": row.purpose,
            "service": row.service,
            "attempt_count": row.attempt_count,
        }

    async def claim_by_id(self, lead_id: str) -> Optional[dict]:
        """Atomically claim a specific lead (pending+due -> dialing).

        Returns the lead only if the transition succeeded. Two concurrent
        callers (e.g. the manual /outbound background task racing the poller)
        both run this UPDATE; the row lock serialises them and the second one
        finds status != 'pending' and gets None, so a lead can never be dialed
        twice.
        """
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            text(
                "UPDATE leads "
                "SET status='dialing', attempt_count=attempt_count + 1, "
                "last_dialed_at=:now, updated_at=:now "
                "WHERE id=:id AND status='pending' "
                "AND (next_retry_at IS NULL OR next_retry_at <= :now) "
                "RETURNING id, customer_name, phone, purpose, service, attempt_count"
            ),
            {"id": uuid.UUID(lead_id), "now": now},
        )
        row = result.first()
        if not row:
            return None
        return {
            "id": str(row.id),
            "customer_name": row.customer_name,
            "phone": row.phone,
            "purpose": row.purpose,
            "service": row.service,
            "attempt_count": row.attempt_count,
        }

    async def record_attempt(
        self,
        lead_id: str,
        outcome: str,
        call_session_id: Optional[str] = None,
        details: Optional[str] = None,
    ):
        attempt = LeadAttempt(
            id=uuid.uuid4(),
            lead_id=uuid.UUID(lead_id),
            call_session_id=uuid.UUID(call_session_id) if call_session_id else None,
            outcome=outcome,
            details=details,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(attempt)
        await self.db.flush()

    async def set_answered(self, lead_id: str, call_session_id: str):
        await self._check_lead(lead_id, call_session_id)

    async def mark_completed(self, lead_id: str):
        await self.db.execute(
            text("UPDATE leads SET status='completed', updated_at=:now WHERE id=:id"),
            {"id": uuid.UUID(lead_id), "now": datetime.now(timezone.utc)},
        )
        await self.db.flush()

    async def rearm_retry(
        self,
        lead_id: str,
        retry_seconds: int = 600,
        max_attempts: int = 3,
    ):
        now = datetime.now(timezone.utc)
        # Only re-arm if we're still within the retry budget.
        row = await self.db.execute(
            text(
                "SELECT attempt_count FROM leads WHERE id=:id FOR UPDATE"
            ),
            {"id": uuid.UUID(lead_id)},
        )
        count = row.scalar_one_or_none()
        if count is None:
            return
        if count >= max_attempts:
            await self.db.execute(
                text("UPDATE leads SET status='failed', updated_at=:now WHERE id=:id"),
                {"id": uuid.UUID(lead_id), "now": now},
            )
            return
        from datetime import timedelta

        await self.db.execute(
            text(
                "UPDATE leads SET status='pending', next_retry_at=:retry_at, "
                "updated_at=:now WHERE id=:id"
            ),
            {
                "id": uuid.UUID(lead_id),
                "retry_at": now + timedelta(seconds=retry_seconds),
                "now": now,
            },
        )
        await self.db.flush()

    async def get_lead(self, lead_id: str) -> Optional[dict]:
        result = await self.db.execute(
            text(
                "SELECT id, customer_name, phone, purpose, service, status, "
                "attempt_count, call_session_id FROM leads WHERE id = :id"
            ),
            {"id": uuid.UUID(lead_id)},
        )
        row = result.first()
        if not row:
            return None
        return {
            "id": str(row.id),
            "customer_name": row.customer_name,
            "phone": row.phone,
            "purpose": row.purpose,
            "service": row.service,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "call_session_id": str(row.call_session_id) if row.call_session_id else None,
        }

    async def list_leads(self, tenant_id: str, limit: int = 100) -> list:
        result = await self.db.execute(
            text(
                "SELECT id, customer_name, phone, purpose, service, status, "
                "attempt_count, last_dialed_at, next_retry_at, created_at "
                "FROM leads WHERE tenant_id = :tid ORDER BY created_at DESC LIMIT :lim"
            ),
            {"tid": uuid.UUID(tenant_id), "lim": limit},
        )
        rows = result.mappings().all()
        return [dict(r) for r in rows]

    async def _check_lead(self, lead_id: str, call_session_id: str):
        """Associate a lead with its live call session."""
        await self.db.execute(
            text(
                "UPDATE leads SET status='dialing', call_session_id=:sid, "
                "updated_at=:now WHERE id=:id"
            ),
            {
                "id": uuid.UUID(lead_id),
                "sid": uuid.UUID(call_session_id),
                "now": datetime.now(timezone.utc),
            },
        )
        await self.db.flush()