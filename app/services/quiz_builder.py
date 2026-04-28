"""
Quiz Builder — converts bank questions into structured quiz questions.

Handles:
  - Parsing choices from bank question_text (A./B./C./D. pattern)
  - Mapping legacy bank types → quiz types
  - Restructuring flat answer/solution_steps → JSONB formats
  - AI-powered type conversion via QuizAIConverter (when target_type is set)
"""

import asyncio
import re
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_IELTS_STRUCTURED_TYPES = {
    "true_false_not_given", "yes_no_not_given",
    "matching", "matching_headings",
}

# Regex: match lines starting with A. / B) / A) / A: etc.
_CHOICE_PATTERN = re.compile(
    r"^([A-Ha-h])\s*[.):\]]\s*(.+)",
    re.MULTILINE,
)


def parse_choices_from_text(question_text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Extract choices (A/B/C/D) from question_text.

    Returns:
        (main_text, choices) where choices = [{"key": "A", "text": "...", "is_correct": False, "media": None}]
        If no choices found, returns (original_text, []).
    """
    lines = question_text.split("\n")
    main_lines: List[str] = []
    choices: List[Dict[str, Any]] = []
    in_choices = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not in_choices:
                main_lines.append(line)
            continue

        match = _CHOICE_PATTERN.match(stripped)
        if match:
            in_choices = True
            key = match.group(1).upper()
            text = match.group(2).strip()
            choices.append({
                "key": key,
                "text": text,
                "is_correct": False,
                "media": None,
            })
        else:
            if in_choices and choices:
                # Continuation line of the last choice
                choices[-1]["text"] += " " + stripped
            else:
                main_lines.append(line)

    # Only return as choices if we found at least 2
    if len(choices) < 2:
        return question_text, []

    main_text = "\n".join(main_lines).strip()
    return main_text, choices


def mark_correct_choice(choices: List[Dict[str, Any]], answer: Optional[str]) -> None:
    """Mark the correct choice based on bank answer field."""
    if not answer or not choices:
        return

    answer_clean = answer.strip().upper()

    # Try matching by key letter (e.g. "B", "C")
    for c in choices:
        if c["key"] == answer_clean:
            c["is_correct"] = True
            return

    # Try matching first letter if answer is like "B. Tokyo"
    if len(answer_clean) >= 1 and answer_clean[0].isalpha():
        first = answer_clean[0]
        for c in choices:
            if c["key"] == first:
                c["is_correct"] = True
                return

    # Try matching by text content
    for c in choices:
        if answer.strip().lower() in c["text"].lower() or c["text"].lower() in answer.strip().lower():
            c["is_correct"] = True
            return

    # Fallback: mark first choice
    logger.warning(f"Could not match answer '{answer}' to any choice, marking first as correct")
    if choices:
        choices[0]["is_correct"] = True


def parse_solution_steps(steps_raw: Optional[str]) -> List[str]:
    """Parse solution_steps from bank format (JSON string or plain text) → List[str]."""
    if not steps_raw:
        return []
    if isinstance(steps_raw, list):
        return [str(s) for s in steps_raw]
    try:
        parsed = json.loads(steps_raw)
        if isinstance(parsed, list):
            return [str(s) for s in parsed]
    except (json.JSONDecodeError, ValueError):
        pass
    return [s.strip() for s in steps_raw.split("\n") if s.strip()]


def _empty_choices(count: int = 4) -> List[Dict[str, Any]]:
    """Create empty placeholder choices."""
    keys = "ABCDEFGH"
    return [
        {"key": keys[i], "text": "", "is_correct": False, "media": None}
        for i in range(min(count, len(keys)))
    ]


async def convert_bank_question(
    bank_q,
    source_type: str = "bank_import",
    order: int = 0,
    target_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Convert a bank Question ORM object → dict suitable for creating QuizQuestion.

    If target_type is None: auto-detect (multiple_choice or fill_blank).
    If target_type is set: force that type, using LLM to generate missing data.
    """
    # Parse extra_data (IELTS bank questions)
    extra: Dict[str, Any] = {}
    if getattr(bank_q, "extra_data", None):
        try:
            extra = json.loads(bank_q.extra_data)
        except Exception:
            pass

    q_type = bank_q.question_type or ""

    # ── IELTS structured types (matching, true_false_not_given, etc.) ──────────
    if q_type in _IELTS_STRUCTURED_TYPES and extra:
        stored_answer = bank_q.answer or ""
        try:
            parsed_answer = json.loads(stored_answer)
        except Exception:
            parsed_answer = stored_answer

        difficulty_map = {"NB": "easy", "TH": "medium", "VD": "hard", "VDC": "expert"}
        return {
            "origin_question_id": bank_q.id,
            "source_type": source_type,
            "order": order,
            "question_text": bank_q.question_text or "",
            "difficulty": difficulty_map.get(bank_q.difficulty, bank_q.difficulty),
            "subject_code": bank_q.subject_code,
            "tags": [],
            "solution": None,
            "points": 1.0,
            "type": q_type,
            "choices": extra.get("choices") or None,
            "items": extra.get("items") or None,
            "answer": parsed_answer,
            "has_correct_answer": True,
            "scoring": {"mode": "all_or_nothing", "word_limit": extra.get("word_limit")},
            "metadata": {
                "global_number":     extra.get("global_number"),
                "group_instruction": extra.get("group_instruction", ""),
                "ielts_section":     bank_q.chapter,
                "passage_text":      extra.get("passage_text", ""),
            },
        }

    # ── IELTS fill_blank (has extra_data with word_limit/passage) ─────────────
    if q_type == "fill_blank" and extra:
        stored_answer = bank_q.answer or ""
        try:
            parsed_answer = json.loads(stored_answer)
        except Exception:
            parsed_answer = {"B1": stored_answer}
        difficulty_map = {"NB": "easy", "TH": "medium", "VD": "hard", "VDC": "expert"}
        return {
            "origin_question_id": bank_q.id,
            "source_type": source_type,
            "order": order,
            "question_text": bank_q.question_text or "",
            "difficulty": difficulty_map.get(bank_q.difficulty, bank_q.difficulty),
            "subject_code": bank_q.subject_code,
            "tags": [],
            "solution": None,
            "points": 1.0,
            "type": "fill_blank",
            "answer": parsed_answer if isinstance(parsed_answer, dict) else {"B1": str(parsed_answer)},
            "has_correct_answer": True,
            "scoring": {"mode": "all_or_nothing", "word_limit": extra.get("word_limit")},
            "metadata": {
                "group_instruction": extra.get("group_instruction", ""),
                "ielts_section":     bank_q.chapter,
                "passage_text":      extra.get("passage_text", ""),
            },
        }

    # Parse choices from question_text
    main_text, choices = parse_choices_from_text(bank_q.question_text or "")

    # Parse solution steps
    steps = parse_solution_steps(bank_q.solution_steps)
    solution = None
    if steps:
        solution = {"steps": steps, "explanation": None}

    # Map difficulty
    difficulty_map = {"NB": "easy", "TH": "medium", "VD": "hard", "VDC": "expert"}
    difficulty = difficulty_map.get(bank_q.difficulty, bank_q.difficulty)

    base: Dict[str, Any] = {
        "origin_question_id": bank_q.id,
        "source_type": source_type,
        "order": order,
        "question_text": main_text,
        "difficulty": difficulty,
        "subject_code": bank_q.subject_code,
        "tags": [],
        "solution": solution,
        "points": 1.0,
        "has_correct_answer": True,
    }

    # ─── Auto-detect (no target_type) ─────────────────────────
    if target_type is None:
        if choices:
            mark_correct_choice(choices, bank_q.answer)
            answer_key = next((c["key"] for c in choices if c["is_correct"]), None)
            base.update({"type": "multiple_choice", "choices": choices, "answer": answer_key})
        else:
            base.update({"type": "fill_blank", "answer": {"B1": bank_q.answer or ""}})
        return base

    # ─── Forced type with LLM ─────────────────────────────────
    from app.services.quiz_ai_converter import quiz_ai_converter

    if target_type == "multiple_choice":
        if choices:
            mark_correct_choice(choices, bank_q.answer)
            answer_key = next((c["key"] for c in choices if c["is_correct"]), None)
            base.update({"type": "multiple_choice", "choices": choices, "answer": answer_key})
        else:
            # No choices in bank → LLM generates distractors
            ai_choices = await quiz_ai_converter.generate_choices(
                main_text, bank_q.answer or "", count=3,
            )
            if ai_choices:
                all_choices = [{"key": "A", "text": bank_q.answer or "", "is_correct": True, "media": None}]
                all_choices.extend(ai_choices)
                base.update({"type": "multiple_choice", "choices": all_choices, "answer": "A"})
            else:
                # Fallback: empty choices
                base.update({"type": "multiple_choice", "choices": _empty_choices(), "answer": None})

    elif target_type == "checkbox":
        ai_data = await quiz_ai_converter.generate_checkbox_data(main_text, bank_q.answer)
        if ai_data and ai_data.get("choices"):
            base.update({
                "type": "checkbox",
                "choices": ai_data["choices"],
                "answer": ai_data.get("correct_keys", []),
            })
        else:
            # Fallback
            base.update({"type": "checkbox", "choices": _empty_choices(), "answer": []})

    elif target_type == "fill_blank":
        base.update({"type": "fill_blank", "answer": {"B1": bank_q.answer or ""}})

    elif target_type == "true_false":
        ai_data = await quiz_ai_converter.generate_true_false(main_text, bank_q.answer)
        if ai_data:
            base["question_text"] = ai_data["statement"]
            base.update({"type": "true_false", "answer": ai_data["answer"]})
        else:
            # Fallback: default True
            base.update({"type": "true_false", "answer": True})

    elif target_type == "reorder":
        ai_data = await quiz_ai_converter.generate_reorder_items(main_text, bank_q.answer)
        if ai_data:
            if ai_data.get("question"):
                base["question_text"] = ai_data["question"]
            base.update({
                "type": "reorder",
                "items": ai_data.get("items", []),
                "answer": ai_data.get("correct_order", []),
            })
        else:
            # Fallback: empty items
            base.update({"type": "reorder", "items": [], "answer": []})

    elif target_type == "essay":
        base.update({"type": "essay", "answer": None, "has_correct_answer": False})

    else:
        # Unknown type → auto-detect
        if choices:
            mark_correct_choice(choices, bank_q.answer)
            answer_key = next((c["key"] for c in choices if c["is_correct"]), None)
            base.update({"type": "multiple_choice", "choices": choices, "answer": answer_key})
        else:
            base.update({"type": "fill_blank", "answer": {"B1": bank_q.answer or ""}})

    return base


_CONVERT_BATCH_SIZE = 10


async def convert_bank_questions(
    bank_questions: list,
    source_type: str = "bank_import",
    start_order: int = 0,
    target_type: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Convert multiple bank questions in batches to avoid memory spikes.

    Returns:
        (converted_list, errors) where errors = [(question_id, error_message), ...]
    """
    results: List[Dict[str, Any]] = []
    errors: List[Tuple[int, str]] = []
    for chunk_start in range(0, len(bank_questions), _CONVERT_BATCH_SIZE):
        chunk = bank_questions[chunk_start:chunk_start + _CONVERT_BATCH_SIZE]
        tasks = [
            convert_bank_question(
                bq, source_type=source_type,
                order=start_order + chunk_start + i,
                target_type=target_type,
            )
            for i, bq in enumerate(chunk)
        ]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, res in enumerate(batch_results):
            if isinstance(res, Exception):
                bq = chunk[i]
                errors.append((bq.id, str(res)))
                logger.warning(f"convert_bank_question failed for Q#{bq.id}: {res}")
            else:
                results.append(res)
    return results, errors
