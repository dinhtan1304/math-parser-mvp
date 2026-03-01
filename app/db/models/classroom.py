"""
Classroom models for the MathPlay teacher/student system.
Covers: Class, ClassMember, Assignment, Submission, AnswerDetail, StudentXP, Badge.
"""

import random
import string
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Index, Integer, String, Text,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


def _generate_class_code(length: int = 6) -> str:
    """Generate a random uppercase alphanumeric class code, e.g. 'MAT9A2'."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────
# Class  (a teacher's classroom)
# ─────────────────────────────────────────────────────────────

class Class(Base):
    id          = Column(Integer, primary_key=True, index=True)
    teacher_id  = Column(Integer, ForeignKey("user.id"), nullable=False)

    name        = Column(String(200), nullable=False)
    subject     = Column(String(100), nullable=True)   # e.g. "Toán", "Vật lý"
    grade       = Column(Integer, nullable=True)        # 6-12
    description = Column(Text, nullable=True)
    code        = Column(String(10), unique=True, index=True, nullable=False)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), default=_now)

    # Relationships
    teacher     = relationship("User", backref="classes_taught")
    members     = relationship("ClassMember", backref="classroom", cascade="all, delete-orphan")
    assignments = relationship("Assignment", backref="classroom", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_class_teacher_active", "teacher_id", "is_active"),
    )


# ─────────────────────────────────────────────────────────────
# ClassMember  (student ↔ class enrollment)
# ─────────────────────────────────────────────────────────────

class ClassMember(Base):
    id         = Column(Integer, primary_key=True, index=True)
    class_id   = Column(Integer, ForeignKey("class.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    joined_at  = Column(DateTime(timezone=True), default=_now)
    is_active  = Column(Boolean, default=True)  # False = kicked / left

    # Relationships
    student    = relationship("User", backref="class_memberships")

    __table_args__ = (
        Index("ix_classmember_class_student", "class_id", "student_id", unique=True),
        Index("ix_classmember_student", "student_id"),
    )


# ─────────────────────────────────────────────────────────────
# Assignment  (a batch of questions sent to a class)
# ─────────────────────────────────────────────────────────────

class Assignment(Base):
    id           = Column(Integer, primary_key=True, index=True)
    class_id     = Column(Integer, ForeignKey("class.id", ondelete="CASCADE"), nullable=False)
    exam_id      = Column(Integer, ForeignKey("exam.id", ondelete="SET NULL"), nullable=True)
    created_by   = Column(Integer, ForeignKey("user.id"), nullable=False)

    title        = Column(String(300), nullable=False)
    description  = Column(Text, nullable=True)
    deadline     = Column(DateTime(timezone=True), nullable=True)
    max_attempts = Column(Integer, default=3)
    show_answer  = Column(Boolean, default=True)   # show solution after submit
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime(timezone=True), default=_now)

    # Relationships
    submissions  = relationship("Submission", backref="assignment", cascade="all, delete-orphan")
    teacher      = relationship("User", backref="assignments_created")

    __table_args__ = (
        Index("ix_assignment_class_active", "class_id", "is_active"),
        Index("ix_assignment_class_deadline", "class_id", "deadline"),
    )


# ─────────────────────────────────────────────────────────────
# Submission  (one student's attempt at an assignment)
# ─────────────────────────────────────────────────────────────

class Submission(Base):
    id            = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignment.id", ondelete="CASCADE"), nullable=False)
    student_id    = Column(Integer, ForeignKey("user.id"), nullable=False)

    score         = Column(Integer, nullable=True)     # 0-100
    total_q       = Column(Integer, default=0)         # total questions
    correct_q     = Column(Integer, default=0)         # correct answers
    time_spent_s  = Column(Integer, nullable=True)     # seconds spent
    attempt_no    = Column(Integer, default=1)
    game_mode     = Column(String(50), nullable=True)  # "quiz","drag","fill","find_error"
    status        = Column(String(20), default="in_progress")  # in_progress, completed
    xp_earned     = Column(Integer, default=0)
    submitted_at  = Column(DateTime(timezone=True), nullable=True)
    created_at    = Column(DateTime(timezone=True), default=_now)

    # Relationships
    student       = relationship("User", backref="submissions")
    answers       = relationship("AnswerDetail", backref="submission", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_submission_assignment_student", "assignment_id", "student_id"),
        Index("ix_submission_student", "student_id"),
    )


# ─────────────────────────────────────────────────────────────
# AnswerDetail  (per-question result within a submission)
# ─────────────────────────────────────────────────────────────

class AnswerDetail(Base):
    id            = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submission.id", ondelete="CASCADE"), nullable=False)
    question_id   = Column(Integer, ForeignKey("question.id", ondelete="SET NULL"), nullable=True)

    given_answer  = Column(Text, nullable=True)
    is_correct    = Column(Boolean, nullable=True)
    time_ms       = Column(Integer, nullable=True)   # time to answer in ms

    __table_args__ = (
        Index("ix_answerdetail_submission", "submission_id"),
    )


# ─────────────────────────────────────────────────────────────
# StudentXP  (gamification — one row per student)
# ─────────────────────────────────────────────────────────────

class StudentXP(Base):
    id           = Column(Integer, primary_key=True, index=True)
    student_id   = Column(Integer, ForeignKey("user.id"), unique=True, nullable=False)

    total_xp     = Column(Integer, default=0)
    level        = Column(Integer, default=1)
    streak_days  = Column(Integer, default=0)
    last_active  = Column(DateTime(timezone=True), nullable=True)
    updated_at   = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    student      = relationship("User", backref="xp_record")


# ─────────────────────────────────────────────────────────────
# Badge  (achievements earned by students)
# ─────────────────────────────────────────────────────────────

class Badge(Base):
    id         = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    badge_type = Column(String(100), nullable=False)  # e.g. "streak_7", "combo_10", "first_submit"
    label      = Column(String(200), nullable=True)
    earned_at  = Column(DateTime(timezone=True), default=_now)

    student    = relationship("User", backref="badges")

    __table_args__ = (
        Index("ix_badge_student", "student_id"),
    )