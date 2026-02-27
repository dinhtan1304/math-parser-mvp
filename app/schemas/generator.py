from typing import Optional, List
from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    """Request for generating questions of ONE type/difficulty."""
    question_type: str = Field(default="TN", description="TN, TL, ...")
    topic: str = Field(default="", description="Dai so, Hinh hoc, ...")
    difficulty: str = Field(default="TH", description="NB, TH, VD, VDC")
    count: int = Field(default=5, ge=1, le=50)


class ExamSection(BaseModel):
    """One section of an exam (e.g. 5 NB questions)."""
    difficulty: str = Field(description="NB, TH, VD, VDC")
    count: int = Field(ge=1, le=50)


class ExamGenerateRequest(BaseModel):
    """Request for generating a mixed-difficulty exam."""
    topic: str = Field(default="", description="Chu de chinh")
    question_type: str = Field(default="", description="TN, TL or empty for mixed")
    sections: List[ExamSection] = Field(
        default=[
            ExamSection(difficulty="NB", count=3),
            ExamSection(difficulty="TH", count=3),
            ExamSection(difficulty="VD", count=2),
            ExamSection(difficulty="VDC", count=2),
        ],
        description="Phan bo cau hoi theo muc do"
    )


class GeneratedQuestion(BaseModel):
    """A single generated question."""
    question: str
    type: str = "TN"
    topic: str = ""
    difficulty: str = "TH"
    grade: Optional[int] = None
    chapter: str = ""
    lesson_title: str = ""
    answer: str = ""
    solution_steps: List[str] = []


class GenerateResponse(BaseModel):
    """Response containing generated questions."""
    questions: List[GeneratedQuestion]
    sample_count: int = 0
    message: str = ""