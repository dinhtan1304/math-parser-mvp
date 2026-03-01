"""
/api/v1/game — AI generates multiple game formats from exam questions.

For a given assignment, returns questions in one of the game formats:
  - multiple_choice  : classic 4-option quiz
  - drag_drop        : match question ↔ answer
  - fill_blank       : fill in the missing part
  - order_steps      : arrange solution steps in order
  - find_error       : AI inserts a bug; student finds it
  - flashcard        : front/back card review

The format is chosen randomly (unless overridden) to prevent boredom.
"""

import json
import logging
import random
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import Assignment, Class, ClassMember
from app.db.models.exam import Exam
from app.db.models.question import Question

router = APIRouter()
logger = logging.getLogger(__name__)

# ─── Schemas ─────────────────────────────────────────────────

GAME_MODES = [
    "multiple_choice",
    "drag_drop",
    "fill_blank",
    "order_steps",
    "find_error",
    "flashcard",
]

WEIGHTED_MODES = [
    # (mode, weight) — more weight = picked more often
    ("multiple_choice", 3),
    ("drag_drop",       2),
    ("fill_blank",      2),
    ("order_steps",     1),
    ("find_error",      1),
    ("flashcard",       1),
]


class GameQuestion(BaseModel):
    question_id: int
    question_text: str
    game_mode: str
    payload: Dict[str, Any]   # mode-specific data


class GameSession(BaseModel):
    assignment_id: int
    game_mode: str
    questions: List[GameQuestion]
    total: int


class GameSessionRequest(BaseModel):
    assignment_id: int
    game_mode: Optional[str] = Field(
        None,
        description="Force a specific mode; omit for random selection",
    )


# ─── Helpers ─────────────────────────────────────────────────

def _pick_mode(override: Optional[str] = None) -> str:
    if override and override in GAME_MODES:
        return override
    modes, weights = zip(*WEIGHTED_MODES)
    return random.choices(modes, weights=weights, k=1)[0]


def _parse_solution_steps(steps_raw: Optional[str]) -> List[str]:
    if not steps_raw:
        return []
    try:
        parsed = json.loads(steps_raw)
        if isinstance(parsed, list):
            return [str(s) for s in parsed]
    except Exception:
        pass
    return [s.strip() for s in steps_raw.split("\n") if s.strip()]


def _build_multiple_choice(q: Question) -> Dict[str, Any]:
    """Return question with answer field; distractors come from the question's stored options or
    are AI-generated stubs. For MVP we use the stored answer + synthetic distractors."""
    correct = q.answer or "?"
    # Synthetic distractors — in production, call AI to generate real ones
    distractors = [
        f"[Đáp án nhiễu {i}]" for i in range(1, 4)
    ]
    options = distractors + [correct]
    random.shuffle(options)
    return {
        "options": options,
        "correct_index": options.index(correct),
        "correct_text": correct,
    }


def _build_drag_drop(q: Question) -> Dict[str, Any]:
    """Provide a list of (item, target) pairs for matching."""
    correct = q.answer or "?"
    pairs = [{"item": q.question_text[:80], "target": correct}]
    # Shuffle targets
    targets = [p["target"] for p in pairs]
    random.shuffle(targets)
    return {
        "items": [p["item"] for p in pairs],
        "targets": targets,
        "correct_pairs": [(p["item"], p["target"]) for p in pairs],
    }


def _build_fill_blank(q: Question) -> Dict[str, Any]:
    """Replace the last number/token in the answer with a blank."""
    answer = q.answer or "?"
    # Simple heuristic: hide last word/number
    tokens = answer.split()
    if len(tokens) > 1:
        blank_index = len(tokens) - 1
        prompt = " ".join(tokens[:blank_index]) + " ___"
        hidden = tokens[blank_index]
    else:
        prompt = "___"
        hidden = answer
    return {"prompt": prompt, "hidden": hidden, "full_answer": answer}


def _build_order_steps(q: Question) -> Dict[str, Any]:
    """Shuffle solution steps; student re-orders them."""
    steps = _parse_solution_steps(q.solution_steps)
    if len(steps) < 2:
        # Fallback: split answer into sentences
        steps = [s.strip() for s in (q.answer or "Bước 1. Xem đề bài.").split(".") if s.strip()]
        if not steps:
            steps = ["Bước 1", "Bước 2", "Bước 3"]
    correct_order = list(range(len(steps)))
    shuffled = steps[:]
    random.shuffle(shuffled)
    return {
        "steps": shuffled,
        "correct_order": [steps.index(s) for s in shuffled],
        "original_steps": steps,
    }


def _build_find_error(q: Question) -> Dict[str, Any]:
    """
    Present a solution with one deliberate error.
    For MVP: mark the last step as the error.
    In production: call AI to insert a real mathematical error.
    """
    steps = _parse_solution_steps(q.solution_steps)
    if not steps:
        steps = [
            "Bước 1: Xác định yêu cầu đề bài",
            "Bước 2: Áp dụng công thức",
            "Bước 3: Tính toán (có lỗi ở đây)",
            "Bước 4: Kết luận",
        ]
    # Mark last step as containing the error (stub)
    error_index = len(steps) - 1
    return {
        "steps": steps,
        "error_index": error_index,
        "hint": "Một trong các bước trên có lỗi sai về toán học",
    }


def _build_flashcard(q: Question) -> Dict[str, Any]:
    return {
        "front": q.question_text,
        "back": q.answer or "(Chưa có đáp án)",
        "hint": q.solution_steps[:100] if q.solution_steps else None,
    }


_BUILDERS = {
    "multiple_choice": _build_multiple_choice,
    "drag_drop":       _build_drag_drop,
    "fill_blank":      _build_fill_blank,
    "order_steps":     _build_order_steps,
    "find_error":      _build_find_error,
    "flashcard":       _build_flashcard,
}


# ─── Endpoints ───────────────────────────────────────────────

@router.post("/session", response_model=GameSession)
async def create_game_session(
    payload: GameSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Create a game session for an assignment.

    Returns all questions formatted for the chosen game mode.
    If `game_mode` is omitted, one is chosen randomly (weighted).
    """
    # Verify access
    assignment = await db.scalar(
        select(Assignment).where(
            Assignment.id == payload.assignment_id,
            Assignment.is_active == True,
        )
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

    # Check student enrollment OR teacher ownership
    cls = await db.get(Class, assignment.class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Lớp không tồn tại")

    is_teacher = cls.teacher_id == current_user.id
    if not is_teacher:
        enrolled = await db.scalar(
            select(ClassMember).where(
                ClassMember.class_id == cls.id,
                ClassMember.student_id == current_user.id,
                ClassMember.is_active == True,
            )
        )
        if not enrolled:
            raise HTTPException(status_code=403, detail="Bạn chưa tham gia lớp này")

    # Load questions from the exam
    questions: List[Question] = []
    if assignment.exam_id:
        result = await db.execute(
            select(Question)
            .where(Question.exam_id == assignment.exam_id)
            .order_by(Question.question_order)
        )
        questions = list(result.scalars().all())

    if not questions:
        raise HTTPException(
            status_code=422,
            detail="Đề thi chưa có câu hỏi. Vui lòng parse đề trước.",
        )

    mode = _pick_mode(payload.game_mode)
    builder = _BUILDERS[mode]

    game_questions = []
    for q in questions:
        try:
            q_payload = builder(q)
        except Exception as exc:
            logger.warning("Failed to build game question %d: %s", q.id, exc)
            continue
        game_questions.append(
            GameQuestion(
                question_id=q.id,
                question_text=q.question_text,
                game_mode=mode,
                payload=q_payload,
            )
        )

    return GameSession(
        assignment_id=payload.assignment_id,
        game_mode=mode,
        questions=game_questions,
        total=len(game_questions),
    )


@router.get("/modes", response_model=List[Dict[str, str]])
async def list_game_modes():
    """Return metadata about available game modes."""
    return [
        {"mode": "multiple_choice", "label": "Trắc nghiệm nhanh",  "icon": "⚡", "desc": "4 đáp án, đếm ngược 15s"},
        {"mode": "drag_drop",       "label": "Ghép cặp",           "icon": "🧩", "desc": "Kéo thả câu hỏi ↔ đáp án"},
        {"mode": "fill_blank",      "label": "Điền vào chỗ trống", "icon": "🔢", "desc": "Điền số/từ thiếu"},
        {"mode": "order_steps",     "label": "Sắp xếp bước giải",  "icon": "📊", "desc": "Kéo để sắp xếp đúng thứ tự"},
        {"mode": "find_error",      "label": "Tìm lỗi sai",        "icon": "🔍", "desc": "Bài giải có lỗi — bạn tìm được không?"},
        {"mode": "flashcard",       "label": "Thẻ ghi nhớ",        "icon": "🃏", "desc": "Lật thẻ xem đáp án"},
    ]


@router.get("/preview/{assignment_id}", response_model=Dict[str, Any])
async def preview_all_modes(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Preview the first question in ALL game modes.
    Useful for the teacher to see how the exam will look for students.
    """
    assignment = await db.scalar(
        select(Assignment).where(Assignment.id == assignment_id)
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

    # Teacher only
    cls = await db.get(Class, assignment.class_id)
    if not cls or cls.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ giáo viên mới được xem preview")

    if not assignment.exam_id:
        raise HTTPException(status_code=422, detail="Bài tập chưa liên kết đề thi")

    result = await db.execute(
        select(Question)
        .where(Question.exam_id == assignment.exam_id)
        .order_by(Question.question_order)
        .limit(1)
    )
    q = result.scalars().first()
    if not q:
        raise HTTPException(status_code=422, detail="Đề thi chưa có câu hỏi")

    previews = {}
    for mode, builder in _BUILDERS.items():
        try:
            previews[mode] = {
                "question_id": q.id,
                "question_text": q.question_text,
                "payload": builder(q),
            }
        except Exception as exc:
            previews[mode] = {"error": str(exc)}

    return {
        "question_id": q.id,
        "question_text": q.question_text,
        "modes": previews,
    }