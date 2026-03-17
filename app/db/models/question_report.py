from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from app.db.base_class import Base


REPORT_REASONS = [
    "wrong_answer",      # Đáp án sai
    "duplicate",         # Trùng lặp
    "inappropriate",     # Nội dung không phù hợp
    "poor_quality",      # Chất lượng kém
    "other",             # Khác
]


class QuestionReport(Base):
    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("question.id", ondelete="CASCADE"), nullable=False, index=True)
    reporter_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    reason = Column(String(50), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # One report per user per question
    __table_args__ = (
        UniqueConstraint("question_id", "reporter_id", name="uq_report_question_user"),
    )
