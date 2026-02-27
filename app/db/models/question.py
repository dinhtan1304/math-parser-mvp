from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import hashlib

from app.db.base_class import Base


def _question_hash(text: str) -> str:
    """Compute content hash for duplicate detection (Sprint 3, Task 22).

    Normalizes whitespace before hashing to catch near-exact duplicates.
    """
    normalized = " ".join(text.strip().split()).lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


class Question(Base):
    id = Column(Integer, primary_key=True, index=True)

    # Link back to source exam (NULL for AI-generated questions)
    exam_id = Column(Integer, ForeignKey("exam.id", ondelete="CASCADE"), nullable=True)

    # Link to user who uploaded the exam
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    # Question content (LaTeX)
    question_text = Column(Text, nullable=False)

    # Content hash for duplicate detection (Sprint 3, Task 22)
    content_hash = Column(String(32), nullable=True, index=True)

    # Classification
    question_type = Column(String(50), nullable=True)
    topic = Column(String(100), nullable=True)
    difficulty = Column(String(20), nullable=True)

    # Curriculum classification (GDPT 2018)
    grade = Column(Integer, nullable=True)            # 6-12
    chapter = Column(String(200), nullable=True)       # e.g. "Chương I. Ứng dụng đạo hàm..."
    lesson_title = Column(String(200), nullable=True)  # e.g. "Tính đơn điệu và cực trị..."

    # Answer and solution
    answer = Column(Text, nullable=True)
    solution_steps = Column(Text, nullable=True)

    # Position in original exam
    question_order = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    exam = relationship("Exam", backref="questions")
    user = relationship("User", backref="questions")

    __table_args__ = (
        Index("ix_question_user_topic", "user_id", "topic"),
        Index("ix_question_user_type", "user_id", "question_type"),
        Index("ix_question_user_difficulty", "user_id", "difficulty"),
        # Composite index for generator sample queries
        Index("ix_question_user_topic_diff", "user_id", "topic", "difficulty"),
        Index("ix_question_user_type_topic_diff", "user_id", "question_type", "topic", "difficulty"),
        # Content hash index for duplicate detection
        Index("ix_question_user_hash", "user_id", "content_hash"),
        # Curriculum indexes
        Index("ix_question_user_grade", "user_id", "grade"),
        Index("ix_question_user_grade_chapter", "user_id", "grade", "chapter"),
    )