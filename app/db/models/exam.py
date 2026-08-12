from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index, Boolean
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
    layout_json = Column(Text, nullable=True)
    subject_code = Column(String(30), ForeignKey("subject.subject_code"), nullable=True, default="toan")
    grade = Column(Integer, nullable=True)  # K6-12; nullable for legacy exams + IELTS
    status = Column(String, default="pending")  # pending, processing, completed, failed
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    error_message = Column(Text, nullable=True)

    # Nhãn nguồn gốc nội dung (Điều 44 Luật CN CNS + Luật AI).
    # Xem app/core/content_origin.py: HUMAN | AI_GENERATED | AI_ASSISTED | OCR_IMPORT
    origin = Column(String(20), nullable=False, default="HUMAN", server_default="HUMAN")
    ai_model = Column(String(60), nullable=True)
    reviewed_by_user = Column(Boolean, default=False, nullable=False, server_default='false')
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", backref="exams")

    __table_args__ = (
        # OPT: Covering index for list_exams ORDER BY created_at DESC + user filter.
        # Without this, SQLite does a full table scan on ORDER BY exam.created_at.
        Index("ix_exam_user_created", "user_id", "created_at"),
        # Index for cache lookup: file_hash + status + created_at
        Index("ix_exam_hash_status", "file_hash", "status"),
    )
