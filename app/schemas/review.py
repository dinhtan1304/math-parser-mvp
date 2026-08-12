from typing import Any, Optional

from pydantic import BaseModel, Field


class AssetResponse(BaseModel):
    id: int
    kind: str
    page_num: Optional[int] = None
    bbox: Optional[list[float]] = None
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class DraftQuestionResponse(BaseModel):
    id: int
    cau_num: Optional[int] = None
    question_order: int
    page_num: Optional[int] = None
    question_text: str
    subject_code: Optional[str] = None
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    grade: Optional[int] = None
    chapter: Optional[str] = None
    lesson_title: Optional[str] = None
    answer: Optional[str] = None
    answer_source: Optional[str] = None
    solution_steps: list[str] = Field(default_factory=list)
    bbox: Optional[list[float]] = None
    source_block_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    status: str = "pending"
    asset_ids: list[int] = Field(default_factory=list)


class ReviewPayload(BaseModel):
    pages: list[AssetResponse] = Field(default_factory=list)
    figures: list[AssetResponse] = Field(default_factory=list)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    drafts: list[DraftQuestionResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DraftQuestionPatch(BaseModel):
    question_text: Optional[str] = None
    subject_code: Optional[str] = None
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    grade: Optional[int] = None
    chapter: Optional[str] = None
    lesson_title: Optional[str] = None
    answer: Optional[str] = None
    solution_steps: Optional[list[str]] = None
    bbox: Optional[list[float]] = None
    asset_ids: Optional[list[int]] = None
    status: Optional[str] = None


class DraftSplitRequest(BaseModel):
    split_after_block_id: str


class DraftMergeRequest(BaseModel):
    draft_ids: list[int]


class ReviewCommitResponse(BaseModel):
    saved: int
    skipped: int
    duplicate_count: int
