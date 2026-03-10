from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, Index
from datetime import datetime, timezone
from app.db.base_class import Base


def _now():
    return datetime.now(timezone.utc)


class LiveSession(Base):
    """A live battle room created by a teacher."""
    id = Column(Integer, primary_key=True, index=True)
    room_code = Column(String(6), unique=True, nullable=False, index=True)
    assignment_id = Column(Integer, ForeignKey("assignment.id", ondelete="CASCADE"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    status = Column(String(20), default="waiting", nullable=False)  # waiting | active | ended
    current_question_idx = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_livesession_teacher", "teacher_id"),
    )


class LiveParticipant(Base):
    """A student who joined a live session."""
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("livesession.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    score = Column(Integer, default=0)
    joined_at = Column(DateTime(timezone=True), default=_now)
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        Index("ix_liveparticipant_session", "session_id"),
    )


class LiveAnswer(Base):
    """Answer submitted by a student during a live session."""
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("livesession.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    question_idx = Column(Integer, nullable=False)
    answer = Column(String(500), nullable=True)
    is_correct = Column(Boolean, default=False)
    response_time_ms = Column(Integer, default=0)
    answered_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_liveanswer_session_student", "session_id", "student_id"),
    )
