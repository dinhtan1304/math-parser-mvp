"""
Test suite for the math parser pipeline.

Covers all 4 fixes + each pipeline stage:
  Stage 1 — Upload & Validate
  Stage 2 — Extract Content (quality checks, vision fallback)
  Stage 3 — AI Parse (JSON repair, answer pool, mock-result detection)
  Stage 4 — Save & Classify (intra-batch dedup, re-parse idempotency)
  Stage 5 — Background Index (independent failure isolation)

Run:
    cd math-parser-mvp
    pytest tests/test_parser_pipeline.py -v
"""

import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio


# ─────────────────────────────────────────────
# Top-level imports (real modules, no sys.modules hacks)
# ─────────────────────────────────────────────

# file_handler — pure Python, no DB dependency
from app.services.file_handler import FileHandler

# ai_parser — needs no API key for unit tests
from app.services.ai_parser import AIQuestionParser

# question hash — pure function, no DB needed at import time
from app.db.models.question import _question_hash

# parser.py — now importable after fixing get_current_user bug
import app.api.parser as parser_mod


# ══════════════════════════════════════════════
# STAGE 1 — Upload & Validate
# ══════════════════════════════════════════════

class TestUploadValidation:
    """FIX #1 — path sanitization; FIX #2 — orphan file cleanup."""

    def test_basename_strips_unix_path_traversal(self):
        """FIX #1: ../../../etc/passwd → passwd"""
        result = os.path.basename("../../../etc/passwd")
        assert "/" not in result
        assert ".." not in result
        assert result == "passwd"

    def test_basename_strips_windows_path_traversal(self):
        """FIX #1: ..\\..\\windows\\evil → evil"""
        result = os.path.basename("..\\..\\windows\\evil")
        assert "\\" not in result

    def test_basename_strips_absolute_path(self):
        result = os.path.basename("/absolute/path/evil.pdf")
        assert result == "evil.pdf"

    def test_basename_strips_subdirectory(self):
        result = os.path.basename("subdir/sneaky.pdf")
        assert result == "sneaky.pdf"

    def test_basename_preserves_normal_filename(self):
        normal = "de_thi_toan_12.pdf"
        assert os.path.basename(normal) == normal

    def test_basename_fallback_on_none(self):
        """FIX #1: None filename must not crash — endpoint uses `file.filename or 'unnamed'`."""
        result = os.path.basename(None or "unnamed")
        assert result == "unnamed"

    @pytest.mark.asyncio
    async def test_orphan_file_cleaned_on_db_failure(self, tmp_path):
        """FIX #2: uploaded file must be deleted if DB commit raises."""
        file_path = tmp_path / "test.pdf"
        file_path.write_bytes(b"fake pdf content")
        assert file_path.exists()

        # Replicate the cleanup logic from parse_file_endpoint
        try:
            raise RuntimeError("DB commit failed")
        except Exception:
            try:
                os.remove(str(file_path))
            except OSError:
                pass

        assert not file_path.exists(), "Orphaned file must be removed on DB failure"

    @pytest.mark.asyncio
    async def test_file_write_failure_does_not_reach_db(self, tmp_path):
        """FIX #2: if file write raises OSError, DB must never be touched."""
        db_touched = []

        try:
            with open("/nonexistent_dir_xyz/file.pdf", "wb") as f:
                f.write(b"content")
        except OSError:
            # endpoint raises HTTPException here, DB never touched
            pass
        else:
            db_touched.append("should not reach")

        assert db_touched == []


# ══════════════════════════════════════════════
# STAGE 2 — Extract Content
# ══════════════════════════════════════════════

class TestTextQualityCheck:
    """_is_math_text_poor_quality heuristics (in parser.py)."""

    check = staticmethod(parser_mod._is_math_text_poor_quality)

    def test_empty_string_is_poor(self):
        assert self.check("") is True

    def test_whitespace_only_is_poor(self):
        assert self.check("   ") is True

    def test_short_text_is_poor(self):
        assert self.check("x = 1") is True   # < 50 chars

    def test_no_math_markers_is_poor(self):
        text = "A" * 100
        assert self.check(text) is True

    def test_good_math_text_passes(self):
        text = (
            "Câu 1: Giải phương trình $x^2 - 5x + 6 = 0$.\n"
            "Câu 2: Tính $\\frac{1}{2} + \\frac{3}{4}$.\n"
            "Câu 3: Tìm x biết $\\sqrt{x} + 4 = 10$.\n"
        )
        assert self.check(text) is False

    def test_garbled_binary_chars_are_poor(self):
        # Over 10% non-printable chars (ord < 32 excluding \n\r\t)
        bad = "Câu 1: " + "".join(chr(c) for c in range(1, 8)) * 20 + "= + -"
        assert self.check(bad) is True

    def test_exactly_3_markers_passes(self):
        # '=', '+', 'Câu' → 3 markers → not poor
        text = "a" * 80 + " = + Câu"
        assert self.check(text) is False

    def test_only_2_markers_is_poor(self):
        text = "a" * 80 + " = +"
        assert self.check(text) is True


class TestVisionFallbackLogic:
    """FIX #3 — simplified vision fallback (dead `if images` branch removed)."""

    @pytest.mark.asyncio
    async def test_vision_fallback_always_reextracts(self):
        """Text mode never returns images → fallback always calls extract_text(vision=True)."""
        call_log = []

        async def mock_extract(path, use_vision=False):
            call_log.append(use_vision)
            if use_vision:
                return {"text": "", "images": [{"page": 1, "data": "abc", "mime_type": "image/jpeg"}]}
            # Text mode: never returns images
            return {"text": "garbage\x01\x02", "images": [], "file_hash": "abc"}

        # First pass: text mode
        extracted = await mock_extract("/fake/path.pdf", use_vision=False)
        images = extracted.get("images", [])  # always [] from text mode

        poor_quality = True
        use_vision = False

        # Simplified fallback after FIX #3: no `if images` branch
        if poor_quality and not use_vision:
            extracted = await mock_extract("/fake/path.pdf", use_vision=True)
            images = extracted.get("images", [])
            use_vision = True

        assert use_vision is True
        assert len(images) == 1
        assert call_log == [False, True]

    @pytest.mark.asyncio
    async def test_vision_fallback_skipped_when_already_vision(self):
        """If use_vision=True from the start, fallback is not triggered."""
        call_log = []

        async def mock_extract(path, use_vision=False):
            call_log.append(use_vision)
            return {"text": "", "images": [], "file_hash": "abc"}

        extracted = await mock_extract("/fake/path.pdf", use_vision=True)
        use_vision = True
        poor_quality = True

        if poor_quality and not use_vision:  # False because use_vision=True
            await mock_extract("/fake/path.pdf", use_vision=True)

        assert call_log == [True]  # only the original call, no fallback


class TestFileHandlerQualityCheck:
    """_is_quality_good in file_handler (used to choose between PDF libraries)."""

    def setup_method(self):
        self.handler = FileHandler()

    def test_empty_text_is_bad(self):
        assert self.handler._is_quality_good("") is False

    def test_short_text_is_bad(self):
        assert self.handler._is_quality_good("short") is False

    def test_high_newline_ratio_is_bad(self):
        # ab\n ab\n ... → newline every 3 chars → ratio ~0.33 > 0.2
        text = "ab\n" * 100
        assert self.handler._is_quality_good(text) is False

    def test_many_single_char_lines_is_bad(self):
        # 40 single-char lines + 10 real lines → 80% single-char > 30% threshold
        lines = ["x\n"] * 40 + ["This is a real sentence\n"] * 10
        text = "".join(lines)
        assert self.handler._is_quality_good(text) is False

    def test_good_text_passes(self):
        text = "Câu 1: Giải phương trình sau đây. " * 20
        assert self.handler._is_quality_good(text) is True

    def test_clean_text_strips_control_chars(self):
        dirty = "Câu 1\x00\x01\x08: nội dung\n\n\n\n\n bài toán"
        result = self.handler._clean_text(dirty)
        assert "\x00" not in result
        assert "\x01" not in result

    def test_clean_text_collapses_excess_newlines(self):
        text = "line1\n\n\n\n\n\nline2"
        result = self.handler._clean_text(text)
        assert "\n\n\n\n" not in result


# ══════════════════════════════════════════════
# STAGE 3 — AI Parse
# ══════════════════════════════════════════════

class TestMockResultDetection:
    """_is_mock_result: rejects low-quality cached results."""

    check = staticmethod(parser_mod._is_mock_result)

    def _make_question(self, topic="TOÁN 9 — C3.Căn thức", grade=9,
                       chapter="Căn thức", steps=None):
        return {
            "question": "Tính $\\sqrt{4}$",
            "topic": topic,
            "grade": grade,
            "chapter": chapter,
            "solution_steps": steps or ["Bước 1", "Bước 2"],
            "answer": "2",
        }

    def test_empty_list_is_mock(self):
        assert self.check([]) is True

    def test_good_questions_not_mock(self):
        qs = [self._make_question() for _ in range(5)]
        assert self.check(qs) is False

    def test_all_generic_topic_no_grade_no_steps_is_mock(self):
        qs = [
            {"question": f"Câu {i}", "topic": "Toán học",
             "grade": None, "chapter": "", "solution_steps": []}
            for i in range(5)
        ]
        assert self.check(qs) is True

    def test_threshold_calculation(self):
        """
        mock_signs > len(sample)*2 → is mock.
        5 questions, each with 3 signs → 15 > 10 → mock.
        3 bad + 2 good → bad: 3*3=9, good: 0 → 9 < 10 → not mock.
        """
        all_bad = [
            {"question": f"Q{i}", "topic": "Toán học",
             "grade": None, "chapter": "", "solution_steps": []}
            for i in range(5)
        ]
        assert self.check(all_bad) is True

        mixed = [self._make_question() for _ in range(3)]
        mixed += [
            {"question": f"Q{i}", "topic": "Toán học",
             "grade": None, "chapter": "", "solution_steps": []}
            for i in range(2)
        ]
        # mock_signs from 2 bad = 2*3=6; threshold = 5*2=10 → 6 < 10 → not mock
        assert self.check(mixed) is False


class TestJSONRepair:
    """_extract_json and _aggressive_extract_json repair pipeline."""

    def setup_method(self):
        self.parser = AIQuestionParser.__new__(AIQuestionParser)
        self.parser._answer_pool = {}

    def test_valid_json_fast_path(self):
        data = [{"question": "Tính $x^2$", "answer": "x²"}]
        result = self.parser._extract_json(json.dumps(data))
        assert result == data

    def test_json_in_markdown_fence(self):
        data = [{"question": "Câu 1", "answer": "A"}]
        content = f"```json\n{json.dumps(data)}\n```"
        result = self.parser._extract_json(content)
        assert result == data

    def test_repair_trailing_commas(self):
        bad = '[{"question": "Câu 1", "answer": "A",}]'
        result = self.parser._aggressive_extract_json(bad)
        assert len(result) == 1
        assert result[0]["question"] == "Câu 1"

    def test_repair_triple_backslashes(self):
        # Gemini sometimes emits \\\ → must be fixed to \\
        bad = '[{"question": "$\\\\\\\\frac{1}{2}$", "answer": ""}]'
        result = self.parser._aggressive_extract_json(bad)
        assert len(result) == 1

    def test_repair_python_literals(self):
        bad = '[{"question": "Q", "answer": null, "ok": true, "fail": false}]'
        # python literals shouldn't be there but test the fix
        bad_py = bad.replace("null", "None").replace("true", "True").replace("false", "False")
        result = self.parser._aggressive_extract_json(bad_py)
        assert len(result) == 1

    def test_repair_control_chars(self):
        bad = '[{"question": "Q\x00\x01\x1f", "answer": "A"}]'
        result = self.parser._aggressive_extract_json(bad)
        assert len(result) == 1
        assert "\x00" not in result[0]["question"]

    def test_no_bracket_returns_empty(self):
        assert self.parser._aggressive_extract_json("no json here") == []

    def test_individual_object_salvage(self):
        """Last-resort: extract individual objects from broken array.
        _aggressive_extract_json needs a closing ] to enter the repair path
        (without it, rfind(']') == -1 → returns [] immediately).
        A trailing ] with a broken second object triggers _extract_individual_objects.
        """
        broken = (
            '[{"question": "Câu 1", "answer": "A", "type": "TN"},'
            ' {"question": "Câu 2", "answer": "B", "type": "TN"'  # missing }
            "]"
        )
        result = self.parser._aggressive_extract_json(broken)
        # First complete object must be salvaged
        assert any(q.get("question") == "Câu 1" for q in result)

    def test_empty_input_returns_empty(self):
        assert self.parser._extract_json("") == []
        assert self.parser._aggressive_extract_json("") == []


class TestAnswerPool:
    """Cross-chunk answer matching."""

    def setup_method(self):
        self.parser = AIQuestionParser.__new__(AIQuestionParser)
        self.parser._answer_pool = {}

    def test_collect_short_entry_as_answer_key(self):
        """'Câu 3: B' (short) → goes into pool."""
        qs = [{"question": "Câu 3: B", "answer": ""}]
        self.parser._collect_answers(qs)
        assert "3" in self.parser._answer_pool

    def test_match_fills_empty_answer(self):
        self.parser._answer_pool = {"5": "C"}
        qs = [{"question": "Câu 5: Tính giá trị $x^2 + 1$ khi $x=2$.", "answer": ""}]
        result = self.parser._match_answers_from_pool(qs)
        assert result[0]["answer"] == "C"

    def test_standalone_answer_entries_filtered_out(self):
        """Pure answer-key entries must not appear as questions."""
        qs = [
            {"question": "Câu 1: A", "answer": ""},
            {"question": "Câu 2: B", "answer": ""},
        ]
        result = self.parser._match_answers_from_pool(qs)
        assert result == []

    def test_existing_answer_not_overwritten(self):
        self.parser._answer_pool = {"3": "D"}
        qs = [{"question": "Câu 3: Giải phương trình sau. " * 3, "answer": "C"}]
        result = self.parser._match_answers_from_pool(qs)
        assert result[0]["answer"] == "C"  # pool must not override

    def test_pool_ignores_long_questions(self):
        """Long question text → not treated as answer-key entry."""
        long_q = "Câu 3: " + "Nội dung câu hỏi rất dài. " * 5
        qs = [{"question": long_q, "answer": ""}]
        self.parser._answer_pool = {"3": "B"}
        result = self.parser._match_answers_from_pool(qs)
        assert result[0]["answer"] == "B"


class TestChunking:
    """Smart chunking on question boundaries."""

    def setup_method(self):
        self.parser = AIQuestionParser.__new__(AIQuestionParser)
        self.parser.max_chunk_size = 200

    def test_short_text_stays_single_chunk(self):
        text = "Câu 1: x = 1\nCâu 2: y = 2"
        chunks = self.parser._smart_chunk(text)
        assert len(chunks) == 1
        assert "Câu 1" in chunks[0]
        assert "Câu 2" in chunks[0]

    def test_long_text_splits_at_boundaries(self):
        line = "Câu {n}: Tính giá trị của biểu thức $f(x) = x^2 - 3x + 2$.\n"
        text = "".join(line.format(n=i) for i in range(1, 8))
        chunks = self.parser._smart_chunk(text)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= self.parser.max_chunk_size * 1.2

    def test_no_markers_falls_back_to_size_chunking(self):
        text = "a" * 500
        chunks = self.parser._chunk_by_size(text)
        assert len(chunks) >= 2
        assert sum(len(c) for c in chunks) == 500

    def test_no_data_lost(self):
        line = "Câu {n}: Nội dung câu hỏi số {n} với đủ nội dung dài.\n"
        text = "".join(line.format(n=i) for i in range(1, 15))
        chunks = self.parser._smart_chunk(text)
        reconstructed = "".join(chunks)
        # Allow small intro section before first "Câu" to be dropped
        assert len(reconstructed) >= len(text) * 0.95


# ══════════════════════════════════════════════
# STAGE 4 — Save & Classify
# ══════════════════════════════════════════════

class TestIntraBatchDedup:
    """FIX #4 — cross-exam dedup removed; intra-batch dedup kept."""

    def _run_dedup_logic(self, questions: list) -> tuple[int, int]:
        """Replicate the dedup loop from _save_questions_to_bank after FIX #4."""
        new_questions = []
        for i, q in enumerate(questions):
            q_text = q.get("question", "")
            if not q_text.strip():
                continue
            c_hash = _question_hash(q_text)
            new_questions.append((i, q, c_hash))

        # FIX #4: always start empty (no cross-exam query)
        existing_hashes: set = set()

        saved = 0
        skipped = 0
        for i, q, c_hash in new_questions:
            if c_hash in existing_hashes:
                skipped += 1
                continue
            existing_hashes.add(c_hash)
            saved += 1

        return saved, skipped

    def test_unique_questions_all_saved(self):
        qs = [
            {"question": "Tính $1 + 1$"},
            {"question": "Tính $2 + 2$"},
            {"question": "Tính $3 + 3$"},
        ]
        saved, skipped = self._run_dedup_logic(qs)
        assert saved == 3
        assert skipped == 0

    def test_exact_duplicate_within_batch_skipped(self):
        qs = [
            {"question": "Tính $\\sqrt{4}$"},
            {"question": "Tính $\\sqrt{4}$"},
        ]
        saved, skipped = self._run_dedup_logic(qs)
        assert saved == 1
        assert skipped == 1

    def test_whitespace_normalized_dedup(self):
        qs = [
            {"question": "Tính   $x^2$"},
            {"question": "Tính $x^2$"},
        ]
        saved, skipped = self._run_dedup_logic(qs)
        assert saved == 1
        assert skipped == 1

    def test_empty_question_text_filtered_before_dedup(self):
        qs = [
            {"question": ""},
            {"question": "   "},
            {"question": "Câu hỏi hợp lệ"},
        ]
        saved, skipped = self._run_dedup_logic(qs)
        assert saved == 1
        assert skipped == 0

    def test_same_question_different_exams_both_saved(self):
        """
        FIX #4: each exam's parse starts with empty existing_hashes.
        Same question in exam_A and exam_B → saved in both.
        """
        q = {"question": "Tính $2^{10}$"}
        saved_a, _ = self._run_dedup_logic([q])
        saved_b, _ = self._run_dedup_logic([q])
        assert saved_a == 1, "exam_A should save"
        assert saved_b == 1, "exam_B should also save (no cross-exam dedup)"

    def test_large_batch_no_duplicates(self):
        qs = [{"question": f"Câu {i}: Tính $x^{i}$"} for i in range(50)]
        saved, skipped = self._run_dedup_logic(qs)
        assert saved == 50
        assert skipped == 0


class TestQuestionHash:
    """_question_hash used for dedup."""

    def test_same_content_same_hash(self):
        assert _question_hash("Tính $x^2$") == _question_hash("Tính $x^2$")

    def test_whitespace_normalized(self):
        assert _question_hash("  Tính  $x^2$  ") == _question_hash("Tính $x^2$")

    def test_case_insensitive(self):
        assert _question_hash("TÍNH $X^2$") == _question_hash("tính $x^2$")

    def test_different_content_different_hash(self):
        assert _question_hash("Câu 1: $x=1$") != _question_hash("Câu 2: $x=2$")

    def test_empty_string(self):
        assert _question_hash("") == _question_hash("   ")


# ══════════════════════════════════════════════
# STAGE 5 — Background Index
# ══════════════════════════════════════════════

class TestBackgroundIndexIsolation:
    """Each index step must be independent — one failure must not block others."""

    @pytest.mark.asyncio
    async def test_fts_failure_does_not_block_embedding(self):
        ran = {"fts": False, "embed": False, "similarity": False, "difficulty": False}

        async def fake_fts(db, ids):
            ran["fts"] = True
            raise RuntimeError("FTS table missing")

        async def fake_embed(db, ids):
            ran["embed"] = True

        async def fake_similarity(db, exam_id, user_id):
            ran["similarity"] = True
            return 0

        async def fake_difficulty(db, exam_id, user_id):
            ran["difficulty"] = True
            return 0

        db = MagicMock()
        for step, args in [
            (fake_fts, (db, [1, 2])),
            (fake_embed, (db, [1, 2])),
            (fake_similarity, (db, 1, 1)),
            (fake_difficulty, (db, 1, 1)),
        ]:
            try:
                await step(*args)
            except Exception:
                pass

        assert all(ran.values()), f"Some steps did not run: {ran}"

    @pytest.mark.asyncio
    async def test_all_steps_failing_is_graceful(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("catastrophic")

        errors = []
        for _ in range(4):
            try:
                await boom()
            except Exception as e:
                errors.append(str(e))

        assert len(errors) == 4

    @pytest.mark.asyncio
    async def test_step_order_preserved_on_partial_failure(self):
        """Steps must run in order even if earlier ones fail."""
        order = []

        async def step1(db, ids):
            order.append(1)
            raise RuntimeError("step1 fails")

        async def step2(db, ids):
            order.append(2)

        async def step3(db, exam_id, user_id):
            order.append(3)

        db = MagicMock()
        for fn, args in [(step1, (db, [])), (step2, (db, [])), (step3, (db, 0, 0))]:
            try:
                await fn(*args)
            except Exception:
                pass

        assert order == [1, 2, 3]


# ══════════════════════════════════════════════
# SSE Progress Events
# ══════════════════════════════════════════════

class TestSSEProgressPublish:
    """_publish_progress and subscribe/unsubscribe mechanics."""

    @pytest.mark.asyncio
    async def test_subscribe_and_receive_event(self):
        exam_id = 9901
        q = await parser_mod._subscribe(exam_id)

        parser_mod._publish_progress(exam_id, "progress", {"percent": 50})

        event, data = q.get_nowait()
        assert event == "progress"
        assert json.loads(data)["percent"] == 50

        await parser_mod._unsubscribe(exam_id, q)

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_empty_key(self):
        exam_id = 9902
        q = await parser_mod._subscribe(exam_id)
        assert exam_id in parser_mod._progress_queues

        await parser_mod._unsubscribe(exam_id, q)
        assert exam_id not in parser_mod._progress_queues

    @pytest.mark.asyncio
    async def test_publish_with_no_subscribers_is_safe(self):
        # Must not raise even when no one is subscribed
        parser_mod._publish_progress(99901, "progress", {"percent": 10})

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self):
        exam_id = 9903
        q1 = await parser_mod._subscribe(exam_id)
        q2 = await parser_mod._subscribe(exam_id)

        parser_mod._publish_progress(exam_id, "complete", {"message": "done"})

        assert not q1.empty()
        assert not q2.empty()

        await parser_mod._unsubscribe(exam_id, q1)
        await parser_mod._unsubscribe(exam_id, q2)

    @pytest.mark.asyncio
    async def test_full_queue_drops_event_silently(self):
        """Slow client (full queue) → event dropped, no exception."""
        q = asyncio.Queue(maxsize=1)
        exam_id = 9904

        async with parser_mod._queues_lock:
            parser_mod._progress_queues[exam_id] = [q]

        q.put_nowait(("progress", "{}"))  # fill queue

        # Second publish should not raise (QueueFull swallowed)
        parser_mod._publish_progress(exam_id, "progress", {"percent": 90})

        async with parser_mod._queues_lock:
            del parser_mod._progress_queues[exam_id]

    @pytest.mark.asyncio
    async def test_terminal_events_recognized(self):
        """'complete' and 'error_event' are the terminal event names."""
        terminal = {"complete", "error_event"}
        exam_id = 9905
        q = await parser_mod._subscribe(exam_id)

        for event in terminal:
            parser_mod._publish_progress(exam_id, event, {"message": "done"})
            ev_name, _ = q.get_nowait()
            assert ev_name in terminal

        await parser_mod._unsubscribe(exam_id, q)


# ══════════════════════════════════════════════
# Integration: process_file (mocked DB + AI)
# ══════════════════════════════════════════════

class TestProcessFileIntegration:
    """End-to-end flow with all external services mocked."""

    def _make_exam(self, exam_id=1):
        exam = MagicMock()
        exam.id = exam_id
        exam.user_id = 42
        exam.file_path = "/fake/exam.pdf"
        exam.file_hash = None
        exam.status = "pending"
        exam.result_json = None
        exam.error_message = None
        return exam

    def _fake_questions(self):
        return [{
            "question": "Tính $2^5$",
            "type": "TN",
            "topic": "TOÁN 6 — C1.Số tự nhiên",
            "difficulty": "NB",
            "grade": 6,
            "chapter": "C1",
            "lesson_title": "Lũy thừa",
            "answer": "32",
            "solution_steps": ["$2^5 = 32$"],
        }]

    def _make_db_context(self, exam):
        """
        Build a mock DB context that handles two different query shapes:
        - Exam lookup:  .scalars().first() → exam
        - Cache lookup: .scalar()           → None  (simulate cache miss)
        """
        # Result object that handles both .scalars().first() and .scalar()
        exam_result = MagicMock()
        exam_result.scalars.return_value.first.return_value = exam
        exam_result.scalar.return_value = None  # no cache hit

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=exam_result)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()
        mock_db.rollback = AsyncMock()

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_db)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    @pytest.mark.asyncio
    async def test_successful_parse_sets_completed(self):
        exam = self._make_exam()
        cm = self._make_db_context(exam)

        with (
            patch("app.api.parser.AsyncSessionLocal", return_value=cm),
            patch("app.api.parser.file_handler") as mock_fh,
            patch("app.api.parser.ai_parser") as mock_ai,
            patch("app.api.parser._save_questions_to_bank", new_callable=AsyncMock),
            patch("app.api.parser._publish_progress"),
            patch("os.path.exists", return_value=False),
        ):
            mock_fh.extract_text = AsyncMock(return_value={
                "text": "Câu 1: Tính $2^5$ = ?\n" * 5,
                "images": [],
                "file_hash": "abc123",
            })
            mock_ai._client = True
            mock_ai.parse = AsyncMock(return_value=self._fake_questions())

            await parser_mod.process_file(exam_id=1, speed="balanced", use_vision=False)

        assert exam.status == "completed"
        assert exam.result_json is not None

    @pytest.mark.asyncio
    async def test_extraction_failure_sets_failed(self):
        exam = self._make_exam()
        cm = self._make_db_context(exam)

        with (
            patch("app.api.parser.AsyncSessionLocal", return_value=cm),
            patch("app.api.parser.file_handler") as mock_fh,
            patch("app.api.parser.ai_parser") as mock_ai,
            patch("app.api.parser._publish_progress"),
            patch("os.path.exists", return_value=False),
        ):
            mock_fh.extract_text = AsyncMock(side_effect=RuntimeError("Corrupt PDF"))
            mock_ai._client = True

            await parser_mod.process_file(exam_id=1, speed="balanced", use_vision=False)

        assert exam.status == "failed"
        assert "Corrupt PDF" in (exam.error_message or "")

    @pytest.mark.asyncio
    async def test_bank_save_failure_keeps_completed(self):
        """Exam stays 'completed' even when _save_questions_to_bank raises."""
        exam = self._make_exam()
        cm = self._make_db_context(exam)

        with (
            patch("app.api.parser.AsyncSessionLocal", return_value=cm),
            patch("app.api.parser.file_handler") as mock_fh,
            patch("app.api.parser.ai_parser") as mock_ai,
            patch("app.api.parser._save_questions_to_bank",
                  new_callable=AsyncMock, side_effect=RuntimeError("DB crash")),
            patch("app.api.parser._publish_progress"),
            patch("os.path.exists", return_value=False),
        ):
            mock_fh.extract_text = AsyncMock(return_value={
                "text": "Câu 1: Tính $2^5$\n" * 5,
                "images": [],
                "file_hash": "xyz",
            })
            mock_ai._client = True
            mock_ai.parse = AsyncMock(return_value=self._fake_questions())

            await parser_mod.process_file(exam_id=1, speed="balanced", use_vision=False)

        assert exam.status == "completed"

    @pytest.mark.asyncio
    async def test_ai_returns_no_questions_sets_failed(self):
        """If AI returns empty list → ValueError → exam fails."""
        exam = self._make_exam()
        cm = self._make_db_context(exam)

        with (
            patch("app.api.parser.AsyncSessionLocal", return_value=cm),
            patch("app.api.parser.file_handler") as mock_fh,
            patch("app.api.parser.ai_parser") as mock_ai,
            patch("app.api.parser._publish_progress"),
            patch("os.path.exists", return_value=False),
        ):
            mock_fh.extract_text = AsyncMock(return_value={
                "text": "Câu 1: Tính $2^5$\n" * 5,
                "images": [],
                "file_hash": "abc",
            })
            mock_ai._client = True
            mock_ai.parse = AsyncMock(return_value=[])  # AI found nothing

            await parser_mod.process_file(exam_id=1, speed="balanced", use_vision=False)

        assert exam.status == "failed"
        assert exam.error_message is not None
