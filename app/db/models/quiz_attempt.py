"""
Quiz attempt models — tracks student quiz sessions and per-question answers.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Index, Integer, Numeric, String, JSON,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────
# QuizAttempt  (one student's attempt at a quiz)
# ─────────────────────────────────────────────────────────────

class QuizAttempt(Base):
    id              = Column(Integer, primary_key=True, index=True)
    quiz_id         = Column(Integer, ForeignKey("quiz.id", ondelete="CASCADE"), nullable=False)
    student_id      = Column(Integer, ForeignKey("user.id"), nullable=True)  # NULL = anonymous guest

    # Context — how the quiz was taken
    assignment_id   = Column(Integer, ForeignKey("assignment.id", ondelete="SET NULL"), nullable=True)
    live_session_id = Column(Integer, ForeignKey("livesession.id", ondelete="SET NULL"), nullable=True)

    attempt_no      = Column(Integer, nullable=False, default=1)
    status          = Column(String(20), nullable=False, default="in_progress")  # in_progress | completed | timed_out | abandoned

    # Scores
    score           = Column(Numeric(8, 2), nullable=True)
    max_score       = Column(Numeric(8, 2), nullable=True)
    percentage      = Column(Numeric(5, 2), nullable=True)      # 0-100
    passed          = Column(Boolean, nullable=True)

    total_questions = Column(Integer, nullable=False, default=0)
    correct_count   = Column(Integer, nullable=False, default=0)

    # Random selection: which question IDs were picked for this attempt (None = all)
    selected_question_ids = Column(JSON, nullable=True)

    time_spent_s    = Column(Integer, nullable=True)
    xp_earned       = Column(Integer, default=0)

    # Grading
    graded_by_id    = Column(Integer, ForeignKey("user.id"), nullable=True)  # teacher who graded (manual mode)
    graded_at       = Column(DateTime(timezone=True), nullable=True)

    started_at      = Column(DateTime(timezone=True), default=_now)
    submitted_at    = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), default=_now)

    # Relationships
    quiz            = relationship("Quiz", backref="attempts")
    student         = relationship("User", backref="quiz_attempts", foreign_keys=[student_id])
    graded_by       = relationship("User", foreign_keys=[graded_by_id])
    answers         = relationship("QuizAnswer", backref="attempt", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_quizattempt_quiz_student", "quiz_id", "student_id"),
        Index("ix_quizattempt_student", "student_id", "created_at"),
        Index("ix_quizattempt_assignment", "assignment_id"),
        Index("ix_quizattempt_status", "quiz_id", "status"),
    )


# ─────────────────────────────────────────────────────────────
# QuizAnswer  (per-question answer within a quiz attempt)
# ─────────────────────────────────────────────────────────────

class QuizAnswer(Base):
    id              = Column(Integer, primary_key=True, index=True)
    attempt_id      = Column(Integer, ForeignKey("quizattempt.id", ondelete="CASCADE"), nullable=False)
    question_id     = Column(Integer, ForeignKey("quizquestion.id", ondelete="CASCADE"), nullable=False)

    # Answer — same polymorphic JSONB format as QuizQuestion.answer
    given_answer    = Column(JSON, nullable=True)
    is_correct      = Column(Boolean, nullable=True)
    points_earned   = Column(Numeric(6, 2), default=0)
    time_ms         = Column(Integer, nullable=True)

    # Hint tracking
    hint_used       = Column(Boolean, default=False)
    hint_level      = Column(Integer, default=0)                # 0=none, 1/2/3 = hint levels

    # Manual grading
    teacher_comment = Column(String(1000), nullable=True)       # teacher feedback on this answer

    created_at      = Column(DateTime(timezone=True), default=_now)

    # Relationships
    question        = relationship("QuizQuestion", backref="received_answers")

    __table_args__ = (
        Index("ix_quizanswer_attempt", "attempt_id"),
        Index("ix_quizanswer_question", "question_id"),
        Index("ix_quizanswer_attempt_question", "attempt_id", "question_id", unique=True),
    )
