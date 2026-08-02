import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), nullable=False)
    customer_name = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    slot = Column(String, nullable=True)
    booking_date = Column(DateTime(timezone=True), nullable=True)
    booking_confirmed = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
    )

    def __repr__(self):
        return (
            f"<Booking {self.id} session={self.session_id} "
            f"name={self.customer_name!r} confirmed={self.booking_confirmed}>"
        )
