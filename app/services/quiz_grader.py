"""
Quiz Grader — scores each question type.

Supports: multiple_choice, checkbox, fill_blank, reorder, true_false, essay.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def grade_question(
    question_type: str,
    correct_answer: Any,
    given_answer: Any,
    points: float = 1.0,
    scoring: Optional[Dict[str, Any]] = None,
    choices: Optional[List[Dict[str, Any]]] = None,
    items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Grade a single quiz question.

    Returns:
        {"is_correct": bool, "points_earned": float, "detail": str}
    """
    if given_answer is None:
        return {"is_correct": False, "points_earned": 0.0, "detail": "No answer given"}

    scoring = scoring or {}

    graders = {
        "multiple_choice":       _grade_multiple_choice,
        "checkbox":              _grade_checkbox,
        "fill_blank":            _grade_fill_blank,
        "reorder":               _grade_reorder,
        "true_false":            _grade_true_false,
        "essay":                 _grade_essay,
        # IELTS types
        "true_false_not_given":  _grade_not_given_type,
        "yes_no_not_given":      _grade_not_given_type,
        "matching":              _grade_matching,
        "matching_headings":     _grade_matching,
    }

    grader = graders.get(question_type)
    if not grader:
        logger.warning(f"Unknown question type: {question_type}")
        return {"is_correct": False, "points_earned": 0.0, "detail": f"Unknown type: {question_type}"}

    return grader(correct_answer, given_answer, points, scoring, choices, items)


def _grade_multiple_choice(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Single correct answer: compare string keys."""
    correct_key = str(correct).strip().upper() if correct else ""
    given_key = str(given).strip().upper() if given else ""

    is_correct = correct_key == given_key
    return {
        "is_correct": is_correct,
        "points_earned": points if is_correct else 0.0,
        "detail": f"Expected {correct_key}, got {given_key}",
    }


def _grade_checkbox(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Multiple correct answers: compare sets of keys."""
    if not isinstance(correct, list):
        correct = [correct] if correct else []
    if not isinstance(given, list):
        given = [given] if given else []

    correct_set = {str(k).strip().upper() for k in correct}
    given_set = {str(k).strip().upper() for k in given}

    mode = scoring.get("mode", "all_or_nothing")

    if mode == "all_or_nothing":
        is_correct = correct_set == given_set
        earned = points if is_correct else 0.0
    else:
        # Partial credit
        if not correct_set:
            is_correct = len(given_set) == 0
            earned = points if is_correct else 0.0
        else:
            correct_chosen = correct_set & given_set
            wrong_chosen = given_set - correct_set

            penalty = scoring.get("penalty_wrong_choice", 0)
            per_correct = points / len(correct_set)

            earned = len(correct_chosen) * per_correct - len(wrong_chosen) * penalty
            earned = max(0.0, min(points, earned))
            is_correct = correct_set == given_set

    return {
        "is_correct": is_correct,
        "points_earned": round(earned, 2),
        "detail": f"Expected {sorted(correct_set)}, got {sorted(given_set)}",
    }


def _grade_fill_blank(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Fill in blanks: check each blank key."""
    if not isinstance(correct, dict) or not isinstance(given, dict):
        is_eq = str(correct).strip().lower() == str(given).strip().lower()
        return {
            "is_correct": is_eq,
            "points_earned": points if is_eq else 0.0,
            "detail": f"Expected {correct}, got {given}",
        }

    mode = scoring.get("mode", "per_blank")
    total_blanks = len(correct)
    if total_blanks == 0:
        return {"is_correct": True, "points_earned": points, "detail": "No blanks"}

    correct_count = 0
    details = []

    for blank_key, expected in correct.items():
        student_val = given.get(blank_key, "")
        if isinstance(student_val, str):
            student_val = student_val.strip()

        if isinstance(expected, dict):
            # Structured blank: {accept: [...], match_mode: ..., case_sensitive: ...}
            accept_list = expected.get("accept", [])
            case_sensitive = expected.get("case_sensitive", False)
            trim = expected.get("trim_whitespace", True)

            if trim and isinstance(student_val, str):
                student_val = student_val.strip()

            matched = False
            for acceptable in accept_list:
                a = str(acceptable)
                s = str(student_val)
                if not case_sensitive:
                    a = a.lower()
                    s = s.lower()
                if trim:
                    a = a.strip()
                    s = s.strip()
                if a == s:
                    matched = True
                    break

            if matched:
                correct_count += 1
                details.append(f"{blank_key}: correct")
            else:
                details.append(f"{blank_key}: expected one of {accept_list}, got '{student_val}'")
        else:
            # Simple string comparison
            exp = str(expected).strip().lower()
            stu = str(student_val).strip().lower()
            if exp == stu:
                correct_count += 1
                details.append(f"{blank_key}: correct")
            else:
                details.append(f"{blank_key}: expected '{expected}', got '{student_val}'")

    is_all_correct = correct_count == total_blanks

    if mode == "per_blank":
        ppb = scoring.get("points_per_blank", points / total_blanks)
        earned = correct_count * ppb
    elif mode == "all_or_nothing":
        earned = points if is_all_correct else 0.0
    else:
        earned = (correct_count / total_blanks) * points

    return {
        "is_correct": is_all_correct,
        "points_earned": round(earned, 2),
        "detail": "; ".join(details),
    }


def _grade_reorder(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Reorder: compare ordered lists of item IDs."""
    if not isinstance(correct, list) or not isinstance(given, list):
        return {"is_correct": False, "points_earned": 0.0, "detail": "Invalid format"}

    correct_list = [str(x) for x in correct]
    given_list = [str(x) for x in given]

    mode = scoring.get("mode", "all_or_nothing")

    if mode == "all_or_nothing" or not scoring.get("partial_credit", False):
        is_correct = correct_list == given_list
        return {
            "is_correct": is_correct,
            "points_earned": points if is_correct else 0.0,
            "detail": f"Expected {correct_list}, got {given_list}",
        }

    # Partial credit: count items in correct position
    total = len(correct_list)
    if total == 0:
        return {"is_correct": True, "points_earned": points, "detail": "Empty list"}

    correct_positions = sum(
        1 for i, item_id in enumerate(given_list)
        if i < total and item_id == correct_list[i]
    )

    is_correct = correct_positions == total
    earned = (correct_positions / total) * points

    return {
        "is_correct": is_correct,
        "points_earned": round(earned, 2),
        "detail": f"{correct_positions}/{total} correct positions",
    }


def _to_bool(val: Any) -> bool:
    """Convert various representations to bool, handling string 'false' correctly."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "đúng")
    if isinstance(val, (int, float)):
        return val != 0
    return bool(val)


def _grade_true_false(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """True/False: compare boolean values."""
    correct_bool = _to_bool(correct) if correct is not None else True
    given_bool = _to_bool(given) if given is not None else False

    is_correct = correct_bool == given_bool
    return {
        "is_correct": is_correct,
        "points_earned": points if is_correct else 0.0,
        "detail": f"Expected {correct_bool}, got {given_bool}",
    }


def _grade_essay(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Essay: no auto-grading, return pending."""
    return {
        "is_correct": None,
        "points_earned": 0.0,
        "detail": "Essay — requires manual grading",
    }


# ── IELTS graders ─────────────────────────────────────────────────────────────

_NOT_GIVEN_ALIASES: dict[str, str] = {
    "NG": "NOT GIVEN", "N/G": "NOT GIVEN", "NOTGIVEN": "NOT GIVEN",
    "T": "TRUE", "F": "FALSE",
    "Y": "YES", "N": "NO",
}


def _normalize_ng(val: Any) -> str:
    """Normalize TRUE/FALSE/NOT GIVEN and YES/NO/NOT GIVEN values."""
    s = str(val).strip().upper().replace(" ", "")
    return _NOT_GIVEN_ALIASES.get(s, str(val).strip().upper())


def _grade_not_given_type(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Grade true_false_not_given and yes_no_not_given questions."""
    c = _normalize_ng(correct)
    g = _normalize_ng(given or "")
    ok = c == g
    return {
        "is_correct": ok,
        "points_earned": points if ok else 0.0,
        "detail": f"Expected {c}, got {g}",
    }


def _grade_matching(
    correct: Any, given: Any, points: float,
    scoring: dict, choices: Any, items: Any,
) -> Dict[str, Any]:
    """Grade matching and matching_headings questions.

    Supports all_or_nothing (default) and per_blank partial credit.
    """
    if not isinstance(correct, dict):
        return {"is_correct": False, "points_earned": 0.0, "detail": "invalid_correct_format"}
    if not isinstance(given, dict):
        return {"is_correct": False, "points_earned": 0.0, "detail": "no_answer_provided"}

    total = len(correct)
    if total == 0:
        return {"is_correct": True, "points_earned": points, "detail": "empty"}

    correct_count = sum(
        1 for k, v in correct.items()
        if str(given.get(k, "")).strip().upper() == str(v).strip().upper()
    )

    mode = (scoring or {}).get("mode", "all_or_nothing")
    if mode == "per_blank":
        earned = round(correct_count / total * points, 2)
        is_correct = correct_count == total
    else:
        is_correct = correct_count == total
        earned = points if is_correct else 0.0

    return {
        "is_correct": is_correct,
        "points_earned": earned,
        "detail": f"{correct_count}/{total} correct",
    }
