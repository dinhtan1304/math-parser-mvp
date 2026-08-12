"""Unit tests for ``app.services.k12_batch.gemini_finalizer``.

The Gemini call itself is mocked — these tests only exercise the
wrapper logic (input assembly, image re-insertion, default filling).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.k12_batch.gemini_finalizer import (
    _apply_defaults,
    assemble_input_text,
    finalize,
    merge_image_refs,
    subject_label,
)
from app.services.k12_batch.regex_segmenter import QuestionBlock


class TestSubjectLabel:
    def test_known_subject(self) -> None:
        assert subject_label("toan") == "Toán"
        assert subject_label("ly") == "Vật lý"
        assert subject_label("anh") == "Tiếng Anh"

    def test_unknown_subject_returns_input(self) -> None:
        assert subject_label("xxx") == "xxx"


class TestAssembleInputText:
    def test_includes_subject_grade_hint(self) -> None:
        blocks = [QuestionBlock(number=1, raw_block="Câu 1. abc")]
        text = assemble_input_text(blocks, "toan", 10)
        assert "Toán" in text and "lớp 10" in text and "Câu 1." in text

    def test_no_hint_when_disabled(self) -> None:
        blocks = [QuestionBlock(number=1, raw_block="Câu 1. abc")]
        text = assemble_input_text(blocks, "toan", 10, inject_hint=False)
        assert "K12 hint" not in text

    def test_preserves_block_order(self) -> None:
        blocks = [
            QuestionBlock(number=1, raw_block="Câu 1. one"),
            QuestionBlock(number=2, raw_block="Câu 2. two"),
            QuestionBlock(number=3, raw_block="Câu 3. three"),
        ]
        text = assemble_input_text(blocks, "toan", 10)
        assert text.index("one") < text.index("two") < text.index("three")


class TestMergeImageRefs:
    def test_reinjects_missing_image(self) -> None:
        blocks = [QuestionBlock(number=1, raw_block="...", image_refs=["![mach](images/p1.png)"])]
        parsed = [{"question": "Câu 1. Cho mạch."}]
        out = merge_image_refs(parsed, blocks)
        assert "images/p1.png" in out[0]["question"]

    def test_no_op_if_image_already_present(self) -> None:
        blocks = [QuestionBlock(number=1, raw_block="...", image_refs=["![mach](images/p1.png)"])]
        parsed = [{"question": "Câu 1. Cho mạch ![mach](images/p1.png)."}]
        out = merge_image_refs(parsed, blocks)
        # Image kept exactly once.
        assert out[0]["question"].count("p1.png") == 1

    def test_matches_by_question_number(self) -> None:
        blocks = [
            QuestionBlock(number=1, raw_block="", image_refs=["![](a.png)"]),
            QuestionBlock(number=2, raw_block="", image_refs=["![](b.png)"]),
        ]
        # Gemini returns them in reverse order.
        parsed = [
            {"question": "Câu 2. abc"},
            {"question": "Câu 1. xyz"},
        ]
        out = merge_image_refs(parsed, blocks)
        assert "b.png" in out[0]["question"]
        assert "a.png" in out[1]["question"]


class TestApplyDefaults:
    def test_renames_subject_to_subject_code(self) -> None:
        item = {"question": "x", "subject": "vat-li"}
        out = _apply_defaults(item, "ly", 10)
        assert out["subject_code"] == "vat-li"
        assert "subject" not in out

    def test_fills_subject_code_when_missing(self) -> None:
        out = _apply_defaults({"question": "x"}, "toan", 10)
        assert out["subject_code"] == "toan" and out["grade"] == 10

    def test_normalizes_solution_steps_string_to_list(self) -> None:
        out = _apply_defaults({"question": "x", "solution_steps": "buoc 1"}, "toan", 10)
        assert out["solution_steps"] == ["buoc 1"]

    def test_empty_string_solution_becomes_empty_list(self) -> None:
        out = _apply_defaults({"question": "x", "solution_steps": ""}, "toan", 10)
        assert out["solution_steps"] == []

    def test_invalid_solution_type_becomes_empty_list(self) -> None:
        out = _apply_defaults({"question": "x", "solution_steps": 42}, "toan", 10)
        assert out["solution_steps"] == []


class TestFinalize:
    @pytest.mark.asyncio
    async def test_empty_blocks_returns_empty_list(self) -> None:
        mock_parser = AsyncMock()
        out = await finalize([], "toan", 10, mock_parser)
        assert out == []
        mock_parser.parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_ai_parser_with_assembled_text(self) -> None:
        blocks = [QuestionBlock(number=1, raw_block="Câu 1. Tính 1+1")]
        mock_parser = AsyncMock()
        mock_parser.parse.return_value = [
            {"question": "Câu 1. Tính 1+1", "type": "TN", "subject": "toan"}
        ]
        out = await finalize(blocks, "toan", 10, mock_parser)
        assert len(out) == 1
        # The wrapper handed the parser our assembled text + subject hint.
        assert mock_parser.parse.await_count == 1
        kwargs = mock_parser.parse.call_args.kwargs
        assert kwargs["subject_hint"] == "toan"
        assert "Câu 1" in kwargs["text"]
        # And the result was normalized.
        assert out[0]["subject_code"] == "toan"
        assert out[0]["grade"] == 10

    @pytest.mark.asyncio
    async def test_missing_images_reinjected(self) -> None:
        blocks = [QuestionBlock(number=1, raw_block="...", image_refs=["![](images/x.png)"])]
        mock_parser = AsyncMock()
        mock_parser.parse.return_value = [{"question": "Câu 1. Cho hình.", "type": "TN"}]
        out = await finalize(blocks, "toan", 10, mock_parser)
        assert "images/x.png" in out[0]["question"]
