from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.db.base_class import Base


class Exam(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    filename = Column(String, nullable=False)
    file_path = Column(String, nullable=True)
    file_hash = Column(String(32), nullable=True, index=True)  # MD5 hash for cache (Task 19)
    result_json = Column(Text, nullable=True)
    status = Column(String, default="pending")  # pending, processing, completed, failed
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    error_message = Column(Text, nullable=True)

    user = relationship("User", backref="exams")

    __table_args__ = (
        # OPT: Covering index for list_exams ORDER BY created_at DESC + user filter.
        # Without this, SQLite does a full table scan on ORDER BY exam.created_at.
        Index("ix_exam_user_created", "user_id", "created_at"),
        # Index for cache lookup: file_hash + status + created_at
        Index("ix_exam_hash_status", "file_hash", "status"),
    )