from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class DocumentAsset(Base):
    __tablename__ = "documentasset"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exam.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    kind = Column(String(30), nullable=False)  # page_render | figure_crop
    storage_key = Column(String(500), nullable=False, unique=True)
    content_hash = Column(String(64), nullable=False, index=True)
    mime_type = Column(String(100), nullable=False, default="image/png")
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    page_num = Column(Integer, nullable=True)
    bbox_json = Column(Text, nullable=True)
    provenance_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    exam = relationship("Exam", backref="document_assets")
    draft_links = relationship("QuestionDraftAsset", back_populates="asset", cascade="all, delete-orphan")
    question_links = relationship("QuestionAsset", back_populates="asset", cascade="all, delete-orphan")


class QuestionDraft(Base):
    __tablename__ = "questiondraft"

    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exam.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    cau_num = Column(Integer, nullable=True)
    question_order = Column(Integer, nullable=False, default=0)
    page_num = Column(Integer, nullable=True)
    question_text = Column(Text, nullable=False)
    subject_code = Column(String(30), nullable=True, default="toan")
    question_type = Column(String(50), nullable=True)
    topic = Column(String(100), nullable=True)
    difficulty = Column(String(20), nullable=True)
    grade = Column(Integer, nullable=True)
    chapter = Column(String(200), nullable=True)
    lesson_title = Column(String(200), nullable=True)
    answer = Column(Text, nullable=True)
    answer_source = Column(String(20), nullable=True)
    solution_steps = Column(Text, nullable=True)
    bbox_json = Column(Text, nullable=True)
    source_block_ids_json = Column(Text, nullable=True)
    confidence = Column(Float, nullable=False, default=0.0)
    status = Column(String(20), nullable=False, default="pending")  # pending | accepted | rejected
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    exam = relationship("Exam", backref="question_drafts")
    asset_links = relationship("QuestionDraftAsset", back_populates="draft", cascade="all, delete-orphan")


class QuestionDraftAsset(Base):
    __tablename__ = "questiondraftasset"

    id = Column(Integer, primary_key=True, index=True)
    draft_id = Column(Integer, ForeignKey("questiondraft.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_id = Column(Integer, ForeignKey("documentasset.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type = Column(String(30), nullable=False, default="figure")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    draft = relationship("QuestionDraft", back_populates="asset_links")
    asset = relationship("DocumentAsset", back_populates="draft_links")

    __table_args__ = (
        UniqueConstraint("draft_id", "asset_id", name="uq_questiondraftasset_draft_asset"),
    )


class QuestionAsset(Base):
    __tablename__ = "questionasset"

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("question.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_id = Column(Integer, ForeignKey("documentasset.id", ondelete="CASCADE"), nullable=False, index=True)
    bbox_json = Column(Text, nullable=True)
    alt_text = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    question = relationship("Question", back_populates="asset_links")
    asset = relationship("DocumentAsset", back_populates="question_links")

    __table_args__ = (
        UniqueConstraint("question_id", "asset_id", name="uq_questionasset_question_asset"),
    )
