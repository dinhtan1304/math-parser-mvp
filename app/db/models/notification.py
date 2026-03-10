from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from datetime import datetime, timezone
from app.db.base_class import Base


class DeviceToken(Base):
    """Expo push token for a user device."""
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    expo_push_token = Column(String(200), nullable=False)
    platform = Column(String(10), nullable=True)  # 'ios' | 'android'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # One token per user (upsert by user_id)
        UniqueConstraint("user_id", "expo_push_token", name="uq_device_token_user_token"),
    )
