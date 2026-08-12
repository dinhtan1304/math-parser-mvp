"""
IELTS Writing AI grader — call Gemini to score essays against the official
9-band descriptors. Returns four sub-scores (TA/CC/LR/GRA) + overall band
+ Vietnamese feedback per criterion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)


TaskType = Literal["task_1", "task_2"]


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class WritingGradeResult:
    band_ta: float
    band_cc: float
    band_lr: float
    band_gra: float
    band_overall: float
    feedback: dict[str, str]   # {ta, cc, lr, gra}
    model: str
    grade_hash: str


# ─── Hashing ──────────────────────────────────────────────────────────────────

def compute_grade_hash(essay_text: str, prompt_text: str, task_type: TaskType) -> str:
    norm_essay = " ".join((essay_text or "").strip().split()).lower()
    norm_prompt = " ".join((prompt_text or "").strip().split()).lower()
    payload = f"{task_type}::{norm_prompt}::{norm_essay}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── Gemini schema ────────────────────────────────────────────────────────────

# Schema enforced via response_schema (genai SDK). Use plain dict so we don't
# need to import types at module load time.
WRITING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ta":      {"type": "object", "properties": {
            "band": {"type": "number"}, "feedback": {"type": "string"}
        }, "required": ["band", "feedback"]},
        "cc":      {"type": "object", "properties": {
            "band": {"type": "number"}, "feedback": {"type": "string"}
        }, "required": ["band", "feedback"]},
        "lr":      {"type": "object", "properties": {
            "band": {"type": "number"}, "feedback": {"type": "string"}
        }, "required": ["band", "feedback"]},
        "gra":     {"type": "object", "properties": {
            "band": {"type": "number"}, "feedback": {"type": "string"}
        }, "required": ["band", "feedback"]},
        "overall": {"type": "number"},
    },
    "required": ["ta", "cc", "lr", "gra", "overall"],
}


_SYSTEM_PROMPT = (
    "You are an IELTS examiner certified by the British Council with 10 years "
    "of experience grading Academic Writing. Apply the official 9-band "
    "descriptors strictly. Output ONLY valid JSON matching the response schema. "
    "Be honest, not encouraging — calibrate against real exam outcomes."
)


def _build_user_prompt(
    essay_text: str,
    task_type: TaskType,
    prompt_text: str,
    chart_description: Optional[str],
) -> str:
    word_limit = 150 if task_type == "task_1" else 250
    chart = f"CHART/IMAGE DESCRIPTION: {chart_description}\n" if (task_type == "task_1" and chart_description) else ""
    return (
        f"TASK TYPE: {task_type}\n"
        f"TASK PROMPT: {prompt_text}\n"
        f"{chart}"
        f"WORD LIMIT: at least {word_limit} words\n\n"
        f"CANDIDATE'S ESSAY:\n\"\"\"\n{essay_text}\n\"\"\"\n\n"
        "GRADE on 4 criteria (each 0-9, step 0.5):\n"
        "1. Task Achievement (Task 1) / Task Response (Task 2): does it address every part of the prompt? "
        "For Task 1, is an overview present?\n"
        "2. Coherence & Cohesion: paragraphing, logical progression, linking, referencing.\n"
        "3. Lexical Resource: range, accuracy, collocation, less-common items.\n"
        "4. Grammatical Range & Accuracy: variety of structures, error density, communicative impact.\n\n"
        "For each criterion give:\n"
        "- band (number, e.g. 6.5)\n"
        "- feedback in **Vietnamese**, 2-4 sentences, citing short phrases from the essay.\n\n"
        "OVERALL = average of the 4 bands, rounded to the nearest 0.5.\n\n"
        "REFERENCE BAND ANCHORS:\n"
        "- Band 9: native-like, sophisticated, fully developed; rare/no errors.\n"
        "- Band 7: clear position, well-organised, range of vocab/grammar with occasional errors.\n"
        "- Band 5: addresses task partially, limited vocab, frequent errors but meaning often clear.\n"
        "- Band 3: very limited content, hard to follow, basic structures only.\n\n"
        "Output JSON: {ta:{band,feedback}, cc:{band,feedback}, lr:{band,feedback}, "
        "gra:{band,feedback}, overall:number}"
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _round_half(x: float) -> float:
    """Round to nearest 0.5 step, clamp to [0, 9]."""
    if x is None:
        return 0.0
    rounded = round(float(x) * 2) / 2
    return max(0.0, min(9.0, rounded))


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort JSON extraction (Gemini sometimes wraps in code fences)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        # Try to locate the first {...} block
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


# ─── Main entry ───────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash"


async def grade_writing_essay(
    essay_text: str,
    task_type: TaskType,
    prompt_text: str,
    chart_description: Optional[str] = None,
    timeout_sec: int = 60,
) -> WritingGradeResult:
    """Call Gemini and return parsed band scores. Raises RuntimeError on
    misconfiguration or unrecoverable parsing failure."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError("google-genai SDK chưa được cài đặt.") from e

    client = genai.Client(api_key=api_key)
    user_prompt = _build_user_prompt(essay_text, task_type, prompt_text, chart_description)

    cfg = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=1500,
        response_mime_type="application/json",
        response_schema=WRITING_SCHEMA,
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=cfg,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"Gemini timed out after {timeout_sec}s") from e

    # Prefer structured `parsed`, fall back to text extraction
    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, dict):
        text_attr = getattr(response, "text", None)
        parsed = _extract_json(text_attr or "")
    if not isinstance(parsed, dict):
        raise RuntimeError("Không thể parse JSON từ Gemini response.")

    def _crit(key: str) -> tuple[float, str]:
        v = parsed.get(key) or {}
        return (
            _round_half(float(v.get("band", 0))),
            str(v.get("feedback", "")).strip(),
        )

    band_ta, fb_ta   = _crit("ta")
    band_cc, fb_cc   = _crit("cc")
    band_lr, fb_lr   = _crit("lr")
    band_gra, fb_gra = _crit("gra")

    overall_raw = parsed.get("overall")
    if isinstance(overall_raw, (int, float)):
        overall = _round_half(float(overall_raw))
    else:
        overall = _round_half((band_ta + band_cc + band_lr + band_gra) / 4)

    return WritingGradeResult(
        band_ta=band_ta,
        band_cc=band_cc,
        band_lr=band_lr,
        band_gra=band_gra,
        band_overall=overall,
        feedback={"ta": fb_ta, "cc": fb_cc, "lr": fb_lr, "gra": fb_gra},
        model=GEMINI_MODEL,
        grade_hash=compute_grade_hash(essay_text, prompt_text, task_type),
    )


# ─── Async grade-attempt orchestration ────────────────────────────────────────

async def grade_writing_for_attempt(attempt_id: int) -> None:
    """Background task: find all essay answers in `attempt_id` whose
    QuizQuestion is an IELTS writing task, grade each, write IeltsWritingGrade
    rows. Idempotent — uses grade_hash cache to skip prior calls."""
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models.quiz_attempt import QuizAttempt, QuizAnswer
    from app.db.models.quiz import QuizQuestion
    from app.db.models.writing_grade import IeltsWritingGrade

    async with AsyncSessionLocal() as db:
        # Eligible essay answers
        result = await db.execute(
            select(QuizAnswer, QuizQuestion)
            .join(QuizQuestion, QuizAnswer.question_id == QuizQuestion.id)
            .where(
                QuizAnswer.attempt_id == attempt_id,
                QuizQuestion.type == "essay",
            )
        )
        rows = result.all()

        for ans, qq in rows:
            essay_text = (ans.given_answer or "")
            if isinstance(essay_text, dict):
                essay_text = essay_text.get("text", "")
            essay_text = (str(essay_text) or "").strip()
            if not essay_text:
                continue

            # Determine task type from question metadata / text heuristics
            meta = qq.extra_metadata or {}
            instr = (meta.get("group_instruction") or "")
            section = (meta.get("ielts_section") or "")
            task_type: TaskType = "task_1" if (
                "task 1" in instr.lower() or "task 1" in section.lower()
            ) else "task_2"

            prompt_text = qq.question_text or ""
            grade_hash = compute_grade_hash(essay_text, prompt_text, task_type)

            # Skip if already graded for this attempt+question
            existing_attempt = await db.execute(
                select(IeltsWritingGrade).where(
                    IeltsWritingGrade.attempt_id == attempt_id,
                    IeltsWritingGrade.question_id == qq.id,
                    IeltsWritingGrade.status == "graded",
                )
            )
            if existing_attempt.scalars().first():
                continue

            # Cache hit: copy from any prior graded row with same hash
            cached_q = await db.execute(
                select(IeltsWritingGrade)
                .where(
                    IeltsWritingGrade.grade_hash == grade_hash,
                    IeltsWritingGrade.status == "graded",
                )
                .limit(1)
            )
            cached = cached_q.scalars().first()

            if cached is not None:
                row = IeltsWritingGrade(
                    attempt_id=attempt_id,
                    question_id=qq.id,
                    grade_hash=grade_hash,
                    task_type=task_type,
                    band_ta=cached.band_ta,
                    band_cc=cached.band_cc,
                    band_lr=cached.band_lr,
                    band_gra=cached.band_gra,
                    band_overall=cached.band_overall,
                    feedback_json=cached.feedback_json,
                    status="graded",
                    model=cached.model,
                )
                db.add(row)
                await db.commit()
                continue

            # Insert pending row, then call Gemini.
            pending = IeltsWritingGrade(
                attempt_id=attempt_id,
                question_id=qq.id,
                grade_hash=grade_hash,
                task_type=task_type,
                status="pending",
            )
            db.add(pending)
            await db.commit()
            await db.refresh(pending)

            try:
                result_obj = await grade_writing_essay(
                    essay_text=essay_text,
                    task_type=task_type,
                    prompt_text=prompt_text,
                )
                pending.band_ta      = result_obj.band_ta
                pending.band_cc      = result_obj.band_cc
                pending.band_lr      = result_obj.band_lr
                pending.band_gra     = result_obj.band_gra
                pending.band_overall = result_obj.band_overall
                pending.feedback_json = result_obj.feedback
                pending.model        = result_obj.model
                pending.status       = "graded"
                await db.commit()
            except Exception as e:
                logger.error(f"Writing grade failed (attempt={attempt_id}, q={qq.id}): {e}", exc_info=True)
                pending.status = "failed"
                pending.error_message = str(e)[:1000]
                await db.commit()
