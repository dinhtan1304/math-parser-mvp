"""
Pydantic schemas for Question API.
"""

from typing import Optional, List, Union
from datetime import datetime
import json
from pydantic import BaseModel, field_validator


class QuestionResponse(BaseModel):
    """Single question returned by API."""
    id: int
    exam_id: Optional[int] = None
    question_text: str
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
    question_order: int = 0
    created_at: datetime

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

    class Config:
        from_attributes = True


class QuestionUpdate(BaseModel):
    """Update fields for a question. All fields optional — only send what changed."""
    question_text: Optional[str] = None
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    grade: Optional[int] = None
    chapter: Optional[str] = None
    lesson_title: Optional[str] = None
    answer: Optional[str] = None
    solution_steps: Optional[str] = None  # JSON string


class QuestionBulkCreate(BaseModel):
    """Bulk create questions (e.g. save generated questions to bank)."""
    questions: List['QuestionCreateItem']


class QuestionCreateItem(BaseModel):
    """Single question to create."""
    question_text: str
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
    types: List[str]
    topics: List[str]
    difficulties: List[str]
    grades: List[int] = []
    chapters: List[str] = []
    total_questions: int