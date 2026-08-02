import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String
from sqlalchemy import Numeric
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class VoiceConfig(Base):
    __tablename__ = "voice_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False, unique=True)
    provider = Column(String, nullable=True)
    voice_id = Column(String, nullable=True)
    language = Column(String, default="en-US")
    stability = Column(Numeric(3, 2), default=0.5)
    similarity = Column(Numeric(3, 2), default=0.75)
    speaking_rate = Column(Numeric(3, 2), default=1.0)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))

    def __repr__(self):
        return f"<VoiceConfig {self.id} tenant={self.tenant_id}>"
