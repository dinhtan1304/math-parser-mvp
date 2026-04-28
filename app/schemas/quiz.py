"""
Pydantic schemas for the Quiz system.
Covers: Quiz CRUD, QuizQuestion, QuizTheory, QuizAttempt, QuizAnswer.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


# ─── Quiz ────────────────────────────────────────────────────

class QuizSettings(BaseModel):
    """Quiz-level settings (stored as JSON on quiz.settings)."""
    time_limit_minutes: Optional[int] = None
    shuffle_questions: bool = False
    shuffle_choices: bool = True
    show_correct_after_each: bool = True
    allow_retake: bool = True
    max_retakes: Optional[int] = None
    passing_score: Optional[float] = 5.0
    passing_score_type: str = "points"          # points | percentage
    points_mode: str = "fixed"                  # fixed | speed_bonus
    show_leaderboard: bool = True
    allow_review_after_submit: bool = True
    auto_submit_on_timeout: bool = True
    hint_penalty: Dict[str, float] = Field(
        default={"level_1": 0, "level_2": 0.25, "level_3": 0.50}
    )
    negative_scoring: bool = False
    question_selection_count: Optional[int] = None      # None = all questions
    difficulty_distribution: Dict[str, float] = Field(
        default={"easy": 0.50, "medium": 0.30, "hard": 0.15, "expert": 0.05}
    )
    grading_mode: str = "auto"                  # auto | manual


class QuizCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    subject_code: Optional[str] = None
    grade: Optional[int] = Field(None, ge=1, le=12)
    mode: str = "quiz"
    language: str = "vi"
    visibility: str = "private"
    tags: List[str] = []
    settings: Optional[QuizSettings] = None


class QuizUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=300)
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    subject_code: Optional[str] = None
    grade: Optional[int] = Field(None, ge=1, le=12)
    mode: Optional[str] = None
    language: Optional[str] = None
    visibility: Optional[str] = None
    tags: Optional[List[str]] = None
    settings: Optional[QuizSettings] = None
    status: Optional[str] = None                # draft | published | archived


class QuizResponse(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    created_by_id: int
    subject_code: Optional[str] = None
    grade: Optional[int] = None
    mode: str
    language: str
    visibility: str
    status: str
    tags: List[str] = []
    version: int
    settings: Dict[str, Any] = {}
    question_count: int = 0
    total_points: Optional[float] = 0
    created_at: datetime
    updated_at: datetime
    published_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class QuizListResponse(BaseModel):
    items: List[QuizResponse]
    total: int
    page: int
    page_size: int


# ─── QuizTheory ──────────────────────────────────────────────

class TheorySectionCreate(BaseModel):
    order: int = 0
    content: str
    content_format: str = "markdown"
    media: Optional[Dict[str, Any]] = None


class TheorySectionResponse(BaseModel):
    id: int
    theory_id: int
    order: int
    content: str
    content_format: str
    media: Optional[Dict[str, Any]] = None

    model_config = {"from_attributes": True}


class QuizTheoryCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content_type: str = "rich_text"
    language: str = "vi"
    tags: List[str] = []
    display_order: int = 0
    sections: List[TheorySectionCreate] = []


class QuizTheoryUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    content_type: Optional[str] = None
    language: Optional[str] = None
    tags: Optional[List[str]] = None
    display_order: Optional[int] = None


class QuizTheoryResponse(BaseModel):
    id: int
    quiz_id: int
    title: str
    content_type: str
    language: str
    tags: List[str] = []
    display_order: int
    created_at: datetime
    sections: List[TheorySectionResponse] = []

    model_config = {"from_attributes": True}


# ─── QuizQuestion ────────────────────────────────────────────

class ChoiceItem(BaseModel):
    key: str                                    # "A", "B", "C", "D"
    text: str
    is_correct: bool = False
    media: Optional[Dict[str, Any]] = None


class ReorderItem(BaseModel):
    id: str                                     # "I1", "I2"
    text: str


class FillBlankAnswer(BaseModel):
    display: Optional[str] = None
    accept: List[str] = []
    match_mode: str = "exact_list"              # exact_list | contains | regex
    case_sensitive: bool = False
    trim_whitespace: bool = True


class ScoringRules(BaseModel):
    mode: str = "all_or_nothing"                # all_or_nothing | per_blank | partial
    partial_credit: bool = False
    partial_formula: Optional[str] = None
    penalty_wrong_choice: float = 0
    points_per_blank: Optional[float] = None
    word_limit: Optional[str] = None           # IELTS: "TWO WORDS AND/OR A NUMBER"
    source: Optional[str] = None               # IELTS: "passage"


class SolutionData(BaseModel):
    steps: List[str] = []
    explanation: Optional[str] = None


class QuizQuestionCreate(BaseModel):
    """Create a quiz question (manual or import)."""
    type: str = Field(..., description="multiple_choice | checkbox | fill_blank | reorder | true_false | essay | true_false_not_given | yes_no_not_given | matching | matching_headings")
    question_text: str = Field(..., min_length=1)
    order: Optional[int] = None
    code: Optional[str] = None

    has_correct_answer: bool = True
    required: bool = True
    points: float = 1.0
    time_limit_seconds: Optional[int] = None
    difficulty: Optional[str] = None
    subject_code: Optional[str] = None
    tags: List[str] = []
    media: Optional[Dict[str, Any]] = None

    answer: Optional[Any] = None                # polymorphic
    choices: Optional[List[ChoiceItem]] = None
    items: Optional[List[ReorderItem]] = None
    scoring: Optional[ScoringRules] = None
    solution: Optional[SolutionData] = None

    hint_section_id: Optional[int] = None
    hint_auto_linked: bool = False
    metadata: Optional[Dict[str, Any]] = None


class BatchCreateQuestionsRequest(BaseModel):
    """Batch-create quiz questions (e.g. from JSON file import)."""
    questions: List[QuizQuestionCreate] = Field(..., min_length=1, max_length=500)
    source_type: str = "file_import"


class QuizQuestionUpdate(BaseModel):
    """Update a quiz question. All fields optional."""
    type: Optional[str] = None
    question_text: Optional[str] = None
    order: Optional[int] = None
    code: Optional[str] = None

    has_correct_answer: Optional[bool] = None
    required: Optional[bool] = None
    points: Optional[float] = None
    time_limit_seconds: Optional[int] = None
    difficulty: Optional[str] = None
    subject_code: Optional[str] = None
    tags: Optional[List[str]] = None
    media: Optional[Dict[str, Any]] = None

    answer: Optional[Any] = None
    choices: Optional[List[ChoiceItem]] = None
    items: Optional[List[ReorderItem]] = None
    scoring: Optional[ScoringRules] = None
    solution: Optional[SolutionData] = None

    hint_section_id: Optional[int] = None
    hint_auto_linked: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


class QuizQuestionResponse(BaseModel):
    id: int
    quiz_id: int
    origin_question_id: Optional[int] = None
    source_type: str
    origin_quiz_code: Optional[str] = None
    order: int
    code: Optional[str] = None
    type: str
    question_text: str
    has_correct_answer: bool
    required: bool
    points: float
    time_limit_seconds: Optional[int] = None
    difficulty: Optional[str] = None
    subject_code: Optional[str] = None
    tags: List[str] = []
    media: Optional[Dict[str, Any]] = None
    answer: Optional[Any] = None
    choices: Optional[List[Dict[str, Any]]] = None
    items: Optional[List[Dict[str, Any]]] = None
    scoring: Optional[Dict[str, Any]] = None
    solution: Optional[Dict[str, Any]] = None
    hint_section_id: Optional[int] = None
    hint_auto_linked: bool = False
    metadata: Optional[Dict[str, Any]] = Field(None, validation_alias="extra_metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Import from bank ────────────────────────────────────────

class ImportQuestionsRequest(BaseModel):
    """Import questions from bank into a quiz."""
    question_ids: List[int] = Field(..., min_length=1)
    source_type: str = "bank_import"            # bank_import | file_import
    target_type: Optional[Literal[
        "multiple_choice", "checkbox", "fill_blank",
        "reorder", "true_false", "essay",
    ]] = None  # None = auto-detect (no LLM)


class SkippedQuestion(BaseModel):
    """One question that was skipped during import, with reason."""
    question_id: int
    reason: str  # no_access | empty_text | convert_error


class ImportQuestionsResponse(BaseModel):
    """Detailed result of a bank import operation."""
    imported: List[QuizQuestionResponse]
    imported_count: int
    skipped: List[SkippedQuestion] = []
    skipped_count: int = 0
    total_requested: int


# ─── Full Quiz Delivery (for student taking quiz) ────────────

class QuizDeliveryQuestion(BaseModel):
    """Question as delivered to student (no answer/solution)."""
    id: int
    order: int
    code: Optional[str] = None
    type: str
    question_text: str
    required: bool
    points: float
    time_limit_seconds: Optional[int] = None
    difficulty: Optional[str] = None
    media: Optional[Dict[str, Any]] = None
    choices: Optional[List[Dict[str, Any]]] = None       # without is_correct
    items: Optional[List[Dict[str, Any]]] = None
    blank_count: int = 0                                     # fill_blank: number of blanks
    blank_labels: Optional[List[str]] = None                  # fill_blank: e.g. ["B1","B2"]
    has_hint: bool = False
    hint_section_id: Optional[int] = None


class QuizDeliveryResponse(BaseModel):
    """Full quiz delivered to student."""
    id: int
    code: str
    name: str
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    subject_code: Optional[str] = None
    grade: Optional[int] = None
    mode: str
    settings: Dict[str, Any]
    question_count: int
    total_points: Optional[float] = 0
    questions: List[QuizDeliveryQuestion]
    theories: List[QuizTheoryResponse] = []


# ─── Quiz Attempt ────────────────────────────────────────────

class StartAttemptRequest(BaseModel):
    quiz_id: int
    assignment_id: Optional[int] = None


class SubmitAnswerItem(BaseModel):
    question_id: int
    given_answer: Optional[Any] = None          # polymorphic
    time_ms: Optional[int] = None
    hint_used: bool = False
    hint_level: int = 0


class SubmitAttemptRequest(BaseModel):
    answers: List[SubmitAnswerItem]


class QuizAnswerResponse(BaseModel):
    id: int
    question_id: int
    given_answer: Optional[Any] = None
    is_correct: Optional[bool] = None
    points_earned: float = 0
    time_ms: Optional[int] = None
    hint_used: bool = False
    hint_level: int = 0
    correct_answer: Optional[Any] = None
    explanation: Optional[str] = None
    teacher_comment: Optional[str] = None

    model_config = {"from_attributes": True}


class QuizAttemptResponse(BaseModel):
    id: int
    quiz_id: int
    student_id: Optional[int] = None
    assignment_id: Optional[int] = None
    attempt_no: int
    status: str                                     # in_progress | completed | pending_review | timed_out | abandoned
    score: Optional[float] = None
    max_score: Optional[float] = None
    percentage: Optional[float] = None
    passed: Optional[bool] = None
    total_questions: int
    correct_count: int
    time_spent_s: Optional[int] = None
    xp_earned: int = 0
    selected_question_ids: Optional[List[int]] = None
    graded_by_id: Optional[int] = None
    graded_at: Optional[datetime] = None
    started_at: datetime
    submitted_at: Optional[datetime] = None
    answers: List[QuizAnswerResponse] = []

    model_config = {"from_attributes": True}


# ─── Manual Grading ─────────────────────────────────────────

class GradeAnswerRequest(BaseModel):
    """Teacher grades a single answer within an attempt."""
    points_earned: float = Field(..., ge=0)
    is_correct: Optional[bool] = None               # None = partial credit
    teacher_comment: Optional[str] = Field(None, max_length=1000)


class FinalizeGradingRequest(BaseModel):
    """Finalize grading for an attempt (optional score override)."""
    passed: Optional[bool] = None                    # override pass/fail, or auto-calculate


# ─── Hint response ──────────────────────────────────────────

class HintResponse(BaseModel):
    """Response for a hint request on a specific question."""
    question_id: int
    hint_level: int                                    # 1, 2, or 3
    theory_content: Optional[str] = None               # Level 1: theory section text
    theory_title: Optional[str] = None                 # Level 1: theory title
    solution: Optional[SolutionData] = None            # Level 2: solution steps (no explanation)
    answer: Optional[Any] = None                       # Level 3: correct answer
    explanation: Optional[str] = None                  # Level 3: explanation text
