from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import pytest

import app.services.local_ocr_service as local_ocr_service
from app.services.local_ocr_service import (
    assess_native_math_text,
    build_formula_audit,
)


BROKEN_NATIVE_MATH_TEXT = """
Câu 1
A =
\x12
1 −1
4
\x13
b) Cho (x −2)2026 + |y + 3|2025 = 0.
C = a2
bc + b2
ac + c2
ab
"""


def test_detects_corrupted_native_math_text_for_stem_docs():
    audit = assess_native_math_text(BROKEN_NATIVE_MATH_TEXT, "toan")

    assert audit["is_corrupted"] is True
    assert audit["control_char_count"] >= 2
    assert audit["flattened_power_count"] >= 2


def test_does_not_mark_same_text_corrupted_for_non_stem_docs():
    audit = assess_native_math_text(BROKEN_NATIVE_MATH_TEXT, "ngu-van")

    assert audit["is_corrupted"] is False


def test_formula_audit_requires_review_for_corrupted_non_marker_stem_output():
    audit = build_formula_audit(
        subject_code="toan",
        latex_quality={"is_math_broken": False},
        native_text_math_audit={"is_corrupted": True},
        ocr_method="pymupdf",
    )

    assert audit["review_required"] is True
    assert "native_text_math_corruption_detected" in audit["reasons"]
    assert "non_marker_stem_output=pymupdf" in audit["reasons"]


def test_formula_audit_allows_healthy_marker_output_to_remain_publishable():
    audit = build_formula_audit(
        subject_code="toan",
        latex_quality={"is_math_broken": False},
        native_text_math_audit={"is_corrupted": True},
        ocr_method="marker",
    )

    assert audit["review_required"] is False
    assert audit["native_text_math_corruption"] is True


def test_formula_audit_requires_review_when_latex_is_still_broken():
    audit = build_formula_audit(
        subject_code="toan",
        latex_quality={"is_math_broken": True},
        native_text_math_audit={"is_corrupted": False},
        ocr_method="marker",
    )

    assert audit["review_required"] is True
    assert "latex_quality_broken" in audit["reasons"]


# ── PaddleOCR-VL fallback cascade (thay thế các test marker-rescue cũ) ──
# Quality assessors được monkeypatch theo nội dung text ('$' = LaTeX tốt) để
# test tập trung vào logic cascade, không phụ thuộc ngưỡng thực của assessor.

def _fake_latex(text, subject_code):
    broken = "$" not in (text or "")
    return {
        "is_stem": True,
        "is_math_broken": broken,
        "latex_ratio": 0.0 if broken else 0.9,
        "plain_math_hint_count": 6 if broken else 0,
        "score": 0.3 if broken else 0.9,
        "reason": "fake",
    }


def _fake_quality(text, subject_code):
    return {"is_low_quality": False, "score": 0.85, "reason": "ok"}


def _patch_assessors(monkeypatch):
    monkeypatch.setattr(local_ocr_service, "assess_latex_quality", _fake_latex)
    monkeypatch.setattr(local_ocr_service, "assess_ocr_quality", _fake_quality)
    monkeypatch.setattr(local_ocr_service, "_read_native_pdf_text", lambda _p: "")
    monkeypatch.setattr(local_ocr_service, "_paddle_vl_available", lambda: True)


def _mk_pdf(tag: str) -> Path:
    p = Path("uploads") / f"cascade_{tag}_{uuid.uuid4().hex}.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4 mocked")
    return p


_PLAIN = (
    "Câu 1: Cho (x - 2)2026 + |y + 3|2025 = 0. Tính B = xy + yx. "
    "Nội dung chữ đủ dài vượt ngưỡng empty nhưng công thức bị làm phẳng (no LaTeX)."
)
_LATEX = (
    "Câu 1: Cho $(x-2)^{2026} + |y+3|^{2025} = 0$. Tính $B = x^y + y^x$.\n"
    "Câu 2: Tính $\\frac{a^2}{bc} + \\frac{b^2}{ac} + \\frac{c^2}{ab}$ — LaTeX đầy đủ, đủ dài."
)


def _ocr_out(text):
    return {"text": text, "page_count": 1, "image_map": {}, "blocks": [], "figures": [], "warnings": []}


@pytest.mark.asyncio
async def test_stem_math_broken_paddle_falls_back_to_mineru(monkeypatch):
    # Cascade đảo chiều: PaddleOCR-VL primary trả math bị làm phẳng (no LaTeX)
    # → MinerU fallback có LaTeX đầy đủ thắng theo quality-gate.
    pdf = _mk_pdf("mineru")
    try:
        _patch_assessors(monkeypatch)
        monkeypatch.setattr(local_ocr_service, "_run_paddle_vl_pipeline", AsyncMock(return_value=_ocr_out(_PLAIN)))
        mineru_mock = AsyncMock(return_value=_ocr_out(_LATEX))
        monkeypatch.setattr(local_ocr_service, "_run_mineru_pipeline", mineru_mock)

        artifact = await local_ocr_service.extract_local_ocr_artifact(str(pdf), "toan", use_cache=False)

        mineru_mock.assert_awaited_once()
        assert artifact["method"] == "mineru"
        assert "paddle-vl" in artifact["fallbacks_used"]
        assert "mineru" in artifact["fallbacks_used"]
        assert artifact["is_empty"] is False
        assert artifact["publishable"] is True
    finally:
        pdf.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_healthy_paddle_skips_mineru_fallback(monkeypatch):
    # PaddleOCR-VL primary trả LaTeX tốt → MinerU fallback KHÔNG chạy.
    pdf = _mk_pdf("paddle")
    try:
        _patch_assessors(monkeypatch)
        monkeypatch.setattr(local_ocr_service, "_run_paddle_vl_pipeline", AsyncMock(return_value=_ocr_out(_LATEX)))
        mineru_mock = AsyncMock()
        monkeypatch.setattr(local_ocr_service, "_run_mineru_pipeline", mineru_mock)

        artifact = await local_ocr_service.extract_local_ocr_artifact(str(pdf), "toan", use_cache=False)

        mineru_mock.assert_not_awaited()
        assert artifact["method"] == "paddle-vl"
        assert artifact["fallbacks_used"] == ["paddle-vl"]
    finally:
        pdf.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_paddle_crash_degrades_to_mineru_not_exception(monkeypatch):
    # PaddleOCR-VL primary crash → MinerU fallback cứu, không raise ra ngoài.
    pdf = _mk_pdf("crash")

    async def _boom(*a, **k):
        raise RuntimeError("PaddleOCR-VL not available — pip install paddleocr")

    try:
        _patch_assessors(monkeypatch)
        monkeypatch.setattr(local_ocr_service, "_run_paddle_vl_pipeline", _boom)
        monkeypatch.setattr(local_ocr_service, "_run_mineru_pipeline", AsyncMock(return_value=_ocr_out(_LATEX)))

        artifact = await local_ocr_service.extract_local_ocr_artifact(str(pdf), "toan", use_cache=False)

        assert artifact["method"] == "mineru"
        assert artifact["is_empty"] is False
        assert any("paddle_vl_failed" in w for w in artifact["warnings"])
    finally:
        pdf.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_engine_paddle_vl_forced_skips_mineru(monkeypatch):
    pdf = _mk_pdf("force_paddle")
    try:
        _patch_assessors(monkeypatch)
        mineru_mock = AsyncMock(return_value=_ocr_out(_LATEX))
        monkeypatch.setattr(local_ocr_service, "_run_mineru_pipeline", mineru_mock)
        monkeypatch.setattr(local_ocr_service, "_run_paddle_vl_pipeline", AsyncMock(return_value=_ocr_out(_LATEX)))

        artifact = await local_ocr_service.extract_local_ocr_artifact(
            str(pdf), "toan", use_cache=False, engine="paddle-vl"
        )

        mineru_mock.assert_not_awaited()  # ép paddle → MinerU không chạy
        assert artifact["method"] == "paddle-vl"
        assert artifact["fallbacks_used"] == ["paddle-vl"]
    finally:
        pdf.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_engine_mineru_forced_no_paddle_fallback(monkeypatch):
    pdf = _mk_pdf("force_mineru")
    try:
        _patch_assessors(monkeypatch)
        # MinerU trả math-broken (plain) → ở "auto" sẽ fallback, nhưng engine="mineru" thì KHÔNG.
        monkeypatch.setattr(local_ocr_service, "_run_mineru_pipeline", AsyncMock(return_value=_ocr_out(_PLAIN)))
        paddle_mock = AsyncMock(return_value=_ocr_out(_LATEX))
        monkeypatch.setattr(local_ocr_service, "_run_paddle_vl_pipeline", paddle_mock)

        artifact = await local_ocr_service.extract_local_ocr_artifact(
            str(pdf), "toan", use_cache=False, engine="mineru"
        )

        paddle_mock.assert_not_awaited()  # engine=mineru → không gọi fallback
        assert artifact["method"] == "mineru"
        assert artifact["fallbacks_used"] == ["mineru"]
    finally:
        pdf.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_both_engines_empty_returns_unpublishable_artifact(monkeypatch):
    pdf = _mk_pdf("empty")
    try:
        _patch_assessors(monkeypatch)
        monkeypatch.setattr(local_ocr_service, "_run_mineru_pipeline", AsyncMock(return_value=_ocr_out("")))
        monkeypatch.setattr(local_ocr_service, "_run_paddle_vl_pipeline", AsyncMock(return_value=_ocr_out("")))

        artifact = await local_ocr_service.extract_local_ocr_artifact(str(pdf), "toan", use_cache=False)

        assert artifact["is_empty"] is True
        assert artifact["publishable"] is False  # Phase 2.1: empty không bao giờ publishable
    finally:
        pdf.unlink(missing_ok=True)
