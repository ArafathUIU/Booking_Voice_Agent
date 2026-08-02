import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class LeadAttempt(Base):
    """A single dial attempt for a lead.

    one `leads` row is the source of truth for a person; each time the agent
    dials them we append a row here (answered / no_answer / busy / failed).
    """

    __tablename__ = "call_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id = Column(UUID(as_uuid=True), nullable=False)
    call_session_id = Column(UUID(as_uuid=True), nullable=True)
    outcome = Column(String, nullable=False)  # answered / no_answer / busy / failed
    details = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))

    def __repr__(self):
        return f"<LeadAttempt {self.id} lead={self.lead_id} outcome={self.outcome}>"