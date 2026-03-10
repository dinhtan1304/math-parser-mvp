"""
/api/v1/game — AI generates multiple game formats from exam questions.

For a given assignment, returns questions in one of the game formats:
  - multiple_choice  : classic 4-option quiz (distractors from cross-question answers)
  - drag_drop        : match question ↔ answer (multi-pair batches of 4–5)
  - fill_blank       : fill in the missing part
  - order_steps      : arrange solution steps in order
  - find_error       : find the erroneous step (random step, not always last)
  - flashcard        : front/back card review
  - true_false       : judge if a statement is correct (8-second timer)
  - practice_view    : read-only question viewer for paper solving (no score)

The format is chosen randomly (unless overridden) to prevent boredom.
"""

import json
import logging
import random
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
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

# ─── Mode Registry ─────────────────────────────────────────────

GAME_MODES = [
    "multiple_choice",
    "drag_drop",
    "fill_blank",
    "order_steps",
    "find_error",
    "flashcard",
    "true_false",
    "practice_view",
]

WEIGHTED_MODES = [
    # (mode, weight) — more weight = picked more often
    ("multiple_choice", 3),
    ("fill_blank",      2),
    ("flashcard",       2),
    ("order_steps",     1),
    ("find_error",      1),
    ("drag_drop",       1),
    ("true_false",      2),
    ("practice_view",   1),
]


# ─── Schemas ───────────────────────────────────────────────────

class GameQuestion(BaseModel):
    question_id: int
    question_text: str
    game_mode: str
    payload: Dict[str, Any]
    # Review data — included so client can show answers post-game
    correct_answer: Optional[str] = None
    solution_steps: Optional[List[str]] = None


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


# ─── Helpers ───────────────────────────────────────────────────

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


def _generate_false_answer(correct: str) -> str:
    """Create a plausibly wrong version of a correct answer by modifying a number."""
    def modify_num(m: re.Match) -> str:
        n = m.group()
        try:
            val = float(n.replace(",", "."))
            if val == int(val):
                new_val = -int(val) if val > 0 else int(val) + 2
                return str(new_val)
            return str(round(val + 1.5, 2))
        except Exception:
            return n

    modified = re.sub(r"-?\d+(?:[.,]\d+)?", modify_num, correct, count=1)
    if modified == correct:
        # No number found — prepend negation
        return "Không " + correct[:1].lower() + correct[1:] if not correct.lower().startswith("không") else correct[6:].strip()
    return modified


# ─── Per-question Builders ─────────────────────────────────────

def _build_multiple_choice(q: Question, cross_answers: List[str]) -> Dict[str, Any]:
    """
    4-option quiz. Distractors come from other questions' answers in the same exam.
    Falls back to synthetic stubs if fewer than 3 cross-answers are available.
    """
    correct = q.answer or "?"
    # Collect distractors from other questions (exclude self)
    pool = [a for a in cross_answers if a and a != correct]
    random.shuffle(pool)
    distractors = pool[:3]
    # Pad with synthetic stubs if needed
    while len(distractors) < 3:
        distractors.append(f"Phương án {len(distractors) + 1}")
    options = distractors[:3] + [correct]
    random.shuffle(options)
    return {
        "options": options,
        "correct_index": options.index(correct),
        "correct_text": correct,
    }


def _build_fill_blank(q: Question) -> Dict[str, Any]:
    """Replace the last number/token in the answer with a blank."""
    answer = q.answer or "?"
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
        steps = [s.strip() for s in (q.answer or "Bước 1. Xem đề bài.").split(".") if s.strip()]
        if len(steps) < 2:
            steps = ["Bước 1: Đọc đề bài", "Bước 2: Áp dụng công thức", "Bước 3: Tính kết quả"]
    shuffled = steps[:]
    random.shuffle(shuffled)
    forward = [steps.index(s) for s in shuffled]
    correct_order = [0] * len(steps)
    for shuffled_idx, orig_idx in enumerate(forward):
        correct_order[orig_idx] = shuffled_idx
    return {
        "steps": shuffled,
        "correct_order": correct_order,
        "original_steps": steps,
    }


def _build_find_error(q: Question) -> Dict[str, Any]:
    """
    Present a solution — student finds the erroneous step.
    Picks a random step (not always the last) so it has educational value.
    For production: use Gemini to insert a real mathematical error.
    """
    steps = _parse_solution_steps(q.solution_steps)
    if not steps:
        steps = [
            "Bước 1: Xác định yêu cầu đề bài",
            "Bước 2: Áp dụng công thức",
            "Bước 3: Tính toán (có lỗi ở đây)",
            "Bước 4: Kết luận",
        ]
    # Pick a random step (prefer middle steps, avoid first step)
    candidates = list(range(1, len(steps)))  # skip step 0 (usually setup)
    error_index = random.choice(candidates) if candidates else len(steps) - 1
    return {
        "steps": steps,
        "error_index": error_index,
        "hint": "Một trong các bước trên có lỗi sai về toán học",
    }


def _build_flashcard(q: Question) -> Dict[str, Any]:
    steps = _parse_solution_steps(q.solution_steps)
    hint = steps[0] if steps else (q.solution_steps[:100] if q.solution_steps else None)
    return {
        "front": q.question_text,
        "back": q.answer or "(Chưa có đáp án)",
        "hint": hint,
    }


def _build_true_false(q: Question) -> Dict[str, Any]:
    """
    Show a statement; student judges True or False in 8 seconds.
    60% chance of being a true statement, 40% false (to avoid pattern-guessing).
    """
    correct = q.answer or "?"
    is_true = random.random() < 0.6  # 60 % true

    if is_true:
        statement = f"{q.question_text}\n→ Kết quả: {correct}"
        explanation = f"Đúng. Đáp án chính xác là: {correct}"
    else:
        false_answer = _generate_false_answer(correct)
        statement = f"{q.question_text}\n→ Kết quả: {false_answer}"
        explanation = f"Sai. Đáp án đúng phải là: {correct}"

    return {
        "statement": statement,
        "is_true": is_true,
        "explanation": explanation,
    }


def _build_practice_view(q: Question, show_answer: bool = True) -> Dict[str, Any]:
    """
    Read-only question card for paper solving. No timer, no score.
    `show_answer` is inherited from the assignment's setting.
    """
    steps = _parse_solution_steps(q.solution_steps)
    return {
        "answer": (q.answer or "") if show_answer else "",
        "solution_steps": steps if show_answer else [],
        "difficulty": q.difficulty or "",
        "topic": q.topic or "",
        "show_answer": show_answer,
    }


# ─── Batch Builder: Drag-Drop ──────────────────────────────────

_DRAG_DROP_BATCH_SIZE = 4  # pairs per drag-drop round


def _build_drag_drop_session(questions: List[Question]) -> List[GameQuestion]:
    """
    Group questions into batches of up to _DRAG_DROP_BATCH_SIZE pairs.
    Each batch becomes one DragDropGame round with multiple matching pairs.
    """
    game_questions: List[GameQuestion] = []
    for batch_start in range(0, len(questions), _DRAG_DROP_BATCH_SIZE):
        batch = questions[batch_start : batch_start + _DRAG_DROP_BATCH_SIZE]
        valid = [q for q in batch if q.answer]
        if not valid:
            continue

        items = [q.question_text[:80] for q in valid]
        targets_ordered = [q.answer for q in valid]
        targets_shuffled = targets_ordered[:]
        random.shuffle(targets_shuffled)

        payload: Dict[str, Any] = {
            "items": items,
            "targets": targets_shuffled,
            "correct_pairs": list(zip(items, targets_ordered)),
        }
        # Use first question's id/text as representative for this batch
        game_questions.append(
            GameQuestion(
                question_id=valid[0].id,
                question_text="Ghép đúng câu hỏi với đáp án",
                game_mode="drag_drop",
                payload=payload,
                correct_answer="; ".join(f"{q.question_text[:30]} → {q.answer}" for q in valid if q.answer),
                solution_steps=[],
            )
        )
    return game_questions


# ─── Standard Builder Map ──────────────────────────────────────

_SIMPLE_BUILDERS = {
    "fill_blank":    _build_fill_blank,
    "order_steps":   _build_order_steps,
    "find_error":    _build_find_error,
    "flashcard":     _build_flashcard,
    "true_false":    _build_true_false,
}


# ─── Endpoints ─────────────────────────────────────────────────

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
    assignment = await db.scalar(
        select(Assignment).where(
            Assignment.id == payload.assignment_id,
            Assignment.is_active == True,
        )
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

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
    game_questions: List[GameQuestion] = []

    # ── Drag-drop: batch-level building ──
    if mode == "drag_drop":
        game_questions = _build_drag_drop_session(questions)

    # ── Multiple choice: use cross-question answers as distractors ──
    elif mode == "multiple_choice":
        cross_answers = [q.answer for q in questions if q.answer]
        for q in questions:
            try:
                q_payload = _build_multiple_choice(q, cross_answers)
            except Exception as exc:
                logger.warning("MC build failed for q%d: %s", q.id, exc)
                continue
            game_questions.append(GameQuestion(
                question_id=q.id,
                question_text=q.question_text,
                game_mode=mode,
                payload=q_payload,
                correct_answer=q.answer,
                solution_steps=_parse_solution_steps(q.solution_steps),
            ))

    # ── Practice view: uses assignment.show_answer setting ──
    elif mode == "practice_view":
        show_ans = bool(getattr(assignment, "show_answer", True))
        for q in questions:
            game_questions.append(GameQuestion(
                question_id=q.id,
                question_text=q.question_text,
                game_mode=mode,
                payload=_build_practice_view(q, show_ans),
                correct_answer=q.answer,
                solution_steps=_parse_solution_steps(q.solution_steps),
            ))

    # ── All other modes: per-question builder ──
    else:
        builder = _SIMPLE_BUILDERS[mode]
        for q in questions:
            try:
                q_payload = builder(q)
            except Exception as exc:
                logger.warning("Build failed for q%d mode=%s: %s", q.id, mode, exc)
                continue
            game_questions.append(GameQuestion(
                question_id=q.id,
                question_text=q.question_text,
                game_mode=mode,
                payload=q_payload,
                correct_answer=q.answer,
                solution_steps=_parse_solution_steps(q.solution_steps),
            ))

    if not game_questions:
        raise HTTPException(status_code=422, detail="Không thể tạo game từ đề thi này")

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
        {"mode": "multiple_choice", "label": "Trắc nghiệm nhanh",   "desc": "4 đáp án, đếm ngược 15s"},
        {"mode": "drag_drop",       "label": "Ghép cặp",             "desc": "Kéo thả câu hỏi ↔ đáp án"},
        {"mode": "fill_blank",      "label": "Điền vào chỗ trống",   "desc": "Điền số/từ thiếu"},
        {"mode": "order_steps",     "label": "Sắp xếp bước giải",    "desc": "Sắp đúng thứ tự các bước"},
        {"mode": "find_error",      "label": "Tìm lỗi sai",          "desc": "Bài giải có lỗi — bạn tìm được không?"},
        {"mode": "flashcard",       "label": "Thẻ ghi nhớ",          "desc": "Lật thẻ xem đáp án"},
        {"mode": "true_false",      "label": "Đúng hay Sai",         "desc": "Phán đoán nhanh trong 8 giây"},
        {"mode": "practice_view",   "label": "Xem đề",               "desc": "Đọc đề, tự giải vào giấy"},
    ]


@router.get("/preview/{assignment_id}", response_model=Dict[str, Any])
async def preview_all_modes(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Preview the first question in all game modes (teacher only)."""
    assignment = await db.scalar(
        select(Assignment).where(Assignment.id == assignment_id)
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

    cls = await db.get(Class, assignment.class_id)
    if not cls or cls.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ giáo viên mới được xem preview")

    if not assignment.exam_id:
        raise HTTPException(status_code=422, detail="Bài tập chưa liên kết đề thi")

    result = await db.execute(
        select(Question)
        .where(Question.exam_id == assignment.exam_id)
        .order_by(Question.question_order)
        .limit(5)  # need a few for drag_drop multi-pair preview
    )
    all_qs = list(result.scalars().all())
    if not all_qs:
        raise HTTPException(status_code=422, detail="Đề thi chưa có câu hỏi")

    q = all_qs[0]
    cross_answers = [x.answer for x in all_qs if x.answer]
    show_ans = bool(getattr(assignment, "show_answer", True))

    previews: Dict[str, Any] = {}
    for mode in GAME_MODES:
        try:
            if mode == "drag_drop":
                batch = _build_drag_drop_session(all_qs[:4])
                previews[mode] = {"question_id": q.id, "question_text": "Ghép cặp", "payload": batch[0].payload if batch else {}}
            elif mode == "multiple_choice":
                previews[mode] = {"question_id": q.id, "question_text": q.question_text, "payload": _build_multiple_choice(q, cross_answers)}
            elif mode == "practice_view":
                previews[mode] = {"question_id": q.id, "question_text": q.question_text, "payload": _build_practice_view(q, show_ans)}
            else:
                builder = _SIMPLE_BUILDERS[mode]
                previews[mode] = {"question_id": q.id, "question_text": q.question_text, "payload": builder(q)}
        except Exception as exc:
            previews[mode] = {"error": str(exc)}

    return {"question_id": q.id, "question_text": q.question_text, "modes": previews}
