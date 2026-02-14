from typing import Optional, List
from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    """Request body for generating new questions."""
    question_type: str = Field(default="TN", description="TN, TL, ...")
    topic: str = Field(default="", description="Dai so, Hinh hoc, ...")
    difficulty: str = Field(default="TH", description="NB, TH, VD, VDC")
    count: int = Field(default=5, ge=1, le=20, description="Number of questions")


class GeneratedQuestion(BaseModel):
    """A single generated question."""
    question: str
    type: str = "TN"
    topic: str = ""
    difficulty: str = "TH"
    answer: str = ""
    solution_steps: List[str] = []


class GenerateResponse(BaseModel):
    """Response containing generated questions."""
    questions: List[GeneratedQuestion]
    sample_count: int = 0
    message: str = ""