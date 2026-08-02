import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy import Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class CallSession(Base):
    __tablename__ = "call_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False)
    customer_id = Column(UUID(as_uuid=True), nullable=True)
    lead_id = Column(UUID(as_uuid=True), nullable=True)
    service_id = Column(UUID(as_uuid=True), nullable=True)
    session_type = Column(String, nullable=True)
    livekit_room = Column(String, unique=True, nullable=True)
    status = Column(String, default="ringing")
    ended_reason = Column(String, nullable=True)
    fsm_state = Column(String, default="idle")
    fsm_stack = Column(JSONB, default=list)
    intent_context = Column(JSONB, default=dict)
    booking_outcome = Column(String, nullable=True)
    agent_config_snapshot = Column(JSONB, default=dict)
    started_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    transcript = Column(JSONB, default=list)
    summary = Column(Text, nullable=True)
    sentiment = Column(String, nullable=True)
    detected_intent = Column(String, nullable=True)
    total_input_tokens = Column(Integer, nullable=True)
    total_output_tokens = Column(Integer, nullable=True)
    total_cost = Column(Numeric(10, 6), nullable=True)
    extra_metadata = Column("metadata", JSONB, default=dict)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))

    def __repr__(self):
        return f"<CallSession {self.id} [{self.status}] {self.livekit_room}>"
