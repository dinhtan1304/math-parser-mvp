"""
Pydantic schemas for Question API.
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel


class QuestionResponse(BaseModel):
    """Single question returned by API."""
    id: int
    exam_id: Optional[int] = None
    question_text: str
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    answer: Optional[str] = None
    solution_steps: Optional[str] = None  # JSON string
    question_order: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class QuestionUpdate(BaseModel):
    """Update fields for a question. All fields optional — only send what changed."""
    question_text: Optional[str] = None
    question_type: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
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
    answer: Optional[str] = ""
    solution_steps: Optional[str] = "[]"


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
    total_questions: int