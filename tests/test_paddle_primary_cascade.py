"""PaddleOCR-VL-1.6 primary + MinerU fallback cascade (2026-06-08).

Covers the inverted OCR cascade in :mod:`app.services.local_ocr_service`:
PaddleOCR-VL runs first in ``engine="auto"``; MinerU is only invoked as a
quality-gated fallback. Also covers the new Paddle layout → review-block shape
conversion and pixel-bbox normalization. Pure / mocked — no GPU, no Gemini, no DB.
"""
import pytest

from app.services import local_ocr_service as los
from app.benchmark.engines import paddle_engine as pe


# ── Pixel bbox → [0,1] normalization (FE overlay convention) ────────────────
def test_normalize_bbox_to_unit_scales_by_page_dims():
    # 200×100 page, box covering left-half top-half → [0,0,0.5,0.5]
    assert pe._normalize_bbox_to_unit([0, 0, 100, 50], 200, 100) == [0.0, 0.0, 0.5, 0.5]


def test_normalize_bbox_passthrough_already_normalized():
    assert pe._normalize_bbox_to_unit([0.1, 0.2, 0.3, 0.4], 0, 0) == [0.1, 0.2, 0.3, 0.4]


def test_normalize_bbox_rejects_degenerate_and_missing():
    assert pe._normalize_bbox_to_unit([10, 10, 10, 10], 100, 100) is None   # zero-area
    assert pe._normalize_bbox_to_unit(None, 100, 100) is None
    assert pe._normalize_bbox_to_unit([5, 5, 9], 100, 100) is None          # too short
    # pixel bbox but no page dims → can't normalize
    assert pe._normalize_bbox_to_unit([10, 10, 90, 90], 0, 0) is None


# ── Paddle layout blocks → MinerU-style review blocks ───────────────────────
def test_paddle_blocks_to_review_shape_maps_labels_and_pages():
    layout = [
        {"page_index": 0, "label": "text", "content": "Câu 1", "bbox": [0.1, 0.1, 0.5, 0.2], "order": 1},
        {"page_index": 0, "label": "formula", "content": "$x^2$", "bbox": [0.1, 0.3, 0.4, 0.4], "order": 2},
        {"page_index": 1, "label": "table", "content": "...", "bbox": None, "order": 1},
        {"page_index": 1, "label": "chart", "content": "", "bbox": [0.0, 0.0, 1.0, 1.0], "order": 2},
    ]
    blocks = los._paddle_blocks_to_review_shape(layout)
    assert [b["kind"] for b in blocks] == ["text", "equation", "table", "figure"]
    # page_index 0-based → page_num 1-based
    assert [b["page_num"] for b in blocks] == [1, 1, 2, 2]
    assert all(b["source"] == "paddle-vl" for b in blocks)
    assert blocks[0]["block_id"] == "p1_paddlevl_0"
    assert blocks[0]["bbox"] == [0.1, 0.1, 0.5, 0.2]


def test_paddle_blocks_empty_input():
    assert los._paddle_blocks_to_review_shape([]) == []
    assert los._paddle_blocks_to_review_shape(None) == []


# ── version ladder: request newest, degrade to newest runnable ─────────────
def test_candidate_versions_ladder_descends_from_requested():
    assert pe._candidate_versions("v1.6") == ["v1.6", "v1.5", "v1"]
    assert pe._candidate_versions("v1.5") == ["v1.5", "v1"]
    assert pe._candidate_versions("v1") == ["v1"]
    # unknown / empty → full ladder newest-first
    assert pe._candidate_versions("") == ["v1.6", "v1.5", "v1"]
    assert pe._candidate_versions("bogus") == ["v1.6", "v1.5", "v1"]


# ── Cascade: engine="auto" runs PaddleOCR-VL first ──────────────────────────
def _good(text, src="paddle"):
    return {"text": f"Câu 1. {text} " + "x" * 200, "blocks": [], "figures": [], "page_count": 1, "warnings": []}


@pytest.mark.asyncio
async def test_auto_cascade_prefers_paddle_and_skips_mineru_when_good(monkeypatch):
    calls: list[str] = []

    async def fake_paddle(fp, fh, budget_seconds=None):
        calls.append("paddle-vl")
        return _good("paddle result")

    async def fake_mineru(fp, fh, budget_seconds=None):
        calls.append("mineru")
        return _good("mineru result")

    monkeypatch.setattr(los, "_paddle_vl_available", lambda: True)
    monkeypatch.setattr(los, "_run_paddle_vl_pipeline", fake_paddle)
    monkeypatch.setattr(los, "_run_mineru_pipeline", fake_mineru)

    warnings: list[str] = []
    chosen, method, _q, _l, methods = await los._run_ocr_cascade(
        "paddle-vl", "mineru", file_path="x.pdf", file_hash="h",
        subject_code="toan", is_stem=True, warnings=warnings, quality_gated=True,
    )
    assert calls == ["paddle-vl"]          # MinerU never invoked
    assert method == "paddle-vl"
    assert methods == ["paddle-vl"]
    assert "paddle result" in chosen["text"]


@pytest.mark.asyncio
async def test_auto_cascade_falls_back_to_mineru_when_paddle_empty(monkeypatch):
    calls: list[str] = []

    async def fake_paddle(fp, fh, budget_seconds=None):
        calls.append("paddle-vl")
        return {"text": "", "blocks": [], "figures": [], "page_count": 0, "warnings": []}

    async def fake_mineru(fp, fh, budget_seconds=None):
        calls.append("mineru")
        return _good("mineru rescue")

    monkeypatch.setattr(los, "_paddle_vl_available", lambda: True)
    monkeypatch.setattr(los, "_run_paddle_vl_pipeline", fake_paddle)
    monkeypatch.setattr(los, "_run_mineru_pipeline", fake_mineru)

    chosen, method, _q, _l, methods = await los._run_ocr_cascade(
        "paddle-vl", "mineru", file_path="x.pdf", file_hash="h",
        subject_code="toan", is_stem=True, warnings=[], quality_gated=True,
    )
    assert calls == ["paddle-vl", "mineru"]
    assert method == "mineru"
    assert "mineru rescue" in chosen["text"]


@pytest.mark.asyncio
async def test_forced_mineru_never_calls_paddle(monkeypatch):
    calls: list[str] = []

    async def fake_paddle(fp, fh, budget_seconds=None):
        calls.append("paddle-vl")
        return _good("paddle")

    async def fake_mineru(fp, fh, budget_seconds=None):
        calls.append("mineru")
        return _good("mineru")

    monkeypatch.setattr(los, "_paddle_vl_available", lambda: True)
    monkeypatch.setattr(los, "_run_paddle_vl_pipeline", fake_paddle)
    monkeypatch.setattr(los, "_run_mineru_pipeline", fake_mineru)

    _chosen, method, _q, _l, methods = await los._run_ocr_cascade(
        "mineru", None, file_path="x.pdf", file_hash="h",
        subject_code="toan", is_stem=True, warnings=[], quality_gated=False,
    )
    assert calls == ["mineru"]
    assert method == "mineru"
    assert methods == ["mineru"]
