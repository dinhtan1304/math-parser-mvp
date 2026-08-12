"""
IELTS Writing AI grade — caches per-essay band scores returned by Gemini.

A row is created when an essay is submitted in a QuizAttempt, and again
whenever the cached `grade_hash` is reused (so per-attempt history is
preserved while skipping the actual Gemini call).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IeltsWritingGrade(Base):
    __tablename__ = "ieltswritinggrade"

    id              = Column(Integer, primary_key=True, index=True)
    attempt_id      = Column(Integer, ForeignKey("quizattempt.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id     = Column(Integer, ForeignKey("quizquestion.id", ondelete="CASCADE"), nullable=False)

    grade_hash      = Column(String(64), nullable=False, index=True)
    task_type       = Column(String(10), nullable=False)              # "task_1" | "task_2"

    band_ta         = Column(Numeric(3, 1), nullable=True)
    band_cc         = Column(Numeric(3, 1), nullable=True)
    band_lr         = Column(Numeric(3, 1), nullable=True)
    band_gra        = Column(Numeric(3, 1), nullable=True)
    band_overall    = Column(Numeric(3, 1), nullable=True)

    feedback_json   = Column(JSON, nullable=True)                     # {ta:"...", cc:"...", ...}
    status          = Column(String(20), nullable=False, default="pending")  # pending | graded | failed
    error_message   = Column(Text, nullable=True)
    model           = Column(String(50), nullable=True)

    created_at      = Column(DateTime(timezone=True), default=_now)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    attempt         = relationship("QuizAttempt", backref="writing_grades")
    question        = relationship("QuizQuestion")

    __table_args__ = (
        Index("ix_iwg_attempt_question", "attempt_id", "question_id"),
        Index("ix_iwg_hash_status", "grade_hash", "status"),
    )
