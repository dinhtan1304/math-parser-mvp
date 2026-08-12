"""
IELTS Exam Assembler — gom câu hỏi IELTS từ Question bank thành Quiz mới
theo template (full_test, mini_test, ...).

Phase 1: chỉ hỗ trợ `mix_strategy="reuse_passage"` — nguyên cụm passage +
question được nhân bản từ bank thành QuizTheory + QuizQuestion mới.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.question import Question
from app.db.models.quiz import Quiz, QuizQuestion, QuizTheory, QuizTheorySection

logger = logging.getLogger(__name__)


IeltsSkill = Literal["reading", "listening", "writing", "speaking", "unknown"]
TemplateName = Literal[
    "full_test", "reading_only", "listening_only", "writing_only", "mini_test"
]

# Template → (n_reading, n_listening, n_writing, time_limit_min)
TEMPLATE_SPEC: dict[str, tuple[int, int, int, int]] = {
    "full_test":      (3, 4, 2, 175),  # 60 min R + 30 min L + 60 min W + buffer
    "mini_test":      (1, 1, 1, 30),
    "reading_only":   (3, 0, 0, 60),
    "listening_only": (0, 4, 0, 30),
    "writing_only":   (0, 0, 2, 60),
}


@dataclass
class PassageUnit:
    """A coherent group of bank questions sharing the same passage_text.

    For Listening this = 1 IELTS section (10 Qs). For Reading = 1 passage
    (~13 Qs). For Writing = 1 task (1 Q).
    """
    skill: IeltsSkill
    section_title: str
    passage_text: str
    audio_url: Optional[str]
    question_ids: list[int]
    questions: list[Question] = field(default_factory=list)
    passage_hash: str = ""


# ─── Helpers ──────────────────────────────────────────────────────────────────

_SKILL_PATTERNS: list[tuple[re.Pattern[str], IeltsSkill]] = [
    (re.compile(r"^reading", re.I),      "reading"),
    (re.compile(r"^listening", re.I),    "listening"),
    (re.compile(r"^section\s+[1-4]\b", re.I), "listening"),
    (re.compile(r"^writing|task\s*[12]", re.I), "writing"),
    (re.compile(r"^speaking|^part\s+[1-3]\b", re.I), "speaking"),
]


def _classify_skill(section_title: Optional[str]) -> IeltsSkill:
    if not section_title:
        return "unknown"
    t = section_title.strip()
    for pat, sk in _SKILL_PATTERNS:
        if pat.search(t):
            return sk
    return "unknown"


def _parse_extra(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _passage_hash(passage_text: str, exam_id: Optional[int]) -> str:
    """Hash passage to group questions sharing identical passage. Includes
    exam_id to prevent merging questions from different exams that happen to
    use the same Cambridge passage."""
    snippet = (passage_text or "")[:500].strip()
    key = f"{exam_id or 0}::{snippet}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def build_passage_units(questions: list[Question]) -> list[PassageUnit]:
    """Group bank questions into passage units, preserving DB insertion order
    within each unit."""
    units: dict[str, PassageUnit] = {}
    # Order matters — per-passage question order should mirror the original exam.
    for q in sorted(questions, key=lambda r: (r.exam_id or 0, r.question_order or 0)):
        extra = _parse_extra(q.extra_data)
        passage = (extra.get("passage_text") or "").strip()
        chapter = q.chapter or ""
        skill = _classify_skill(chapter)
        h = _passage_hash(passage, q.exam_id)
        unit = units.get(h)
        if unit is None:
            unit = PassageUnit(
                skill=skill,
                section_title=chapter or "IELTS",
                passage_text=passage,
                audio_url=extra.get("audio_url"),
                question_ids=[],
                questions=[],
                passage_hash=h,
            )
            units[h] = unit
        # Refine skill if previously unknown but now classified.
        if unit.skill == "unknown" and skill != "unknown":
            unit.skill = skill
        unit.question_ids.append(q.id)
        unit.questions.append(q)
    return list(units.values())


# ─── Bank fetch ───────────────────────────────────────────────────────────────

async def _fetch_bank_units(
    db: AsyncSession,
    user_id: int,
    exclude_passage_hashes: set[str],
) -> list[PassageUnit]:
    """Fetch IELTS questions from bank visible to user (own + public),
    return passage units."""
    result = await db.execute(
        select(Question)
        .where(
            Question.subject_code == "ielts",
            or_(Question.user_id == user_id, Question.is_public.is_(True)),
        )
    )
    questions = result.scalars().all()
    units = build_passage_units(questions)
    if exclude_passage_hashes:
        units = [u for u in units if u.passage_hash not in exclude_passage_hashes]
    return units


async def _resolve_excluded_passage_hashes(
    db: AsyncSession, exclude_quiz_ids: list[int]
) -> set[str]:
    """Trace QuizQuestion.origin_question_id back to bank Questions, then
    compute passage hashes. Used to avoid re-using passages the user already
    encountered in `exclude_quiz_ids`."""
    if not exclude_quiz_ids:
        return set()
    sub = select(QuizQuestion.origin_question_id).where(
        QuizQuestion.quiz_id.in_(exclude_quiz_ids),
        QuizQuestion.origin_question_id.isnot(None),
    )
    result = await db.execute(sub)
    origin_ids = [r[0] for r in result.all() if r[0] is not None]
    if not origin_ids:
        return set()
    qres = await db.execute(select(Question).where(Question.id.in_(origin_ids)))
    excluded: set[str] = set()
    for q in qres.scalars().all():
        extra = _parse_extra(q.extra_data)
        passage = (extra.get("passage_text") or "").strip()
        excluded.add(_passage_hash(passage, q.exam_id))
    return excluded


# ─── Pick units per template ──────────────────────────────────────────────────

class NotEnoughBankError(Exception):
    """Raised when bank has fewer passage units than the template needs."""

    def __init__(self, skill: str, needed: int, available: int):
        super().__init__(
            f"Cần ít nhất {needed} {skill} unit, hiện chỉ có {available}."
        )
        self.skill = skill
        self.needed = needed
        self.available = available


def _pick_units(units: list[PassageUnit], template: TemplateName, rng: random.Random) -> list[PassageUnit]:
    n_r, n_l, n_w, _time = TEMPLATE_SPEC[template]

    by_skill: dict[IeltsSkill, list[PassageUnit]] = {
        "reading": [u for u in units if u.skill == "reading"],
        "listening": [u for u in units if u.skill == "listening"],
        "writing": [u for u in units if u.skill == "writing"],
    }

    chosen: list[PassageUnit] = []
    for skill, need in [("reading", n_r), ("listening", n_l), ("writing", n_w)]:
        pool = by_skill[skill]  # type: ignore[index]
        if need > 0 and len(pool) < need:
            raise NotEnoughBankError(skill, need, len(pool))
        if need > 0:
            picks = rng.sample(pool, need)
            # Stable display order: reading before listening before writing.
            chosen.extend(picks)
    return chosen


# ─── Build Quiz ───────────────────────────────────────────────────────────────

async def assemble_ielts_quiz(
    db: AsyncSession,
    *,
    user_id: int,
    template: TemplateName,
    name: str,
    exclude_quiz_ids: Optional[list[int]] = None,
    seed: Optional[int] = None,
) -> Quiz:
    """Create a new IELTS Quiz from passages in the user's bank.

    Raises NotEnoughBankError if the bank doesn't have enough material.
    """
    if template not in TEMPLATE_SPEC:
        raise ValueError(f"Unknown template '{template}'")

    excluded_hashes = await _resolve_excluded_passage_hashes(db, exclude_quiz_ids or [])
    units = await _fetch_bank_units(db, user_id, excluded_hashes)
    rng = random.Random(seed)
    chosen = _pick_units(units, template, rng)

    _, _, _, time_limit_min = TEMPLATE_SPEC[template]

    quiz = Quiz(
        name=name,
        created_by_id=user_id,
        subject_code="ielts",
        mode="exam",
        status="draft",
        language="en",
        settings={
            "shuffle_questions": False,
            "shuffle_choices": False,
            "show_correct_after_each": False,
            "allow_review_after_submit": True,
            "grading_mode": "auto",
            "time_limit_minutes": time_limit_min,
            "auto_submit_on_timeout": True,
            "passing_score_type": "points",
            "points_mode": "fixed",
            "negative_scoring": False,
            "hint_penalty": {"level_1": 0, "level_2": 0, "level_3": 0},
            "audio_tracks": {
                u.section_title: u.audio_url
                for u in chosen
                if u.skill == "listening" and u.audio_url
            },
        },
    )
    db.add(quiz)
    await db.flush()

    order = 0
    for sec_idx, unit in enumerate(chosen):
        theory = QuizTheory(
            quiz_id=quiz.id,
            title=unit.section_title,
            content_type="rich_text",
            language="en",
            display_order=sec_idx,
        )
        db.add(theory)
        await db.flush()

        media = (
            {"type": "audio", "url": unit.audio_url, "duration_sec": None}
            if unit.skill == "listening" and unit.audio_url
            else None
        )
        theory_sec = QuizTheorySection(
            theory_id=theory.id,
            order=1,
            content=unit.passage_text or "",
            content_format="markdown",
            media=media,
        )
        db.add(theory_sec)
        await db.flush()

        for q in unit.questions:
            extra = _parse_extra(q.extra_data)
            choices = extra.get("choices") or None
            items = extra.get("items") or None
            answer_raw = q.answer or ""
            try:
                answer_val = json.loads(answer_raw) if answer_raw.startswith("{") else answer_raw
            except Exception:
                answer_val = answer_raw
            qtype = q.question_type or "fill_blank"

            quiz_q = QuizQuestion(
                quiz_id=quiz.id,
                order=order,
                type=qtype,
                question_text=q.question_text,
                answer=answer_val,
                choices=choices,
                items=items,
                points=1.0,
                has_correct_answer=(qtype != "essay"),
                required=True,
                hint_section_id=theory_sec.id,
                scoring={
                    "mode": "all_or_nothing",
                    "word_limit": extra.get("word_limit"),
                },
                source_type="bank_import",
                origin_question_id=q.id,
                extra_metadata={
                    "global_number": extra.get("global_number") or order + 1,
                    "group_instruction": extra.get("group_instruction") or "",
                    "ielts_section": unit.section_title,
                },
            )
            db.add(quiz_q)
            order += 1

    quiz.question_count = order
    await db.commit()
    await db.refresh(quiz)
    return quiz
