"""
Post-parse validation + normalization for AI-parsed questions.

Runs after Gemini extraction and answer mapping, before questions are written to
the bank. Goals:
  - clamp/null out-of-range grades (valid GDPT range 1..12),
  - coerce difficulty to the canonical {NB, TH, VD, VDC} set,
  - surface (but not delete) multiple-choice questions missing an answer.

Pure function — easy to unit test, no DB/IO. Returns the (mutated) list plus a
stats dict and a list of human-readable Vietnamese warnings to fold into the
exam's quality_report / ingest warnings.
"""
from typing import Any

VALID_DIFFICULTIES = {"NB", "TH", "VD", "VDC"}
DEFAULT_DIFFICULTY = "TH"
MULTIPLE_CHOICE_TYPES = {"TN", "TNKQ", "MC"}


def validate_and_normalize_questions(
    questions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Validate + normalize in place. Returns (questions, stats, warnings)."""
    stats = {
        "checked": len(questions),
        "grade_invalid": 0,
        "difficulty_normalized": 0,
        "empty_answer_mc": 0,
    }

    for q in questions:
        # ── grade ∈ 1..12, else null it out ──
        g = q.get("grade")
        if g is not None and g != "":
            try:
                gi = int(g)
            except (TypeError, ValueError):
                gi = None
            if gi is None or gi < 1 or gi > 12:
                q["grade"] = None
                stats["grade_invalid"] += 1
            else:
                q["grade"] = gi

        # ── difficulty ∈ {NB,TH,VD,VDC}, else default TH ──
        d = str(q.get("difficulty") or "").strip().upper()
        if d not in VALID_DIFFICULTIES:
            q["difficulty"] = DEFAULT_DIFFICULTY
            stats["difficulty_normalized"] += 1
        else:
            q["difficulty"] = d

        # ── multiple-choice question missing an answer (flag only) ──
        qtype = str(q.get("type") or q.get("question_type") or "").strip().upper()
        answer = str(q.get("answer") or "").strip()
        if qtype in MULTIPLE_CHOICE_TYPES and not answer:
            stats["empty_answer_mc"] += 1

    warnings: list[str] = []
    if stats["grade_invalid"]:
        warnings.append(f"{stats['grade_invalid']} câu có lớp không hợp lệ (đã bỏ trống).")
    if stats["difficulty_normalized"]:
        warnings.append(f"{stats['difficulty_normalized']} câu có mức độ lạ (đã đặt mặc định TH).")
    if stats["empty_answer_mc"]:
        warnings.append(f"{stats['empty_answer_mc']} câu trắc nghiệm chưa có đáp án.")

    return questions, stats, warnings
