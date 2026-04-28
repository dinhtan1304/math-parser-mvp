"""
Quiz models for the Edu Smart App quiz system.
Covers: Quiz, QuizTheory, QuizTheorySection, QuizQuestion.

Supports 3 creation modes:
  - Manual: teacher creates questions directly in quiz editor
  - File import: upload → parse → bank → import to quiz
  - Bank import: select existing bank questions → import to quiz
"""

import random
import string
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Index, Integer, Numeric, String, Text, JSON,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_quiz_code(length: int = 8) -> str:
    """Generate a quiz code like 'QUIZ-A1B2'."""
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(chars, k=length))
    return f"QUIZ-{suffix}"


# ─────────────────────────────────────────────────────────────
# Quiz  (top-level quiz container)
# ─────────────────────────────────────────────────────────────

class Quiz(Base):
    id              = Column(Integer, primary_key=True, index=True)
    code            = Column(String(20), unique=True, nullable=False, default=_generate_quiz_code)
    name            = Column(String(300), nullable=False)
    description     = Column(Text, nullable=True)
    cover_image_url = Column(String(500), nullable=True)

    created_by_id   = Column(Integer, ForeignKey("user.id"), nullable=False)
    subject_code    = Column(String(30), ForeignKey("subject.subject_code"), nullable=True)
    grade           = Column(Integer, nullable=True)

    mode            = Column(String(20), nullable=False, default="quiz")        # quiz | survey | practice
    language        = Column(String(5), default="vi")
    visibility      = Column(String(20), nullable=False, default="private")     # private | public | unlisted | class_only
    status          = Column(String(20), nullable=False, default="draft")       # draft | published | archived

    tags            = Column(JSON, default=list)                                # ["demo", "toan-12"]
    version         = Column(Integer, nullable=False, default=1)
    settings        = Column(JSON, default=dict)                                # full settings blob

    # Denormalized counts
    question_count  = Column(Integer, nullable=False, default=0)
    total_points    = Column(Numeric(8, 2), default=0)

    created_at      = Column(DateTime(timezone=True), default=_now)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    published_at    = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    creator         = relationship("User", backref="quizzes")
    theories        = relationship("QuizTheory", backref="quiz", cascade="all, delete-orphan",
                                   order_by="QuizTheory.display_order")
    questions       = relationship("QuizQuestion", backref="quiz", cascade="all, delete-orphan",
                                   order_by="QuizQuestion.order")

    __table_args__ = (
        Index("ix_quiz_created_by", "created_by_id"),
        Index("ix_quiz_status_visibility", "status", "visibility"),
        Index("ix_quiz_subject_grade", "subject_code", "grade"),
        Index("ix_quiz_created_at", "created_by_id", "created_at"),
    )


# ─────────────────────────────────────────────────────────────
# QuizTheory  (theory/lesson content attached to a quiz)
# ─────────────────────────────────────────────────────────────

class QuizTheory(Base):
    id              = Column(Integer, primary_key=True, index=True)
    quiz_id         = Column(Integer, ForeignKey("quiz.id", ondelete="CASCADE"), nullable=False)
    title           = Column(String(300), nullable=False)
    content_type    = Column(String(30), default="rich_text")                   # rich_text | video | image
    language        = Column(String(5), default="vi")
    tags            = Column(JSON, default=list)
    display_order   = Column(Integer, nullable=False, default=0)
    created_at      = Column(DateTime(timezone=True), default=_now)

    # Relationships
    sections        = relationship("QuizTheorySection", backref="theory", cascade="all, delete-orphan",
                                   order_by="QuizTheorySection.order")

    __table_args__ = (
        Index("ix_quiztheory_quiz", "quiz_id"),
    )


# ─────────────────────────────────────────────────────────────
# QuizTheorySection  (individual section within a theory)
# ─────────────────────────────────────────────────────────────

class QuizTheorySection(Base):
    id              = Column(Integer, primary_key=True, index=True)
    theory_id       = Column(Integer, ForeignKey("quiztheory.id", ondelete="CASCADE"), nullable=False)
    order           = Column(Integer, nullable=False, default=0)
    content         = Column(Text, nullable=False)
    content_format  = Column(String(20), default="markdown")                    # markdown | html | plain
    media           = Column(JSON, nullable=True)                               # {type, url, alt}

    __table_args__ = (
        Index("ix_quiztheorysection_theory_order", "theory_id", "order"),
    )


# ─────────────────────────────────────────────────────────────
# QuizQuestion  (a question within a quiz — core table)
# ─────────────────────────────────────────────────────────────

class QuizQuestion(Base):
    id                  = Column(Integer, primary_key=True, index=True)
    quiz_id             = Column(Integer, ForeignKey("quiz.id", ondelete="CASCADE"), nullable=False)

    # Origin tracking
    origin_question_id  = Column(Integer, ForeignKey("question.id", ondelete="SET NULL"), nullable=True)
    source_type         = Column(String(20), nullable=False, default="manual")  # manual | bank_import | file_import | ai_generated
    origin_quiz_code    = Column(String(20), nullable=True)                     # if cloned from another quiz

    # Ordering & identification
    order               = Column(Integer, nullable=False, default=0)
    code                = Column(String(30), nullable=True)                     # "Q-MC-001"

    # Question type (new system — no legacy TN/TL/DS)
    type                = Column(String(30), nullable=False)                    # multiple_choice | checkbox | fill_blank | reorder | true_false | essay

    # Content
    question_text       = Column(Text, nullable=False)
    has_correct_answer  = Column(Boolean, nullable=False, default=True)
    required            = Column(Boolean, nullable=False, default=True)

    # Points & timing
    points              = Column(Numeric(6, 2), nullable=False, default=1.0)
    time_limit_seconds  = Column(Integer, nullable=True)

    # Classification (snapshot — may diverge from bank original)
    difficulty          = Column(String(20), nullable=True)                     # easy | medium | hard | expert
    subject_code        = Column(String(30), ForeignKey("subject.subject_code"), nullable=True)
    tags                = Column(JSON, default=list)

    # Media
    media               = Column(JSON, nullable=True)                           # {type, url, alt}

    # Answer — polymorphic JSONB per type:
    #   multiple_choice: "B"
    #   checkbox:        ["B", "C", "D"]
    #   fill_blank:      {"B1": {"accept": ["π","pi"], "match_mode": "exact_list"}, "B2": "2"}
    #   reorder:         ["I3", "I1", "I4", "I2"]
    #   true_false:      true/false
    #   essay:           null
    answer              = Column(JSON, nullable=True)

    # Choices — for multiple_choice, checkbox
    #   [{"key": "A", "text": "...", "is_correct": false, "media": null}]
    choices             = Column(JSON, nullable=True)

    # Items — for reorder
    #   [{"id": "I1", "text": "..."}]
    items               = Column(JSON, nullable=True)

    # Scoring rules
    #   {"mode": "all_or_nothing", "partial_credit": false}
    #   {"mode": "per_blank", "points_per_blank": 0.5}
    scoring             = Column(JSON, nullable=True)

    # Solution
    #   {"steps": ["..."], "explanation": "..."}
    solution            = Column(JSON, nullable=True)

    # Hint — FK to theory section (real referential integrity)
    hint_section_id     = Column(Integer, ForeignKey("quiztheorysection.id", ondelete="SET NULL"), nullable=True)
    hint_auto_linked    = Column(Boolean, default=False)

    # Extra metadata
    #   {"bloom_level": "remember", "source_page": null}
    extra_metadata      = Column("metadata", JSON, nullable=True)

    created_at          = Column(DateTime(timezone=True), default=_now)
    updated_at          = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    origin_question     = relationship("Question", backref="quiz_usages")
    hint_section        = relationship("QuizTheorySection", backref="hinted_questions")

    __table_args__ = (
        Index("ix_quizquestion_quiz_order", "quiz_id", "order"),
        Index("ix_quizquestion_origin", "origin_question_id"),
        Index("ix_quizquestion_quiz_type", "quiz_id", "type"),
    )
