"""
Pydantic schemas for Question API.
"""

from typing import Optional, List, Union
from datetime import datetime
import json
from pydantic import BaseModel, Field, field_validator


class QuestionImageResponse(BaseModel):
    id: int
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    alt_text: Optional[str] = None
    bbox: Optional[List[float]] = None


class QuestionResponse(BaseModel):
    """Single question returned by API."""
    id: int
    exam_id: Optional[int] = None
    user_id: int
    question_text: str
    subject_code: Optional[str] = "toan"
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    grade: Optional[int] = None
    chapter: Optional[str] = None
    lesson_title: Optional[str] = None
    answer: Optional[str] = None
    # FIX #14: solution_steps normalized to List[str] for ALL responses.
    # DB stores it as JSON string; this validator auto-parses it.
    # Frontend always receives List[str] from both /questions and /generate.
    solution_steps: Optional[List[str]] = None
    images: List["QuestionImageResponse"] = Field(default_factory=list)
    question_order: int = 0
    is_public: bool = True
    author_email: Optional[str] = None  # populated by API join
    created_at: datetime
    # OCR layout — page_num + bbox từ block-aware pipeline (NULL cho rows cũ)
    page_num: Optional[int] = None
    bbox: Optional[List[float]] = Field(default=None, validation_alias="bbox_json")
    # Nhãn nguồn gốc nội dung — FE dùng để hiển thị badge AI (Điều 44 Luật CN CNS)
    origin: str = "HUMAN"
    ai_model: Optional[str] = None
    reviewed_by_user: bool = False

    @field_validator("solution_steps", mode="before")
    @classmethod
    def parse_solution_steps(cls, v):
        """Auto-deserialize JSON string → List[str] from DB storage."""
        if v is None:
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else [str(parsed)]
            except (json.JSONDecodeError, ValueError):
                return [v] if v.strip() else []
        return []

    @field_validator("bbox", mode="before")
    @classmethod
    def parse_bbox(cls, v):
        """Auto-deserialize bbox_json (TEXT JSON) → List[float]."""
        if v is None or v == "":
            return None
        if isinstance(v, list):
            return [float(x) for x in v]
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list) and len(parsed) == 4:
                    return [float(x) for x in parsed]
            except (json.JSONDecodeError, ValueError, TypeError):
                return None
        return None

    class Config:
        from_attributes = True
        populate_by_name = True


class QuestionUpdate(BaseModel):
    """Update fields for a question. All fields optional — only send what changed."""
    question_text: Optional[str] = None
    subject_code: Optional[str] = None
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    grade: Optional[int] = None
    chapter: Optional[str] = None
    lesson_title: Optional[str] = None
    answer: Optional[str] = None
    solution_steps: Optional[str] = None  # JSON string
    is_public: Optional[bool] = None


class QuestionBulkCreate(BaseModel):
    """Bulk create questions (e.g. save generated questions to bank)."""
    questions: List['QuestionCreateItem']
    # Optional: attach all created questions to this exam so they show up in
    # exam-scoped views (e.g. /upload/questions/{exam_id}). Validated against
    # current_user ownership in the endpoint.
    exam_id: Optional[int] = None

    # Nhãn nguồn gốc nội dung (Điều 44 Luật CN CNS + Luật AI). Endpoint này phục
    # vụ cả lưu thủ công lẫn lưu kết quả AI, nên caller phải KHAI BÁO nguồn thay
    # vì để hệ thống đoán. Mặc định HUMAN — không tự suy luận ra nhãn AI.
    origin: Optional[str] = None
    ai_model: Optional[str] = None


class QuestionCreateItem(BaseModel):
    """Single question to create."""
    question_text: str
    subject_code: Optional[str] = "toan"
    question_type: Optional[str] = "TN"
    topic: Optional[str] = ""
    difficulty: Optional[str] = "TH"
    grade: Optional[int] = None
    chapter: Optional[str] = ""
    lesson_title: Optional[str] = ""
    answer: Optional[str] = ""
    # FIX #4: Accept both List[str] (from /generate) and str (JSON) and normalize to str for DB storage
    solution_steps: Union[List[str], Optional[str]] = "[]"

    @field_validator("solution_steps", mode="before")
    @classmethod
    def normalize_solution_steps(cls, v):
        """Normalize List[str] → JSON string for DB storage."""
        if v is None:
            return "[]"
        if isinstance(v, list):
            return json.dumps(v, ensure_ascii=False)
        return v  # Already a string


class QuestionListResponse(BaseModel):
    """Paginated list of questions."""
    items: List[QuestionResponse]
    total: int
    page: int
    page_size: int


class QuestionFilters(BaseModel):
    """Available filter values for the current user's question bank."""
    subjects: List[str] = []
    types: List[str]
    topics: List[str]
    difficulties: List[str]
    grades: List[int] = []
    chapters: List[str] = []
    total_questions: int
