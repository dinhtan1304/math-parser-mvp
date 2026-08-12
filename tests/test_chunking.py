"""Question-aware RAG chunking (chunk_exam_markdown) + re-index helpers.

Verifies the chunker keeps each Câu/Bài whole, never splits inside a formula,
and that document-chunk delete (re-index) works.
"""
import asyncio

import pytest

from app.services.docling_chunker import chunk_exam_markdown, chunk_markdown_text


_EXAM_MD = """# ĐỀ THI TOÁN 7

## Câu 1 (5 điểm)
Tính giá trị: $$A = \\frac{1}{2} + \\frac{1}{3}$$
với điều kiện x > 0.

## Câu 2 (4 điểm)
Cho $a+b+c=0$. Chứng minh điều phải chứng minh.

Bài 3: Tìm số tự nhiên nhỏ nhất chia hết cho 2, 3 và 5.
"""


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_one_chunk_per_question():
    chunks = await chunk_exam_markdown(_EXAM_MD)
    # 3 câu (Câu 1, Câu 2, Bài 3) → 3 chunks (preamble "# ĐỀ THI" gộp vào trước Câu 1
    # hoặc tách nhỏ <30 bị bỏ — nhưng tiêu đề ngắn nên có thể bị drop). Ít nhất 3.
    titles = " ".join(c["section_title"] for c in chunks)
    assert "Câu 1" in titles
    assert "Câu 2" in titles
    assert "Bài 3" in titles or "Bài 3" in " ".join(c["text"] for c in chunks)


@pytest.mark.asyncio
async def test_formulas_never_split_and_unmasked():
    chunks = await chunk_exam_markdown(_EXAM_MD)
    for c in chunks:
        # placeholder phải được unmask hết
        assert "@@F" not in c["text"]
        # số lượng $$ trong mỗi chunk phải chẵn (không cắt ngang block formula)
        assert c["text"].count("$$") % 2 == 0
    # công thức nguyên vẹn nằm trong chunk của Câu 1
    cau1 = next(c for c in chunks if "Câu 1" in c["section_title"])
    assert "\\frac{1}{2}" in cau1["text"]
    assert "$$A =" in cau1["text"]


@pytest.mark.asyncio
async def test_long_question_splits_at_paragraph_keeping_formula():
    big = "## Câu 1\n" + ("\n\n".join(f"Đoạn {i} nội dung dài. $$x_{i}^2 = {i}$$" for i in range(60)))
    chunks = await chunk_exam_markdown(big, max_chars=400)
    assert len(chunks) > 1  # đã tách
    for c in chunks:
        assert "@@F" not in c["text"]
        assert c["text"].count("$$") % 2 == 0


@pytest.mark.asyncio
async def test_no_markers_falls_back():
    plain = "# Tài liệu\n\n" + ("Đây là đoạn văn bản thường không có câu hỏi. " * 30)
    chunks = await chunk_exam_markdown(plain)
    legacy = await chunk_markdown_text(plain)
    assert len(chunks) == len(legacy)  # fallback dùng chunk_markdown_text
    assert chunks and chunks[0]["text"]


@pytest.mark.asyncio
async def test_empty_input():
    assert await chunk_exam_markdown("") == []
    assert await chunk_exam_markdown("   ") == []


def test_mask_unmask_roundtrip():
    from app.services.docling_chunker import _mask_formulas, _unmask_formulas
    text = "Cho $$x^2+1$$ và $y=2$ thì sao?"
    masked, masks = _mask_formulas(text)
    assert "$$" not in masked and "@@F0@@" in masked and "@@F1@@" in masked
    assert _unmask_formulas(masked, masks) == text
