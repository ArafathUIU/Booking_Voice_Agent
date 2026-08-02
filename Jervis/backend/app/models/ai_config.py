import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy import Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class AIConfig(Base):
    __tablename__ = "ai_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False, unique=True)
    system_prompt = Column(Text, nullable=True)
    llm_provider = Column(String, nullable=True)
    llm_model = Column(String, nullable=True)
    stt_provider = Column(String, nullable=True)
    tts_provider = Column(String, nullable=True)
    temperature = Column(Numeric(3, 2), default=0.7)
    max_tokens = Column(Integer, default=150)
    provider_config = Column(JSONB, default=dict)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))

    def __repr__(self):
        return f"<AIConfig {self.id} tenant={self.tenant_id}>"
