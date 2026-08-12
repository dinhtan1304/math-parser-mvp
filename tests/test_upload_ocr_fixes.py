"""Unit tests cho các fix Upload & OCR → JSON (Phase 3 plan).

Pure-function tests (không cần DB / Gemini): segmentation, completeness estimate,
mock-cache heuristic, result_json wrapping, fallback picker.
"""
import json

import pytest


# ── Phase 3.1: segmentation không bỏ sót câu đánh số bằng động từ thường ──
def test_split_questions_relaxed_lowercase_numbered():
    from app.services.pipeline import _split_questions

    text = (
        "1. tính giá trị của biểu thức x + 1\n"
        "2) cho tam giác ABC vuông tại A\n"
        "3. giải phương trình bậc hai sau"
    )
    result = _split_questions(text)
    assert len(result) == 3, f"expected 3 questions, got {len(result)}: {result}"
    assert [q["cau_num"] for q in result] == [1, 2, 3]


def test_split_questions_uppercase_still_works():
    from app.services.pipeline import _split_questions

    text = "Câu 1. Tính A.\nCâu 2. Cho B."
    result = _split_questions(text)
    assert len(result) == 2


# ── Phase 3.7: completeness estimate đếm distinct, không gấp đôi đề+đáp án ──
def test_estimate_question_count_distinct():
    from app.services.ai_parser import _estimate_question_count

    text = (
        "Câu 1. Tính A.\nCâu 2. Tính B.\n"
        "HƯỚNG DẪN CHẤM\nCâu 1. Đáp án A.\nCâu 2. Đáp án B."
    )
    # 4 markers raw nhưng chỉ 2 câu distinct.
    assert _estimate_question_count(text) == 2


# ── khôi phục LaTeX bị JSON ăn backslash ──
def test_repair_latex_escapes():
    from app.services.ai_parser import _repair_latex_escapes

    # \frac → form-feed + 'rac' khi JSON decode single-backslash
    assert _repair_latex_escapes("$\x0crac{23}{45}$") == "$\\frac{23}{45}$"
    # \beta → backspace + 'eta'
    assert _repair_latex_escapes("$\x08eta$") == "$\\beta$"
    # \t (tab) chỉ khôi phục trong ngữ cảnh toán
    assert _repair_latex_escapes("$\times 2$") == "$\\times 2$"
    # văn bản thường có tab → KHÔNG đụng
    assert _repair_latex_escapes("cot 1\tcot 2") == "cot 1\tcot 2"
    # text thường không control char → giữ nguyên
    assert _repair_latex_escapes("đpcm") == "đpcm"


# ── đề + đáp án trùng cau_num → gộp thành 1 câu ──
def test_merge_duplicate_cau_num():
    from app.services.ai_parser import _merge_duplicate_cau_num

    qs = [
        # PHẦN A — đề (answer rỗng)
        {"question": "Câu 1. Tính 1+1.", "answer": "", "solution_steps": []},
        {"question": "Câu 2. Số nào lớn hơn? A.3 B.5", "answer": "", "solution_steps": []},
        # PHẦN B — đáp án (lặp lại số câu, có answer + lời giải)
        {"question": "Câu 1. Ta có 1+1=2.", "answer": "2", "solution_steps": ["1+1=2"]},
        {"question": "Câu 2. So sánh. Chọn B.", "answer": "B", "solution_steps": ["so sánh"]},
    ]
    out = _merge_duplicate_cau_num(qs)
    assert len(out) == 2, f"expected 2 merged, got {len(out)}"
    by_q = {o["question"][:6]: o for o in out}
    assert by_q["Câu 1."]["answer"] == "2"          # answer lấy từ phần đáp án
    assert by_q["Câu 1."]["solution_steps"] == ["1+1=2"]
    assert by_q["Câu 2."]["answer"] == "B"
    # question giữ bản đề (lần đầu) — không bị thay bằng text lời giải
    assert "Tính 1+1" in by_q["Câu 1."]["question"]


# ── Phase 3.6: _is_mock_result chặt hơn + guard mẫu nhỏ ──
def test_is_mock_result_thresholds():
    from app.api.parser import _is_mock_result

    good = [
        {"topic": "Đại số", "grade": 9, "chapter": "Chương 1", "solution_steps": ["b1"]}
        for _ in range(5)
    ]
    mock = [
        {"topic": "Toán học", "grade": None, "chapter": "", "solution_steps": []}
        for _ in range(5)
    ]
    assert _is_mock_result([]) is True            # rỗng
    assert _is_mock_result(good[:2]) is True       # <3 câu → không đủ tin cậy
    assert _is_mock_result(good) is False          # data tốt
    assert _is_mock_result(mock) is True           # mock rõ ràng


# ── Phase 3.3 + 3.13: merge_ingest_metadata wrap + schema_version ──
def test_merge_ingest_metadata_wraps_with_version():
    from app.services.hybrid_ingest import (
        merge_ingest_metadata,
        extract_ingest_metadata,
        extract_questions_payload,
    )

    js = merge_ingest_metadata(
        [{"question": "x"}],
        warnings=["w1", "w1"],  # dedup
        ingest_stats={"questions_saved": 1},
        payload_type="k12",
    )
    d = json.loads(js)
    assert d["schema_version"] == 1
    assert d["type"] == "k12"
    assert extract_questions_payload(js) == [{"question": "x"}]
    warnings, stats = extract_ingest_metadata(js)
    assert warnings == ["w1"]
    assert stats == {"questions_saved": 1}


# ── Phase 1.3: _fallback_beats_primary picker logic ──
def test_fallback_beats_primary_logic():
    from app.services.local_ocr_service import _fallback_beats_primary

    long_a = "Câu 1 " + "a" * 200
    long_b = "Câu 1 " + "b" * 200
    broken = {"is_math_broken": True, "latex_ratio": 0.0}
    good = {"is_math_broken": False, "latex_ratio": 0.9}
    q_lo = {"score": 0.4}
    q_hi = {"score": 0.9}

    # fallback rỗng → không bao giờ thay
    assert _fallback_beats_primary({"text": long_a}, {"text": ""}, q_hi, q_hi, good, good, True) is False
    # primary rỗng/None → lấy fallback non-empty
    assert _fallback_beats_primary(None, {"text": long_b}, q_lo, q_hi, broken, good, True) is True
    # STEM: primary math broken, fallback không broken → thay
    assert _fallback_beats_primary({"text": long_a}, {"text": long_b}, q_hi, q_hi, broken, good, True) is True
    # non-STEM: fallback quality cao hơn rõ → thay
    assert _fallback_beats_primary({"text": long_a}, {"text": long_b}, q_lo, q_hi, good, good, False) is True
    # non-STEM: quality ngang → giữ primary
    assert _fallback_beats_primary({"text": long_a}, {"text": long_b}, q_hi, q_hi, good, good, False) is False
