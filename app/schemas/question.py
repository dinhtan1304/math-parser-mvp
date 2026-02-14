"""
Pydantic schemas for Question API.
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel


class QuestionResponse(BaseModel):
    """Single question returned by API."""
    id: int
    exam_id: int
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