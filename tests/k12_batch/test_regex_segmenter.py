"""Unit tests for ``app.services.k12_batch.regex_segmenter``."""

from __future__ import annotations

from app.services.k12_batch.regex_segmenter import (
    QuestionBlock,
    annotate_hints,
    detect_numbering_gaps,
    segment,
    split_into_blocks,
    strip_answer_section,
)


SAMPLE_TN = """\
Câu 1. Tìm x biết $2x + 3 = 7$.
A. $x = 1$
B. $x = 2$
C. $x = 3$
D. $x = 4$

Câu 2: Phương trình $x^2 - 5x + 6 = 0$ có nghiệm:
A) $x = 1, 2$
B) $x = 2, 3$
C) $x = 3, 4$
D) $x = 4, 5$

Bài 3) Tính $$\\int_0^1 x dx$$.
A. $0.5$
B. $1$
C. $0$
D. $2$

ĐÁP ÁN
1. B    2. B    3. A
"""

SAMPLE_TF = """\
Câu 1. Cho hàm số $f(x) = x^2$. Xét tính đúng sai:
a) $f(2) = 4$. Đúng
b) $f(-1) = -1$. Sai
c) $f$ là hàm chẵn. Đúng
d) $f$ luôn dương. Sai
"""

SAMPLE_NUMERIC = """\
1. Tính 1+1
2. Tính 2+2
3. Tính 3+3
4. Tính 4+4
"""


class TestStripAnswerSection:
    def test_strips_dap_an(self) -> None:
        body, ans = strip_answer_section(SAMPLE_TN)
        assert "ĐÁP ÁN" not in body
        assert ans.startswith("ĐÁP ÁN")
        assert "Câu 1" in body

    def test_no_section_returns_empty(self) -> None:
        body, ans = strip_answer_section("Câu 1. abc")
        assert body == "Câu 1. abc"
        assert ans == ""

    def test_empty_input(self) -> None:
        body, ans = strip_answer_section("")
        assert body == "" and ans == ""


class TestSplitIntoBlocks:
    def test_cau_and_bai_markers(self) -> None:
        body, _ = strip_answer_section(SAMPLE_TN)
        blocks = split_into_blocks(body)
        assert [b.number for b in blocks] == [1, 2, 3]
        assert [b.marker_kind for b in blocks] == ["cau", "cau", "bai"]

    def test_numeric_fallback_active_only_without_cau(self) -> None:
        blocks = split_into_blocks(SAMPLE_NUMERIC)
        assert [b.number for b in blocks] == [1, 2, 3, 4]
        assert all(b.marker_kind == "numeric" for b in blocks)

    def test_numeric_fallback_disabled(self) -> None:
        blocks = split_into_blocks(SAMPLE_NUMERIC, fallback_numeric=False)
        assert blocks == []

    def test_leading_zero_tolerated(self) -> None:
        md = "Câu 01. abc\nCâu 02. xyz"
        blocks = split_into_blocks(md)
        assert [b.number for b in blocks] == [1, 2]


class TestAnnotateHints:
    def test_tn_4choice_detected(self) -> None:
        md = "Câu 1. Tính.\nA. a\nB. b\nC. c\nD. d"
        block = QuestionBlock(number=1, raw_block=md)
        annotate_hints(block)
        assert block.hint_type == "tn_4choice"
        assert set(block.hint_options) == {"A", "B", "C", "D"}

    def test_true_false_candidate(self) -> None:
        block = QuestionBlock(number=1, raw_block=SAMPLE_TF)
        annotate_hints(block)
        assert block.hint_type == "true_false_candidate"

    def test_no_options_short(self) -> None:
        block = QuestionBlock(number=1, raw_block="Câu 1. Tính 1+1 = ?")
        annotate_hints(block)
        assert block.hint_type == "no_options"

    def test_essay_long(self) -> None:
        body = "Câu 1. " + "Chứng minh rằng với mọi $x$ ta có một đẳng thức nào đó. " * 8
        block = QuestionBlock(number=1, raw_block=body)
        annotate_hints(block)
        assert block.hint_type == "essay_long"

    def test_image_refs_collected(self) -> None:
        md = "Câu 1. Cho mạch ![fig](images/p1.png).\nA. a\nB. b\nC. c\nD. d"
        block = QuestionBlock(number=1, raw_block=md)
        annotate_hints(block)
        assert block.image_refs == ["![fig](images/p1.png)"]


class TestSegment:
    def test_tn_full_flow(self) -> None:
        blocks, ans = segment(SAMPLE_TN)
        assert len(blocks) == 3
        assert ans.startswith("ĐÁP ÁN")
        assert all(b.hint_type == "tn_4choice" for b in blocks)

    def test_empty_input(self) -> None:
        blocks, ans = segment("")
        assert blocks == [] and ans == ""


class TestDetectNumberingGaps:
    def test_gaps(self) -> None:
        blocks = [QuestionBlock(number=n, raw_block="") for n in (1, 2, 5)]
        assert detect_numbering_gaps(blocks) == [3, 4]

    def test_no_gaps(self) -> None:
        blocks = [QuestionBlock(number=n, raw_block="") for n in (1, 2, 3)]
        assert detect_numbering_gaps(blocks) == []

    def test_empty(self) -> None:
        assert detect_numbering_gaps([]) == []
