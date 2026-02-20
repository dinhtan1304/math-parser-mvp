from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from app.db.base_class import Base


class Question(Base):
    id = Column(Integer, primary_key=True, index=True)

    # Link back to source exam
    exam_id = Column(Integer, ForeignKey("exam.id", ondelete="CASCADE"), nullable=False)

    # Link to user who uploaded the exam
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    # Question content (LaTeX)
    question_text = Column(Text, nullable=False)

    # Classification
    question_type = Column(String(50), nullable=True)
    topic = Column(String(100), nullable=True)
    difficulty = Column(String(20), nullable=True)

    # Answer and solution
    answer = Column(Text, nullable=True)
    solution_steps = Column(Text, nullable=True)

    # Position in original exam
    question_order = Column(Integer, default=0)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

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
    )