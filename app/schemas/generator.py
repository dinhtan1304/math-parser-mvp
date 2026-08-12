from typing import Optional, List, Dict
from pydantic import BaseModel, Field, model_validator

# Upper bound on total questions an exam-matrix request may ask for.
MAX_MATRIX_TOTAL = 300


class GenerateRequest(BaseModel):
    """Request for generating questions of ONE type/difficulty."""
    subject_code: Optional[str] = Field(default="toan", description="Mon hoc: toan, vat-li, hoa-hoc, ...")
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
    subject_code: Optional[str] = Field(default="toan", description="Mon hoc")
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
    subject_code: str = "toan"
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
    context_stats: Optional[dict] = None

class PromptGenerateRequest(BaseModel):
    """RAG: Sinh đề từ mô tả tự do bằng tiếng Việt."""
    prompt: str = Field(
        description="Mô tả yêu cầu, ví dụ: 'Tạo 10 câu TN lớp 8 về hằng đẳng thức và phân thức, mix NB/TH/VD'",
        min_length=5,
    )
    # Optional overrides — nếu user muốn ép cứng
    subject_code: Optional[str] = Field(default=None, description="Mon hoc override")
    grade: Optional[int] = Field(default=None, ge=1, le=12)
    count: Optional[int] = Field(default=None, ge=1, le=50)


class SaveAsExamRequest(BaseModel):
    """Save AI-generated questions as a named exam in the DB."""
    title: str = Field(..., min_length=1, max_length=300)
    questions: List[GeneratedQuestion]


class SaveAsExamResponse(BaseModel):
    exam_id: int
    question_count: int


# ─── Ma trận đề thi (exam blueprint matrix) ──────────────────────────────────

class MatrixCell(BaseModel):
    """One chapter row of the blueprint: how many questions per difficulty."""
    chapter: str = Field(..., min_length=1, description="Tên chương, khớp Question.chapter")
    counts: Dict[str, int] = Field(
        default_factory=dict,
        description='Số câu theo mức độ, ví dụ {"NB":3,"TH":2,"VD":1,"VDC":0}',
    )


class ExamMatrixRequest(BaseModel):
    """Build an exam by pulling existing bank questions matching a
    chapter × difficulty matrix."""
    title: str = Field(..., min_length=1, max_length=300)
    subject_code: str = Field(default="toan")
    grade: int = Field(..., ge=1, le=12)
    cells: List[MatrixCell] = Field(..., min_length=1)
    allow_partial: bool = Field(
        default=True,
        description="True = vẫn tạo đề dù thiếu câu; False = thiếu thì trả 400",
    )
    fill_from_adjacent_difficulty: bool = Field(
        default=False,
        description="Thiếu câu thì bù từ mức độ kề (NB↔TH, TH↔VD, VD↔VDC)",
    )

    @model_validator(mode="after")
    def _check_total(self):
        total = sum(
            max(0, n) for cell in self.cells for n in cell.counts.values()
        )
        if total < 1:
            raise ValueError("Ma trận phải có ít nhất 1 câu.")
        if total > MAX_MATRIX_TOTAL:
            raise ValueError(f"Tổng số câu vượt giới hạn {MAX_MATRIX_TOTAL}.")
        return self


class MatrixDeficit(BaseModel):
    chapter: str
    difficulty: str
    requested: int
    found: int


class ExamMatrixResponse(BaseModel):
    exam_id: int
    question_count: int
    deficits: List[MatrixDeficit] = []


class ParsedCriteria(BaseModel):
    """Kết quả parse từ prompt tự do — dùng nội bộ."""
    subject_code: str = "toan"
    grade: Optional[int] = None
    chapters: List[str] = []          # ["C2.Hằng đẳng thức", "C6.Phân thức"]
    difficulty_mix: dict = {}         # {"NB": 2, "TH": 4, "VD": 3, "VDC": 1}
    question_type: str = "TN"
    total_count: int = 10
    topic_hint: str = ""              # raw topic string để vector search
