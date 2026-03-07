from typing import Optional, List
from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    """Request for generating questions of ONE type/difficulty."""
    question_type: Optional[str] = Field(default=None, description="TN, TL, ... (None = any)")
    topic: Optional[str] = Field(default=None, description="Dai so, Hinh hoc, ... (None = any)")
    difficulty: Optional[str] = Field(default=None, description="NB, TH, VD, VDC (None = any)")
    count: int = Field(default=5, ge=1, le=50)


class ExamSection(BaseModel):
    """One section of an exam (e.g. 5 NB questions)."""
    difficulty: str = Field(description="NB, TH, VD, VDC")
    count: int = Field(ge=1, le=50)


class ExamGenerateRequest(BaseModel):
    """Request for generating a mixed-difficulty exam."""
    topic: Optional[str] = Field(default=None, description="Chu de chinh")
    question_type: Optional[str] = Field(default=None, description="TN, TL or None for mixed")
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

class PromptGenerateRequest(BaseModel):
    """RAG: Sinh đề từ mô tả tự do bằng tiếng Việt."""
    prompt: str = Field(
        description="Mô tả yêu cầu, ví dụ: 'Tạo 10 câu TN lớp 8 về hằng đẳng thức và phân thức, mix NB/TH/VD'",
        min_length=5,
    )
    # Optional overrides — nếu user muốn ép cứng
    grade: Optional[int] = Field(default=None, ge=6, le=12)
    count: Optional[int] = Field(default=None, ge=1, le=50)


class ParsedCriteria(BaseModel):
    """Kết quả parse từ prompt tự do — dùng nội bộ."""
    grade: Optional[int] = None
    chapters: List[str] = []          # ["C2.Hằng đẳng thức", "C6.Phân thức"]
    difficulty_mix: dict = {}         # {"NB": 2, "TH": 4, "VD": 3, "VDC": 1}
    question_type: str = "TN"
    total_count: int = 10
    topic_hint: str = ""              # raw topic string để vector search