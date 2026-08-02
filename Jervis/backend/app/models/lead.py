import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class Lead(Base):
    __tablename__ = "leads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False)
    customer_name = Column(String, nullable=True)
    phone = Column(String, nullable=False)
    purpose = Column(Text, nullable=True)
    service = Column(String, nullable=True)
    # pending -> dialing -> completed | no_answer | failed
    status = Column(String, default="pending", nullable=False)
    attempt_count = Column(Integer, default=0, nullable=False)
    last_dialed_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    call_session_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
    )

    def __repr__(self):
        return (
            f"<Lead {self.id} phone={self.phone!r} "
            f"status={self.status} attempts={self.attempt_count}>"
        )
