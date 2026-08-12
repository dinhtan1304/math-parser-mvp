"""
Tests for upload/OCR hardening:
  A3 — post-parse question validation/normalization
  B3 — per-user save lock (in-memory fallback serializes)
  B2 — OCR concurrency semaphore serializes heavy engine calls
"""
import asyncio


# ─── A3: validation / normalization ──────────────────────────────────────────

def test_validation_normalizes_grade_difficulty_and_flags_mc():
    from app.services.question_validation import validate_and_normalize_questions

    qs = [
        {"type": "TN", "grade": 99, "difficulty": "easy", "answer": ""},   # bad grade+diff, empty MC
        {"type": "TN", "grade": 8, "difficulty": "VD", "answer": "A"},     # all valid
        {"type": "TL", "grade": "x", "difficulty": "", "answer": ""},      # bad grade+diff, TL (not MC)
    ]
    out, stats, warns = validate_and_normalize_questions(qs)

    assert out[0]["grade"] is None and out[0]["difficulty"] == "TH"
    assert out[1]["grade"] == 8 and out[1]["difficulty"] == "VD"
    assert out[2]["grade"] is None and out[2]["difficulty"] == "TH"

    assert stats["grade_invalid"] == 2
    assert stats["difficulty_normalized"] == 2
    assert stats["empty_answer_mc"] == 1   # only q0 is MC with empty answer
    assert any("đáp án" in w for w in warns)


def test_validation_valid_questions_unchanged():
    from app.services.question_validation import validate_and_normalize_questions

    qs = [{"type": "TN", "grade": 10, "difficulty": "NB", "answer": "B"}]
    out, stats, warns = validate_and_normalize_questions(qs)

    assert out[0] == {"type": "TN", "grade": 10, "difficulty": "NB", "answer": "B"}
    assert stats == {"checked": 1, "grade_invalid": 0, "difficulty_normalized": 0, "empty_answer_mc": 0}
    assert warns == []


# ─── B3: per-user save lock (in-memory fallback) ─────────────────────────────

def test_user_save_lock_serializes_same_user():
    from app.api.parser import user_save_lock

    state = {"cur": 0, "peak": 0}

    async def worker():
        async with user_save_lock(123):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.03)
            state["cur"] -= 1

    async def run():
        await asyncio.wait_for(
            asyncio.gather(*[worker() for _ in range(4)]), timeout=5,
        )

    asyncio.run(run())
    assert state["peak"] == 1  # never two holders at once for the same user


# ─── B2: OCR concurrency semaphore ───────────────────────────────────────────

def test_ocr_semaphore_caps_concurrency():
    from app.services.local_ocr_service import _ocr_limited, OCR_MAX_CONCURRENCY

    state = {"cur": 0, "peak": 0}

    @_ocr_limited
    async def fake_engine():
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return "ok"

    async def run():
        return await asyncio.gather(*[fake_engine() for _ in range(4)])

    results = asyncio.run(run())
    assert results == ["ok"] * 4
    assert state["peak"] <= OCR_MAX_CONCURRENCY   # default 1 → fully serialized
    assert state["peak"] >= 1


# ─── A2: re-extract missing question numbers ─────────────────────────────────

def test_slice_question_region():
    from app.services.ai_parser import _slice_question_region

    text = "Câu 1. Tính 1+1 cho tôi.\nCâu 2. Tính 2+2 cho tôi.\nCâu 3. Tính 3+3 cho tôi."
    r2 = _slice_question_region(text, 2)
    assert r2.startswith("Câu 2")
    assert "Tính 2+2" in r2 and "Câu 3" not in r2

    r3 = _slice_question_region(text, 3)
    assert r3.startswith("Câu 3") and "Tính 3+3" in r3

    assert _slice_question_region(text, 9) == ""   # number not present


def test_recover_missing_questions_targets_only_missing():
    import re
    from app.services.ai_parser import recover_missing_questions

    text = "Câu 1. Tính tổng đầu tiên.\nCâu 2. Tính tổng thứ hai.\nCâu 3. Tính tổng thứ ba."
    calls = []

    async def stub_parse_single(region):
        calls.append(region)
        m = re.search(r"Câu (\d+)", region)
        return [{"question": f"Câu {m.group(1)}. recovered", "answer": "X"}] if m else []

    async def run():
        return await recover_missing_questions(stub_parse_single, text, [2], cap=5)

    recovered = asyncio.run(run())
    assert len(recovered) == 1
    assert recovered[0]["question"].startswith("Câu 2")
    assert len(calls) == 1  # only the missing number was re-parsed


def test_recover_skips_too_short_region():
    from app.services.ai_parser import recover_missing_questions

    async def stub(region):
        return [{"question": "Câu 5. x", "answer": ""}]

    async def run():
        return await recover_missing_questions(stub, "Câu 5. ok", [5], cap=3)

    assert asyncio.run(run()) == []   # region < 20 chars → skipped, no call


# ─── C2: fail-fast on non-retryable Gemini error ─────────────────────────────

def test_gemini_fatal_error_aborts_session_and_skips_remaining():
    from app.services.ai_parser import AIQuestionParser, GeminiFatalError

    p = AIQuestionParser.__new__(AIQuestionParser)  # bypass __init__/client
    p._fatal_error = None
    p._get_semaphore = lambda: asyncio.Semaphore(1)

    async def boom(*a, **kw):
        raise GeminiFatalError("bad key")

    p._call_gemini = boom

    async def run():
        raised = False
        try:
            await p._parse_single("Câu 1. nội dung dài đủ.", chunk_id=0, subject_hint="toan")
        except GeminiFatalError:
            raised = True
        # next chunk is skipped without calling the API
        second = await p._parse_single("Câu 2. nội dung dài đủ.", chunk_id=1, subject_hint="toan")
        return raised, second, p._fatal_error

    raised, second, fatal = asyncio.run(run())
    assert raised is True
    assert second == []
    assert isinstance(fatal, GeminiFatalError)


# ─── C3a: cost calculation + token extraction ────────────────────────────────

def test_cost_usd_and_token_extraction():
    import json
    from app.core.llm_pricing import (
        cost_usd, extract_tokens_from_result_json,
        GEMINI_INPUT_USD_PER_M, GEMINI_OUTPUT_USD_PER_M,
    )

    assert cost_usd(1_000_000, 0) == round(GEMINI_INPUT_USD_PER_M, 4)
    assert cost_usd(0, 1_000_000) == round(GEMINI_OUTPUT_USD_PER_M, 4)
    assert cost_usd(0, 0) == 0.0

    rj = json.dumps({"ingest_stats": {
        "estimated_input_tokens": 1500, "estimated_output_tokens": 2500, "ai_text_calls": 3,
    }})
    assert extract_tokens_from_result_json(rj) == (1500, 2500, 3)
    assert extract_tokens_from_result_json(None) == (0, 0, 0)
    assert extract_tokens_from_result_json("not json") == (0, 0, 0)


# ─── C3b: per-user daily token quota guard at upload ─────────────────────────

def test_upload_blocked_when_daily_token_quota_exceeded(client, make_teacher, monkeypatch):
    import app.api.parser as parser_mod
    from app.core.config import settings as app_settings

    _, headers = make_teacher()

    async def fake_tokens_today(db, uid):
        return 999_999

    monkeypatch.setattr(parser_mod, "_user_tokens_today", fake_tokens_today)
    monkeypatch.setattr(app_settings, "DAILY_TOKEN_QUOTA", 1000)

    r = client.post(
        "/api/v1/parser/parse",
        params={"subject_hint": "toan"},
        files={"file": ("t.pdf", b"%PDF-1.4 minimal", "application/pdf")},
        headers=headers,
    )
    assert r.status_code == 429
    assert "hạn mức" in r.json()["detail"]


def test_upload_allowed_when_quota_disabled(client, make_teacher, monkeypatch):
    """Quota disabled (0) must not block — request proceeds past the guard.

    We stub the heavy parse scheduling so the test stays fast; reaching a
    non-429 status means the quota gate let it through."""
    import app.api.parser as parser_mod
    from app.core.config import settings as app_settings

    _, headers = make_teacher()
    monkeypatch.setattr(app_settings, "DAILY_TOKEN_QUOTA", 0)

    # Stub the heavy background parse so the request returns fast without OCR.
    async def noop_process(*a, **kw):
        return None
    monkeypatch.setattr(parser_mod, "process_file", noop_process)

    r = client.post(
        "/api/v1/parser/parse",
        params={"subject_hint": "toan"},
        files={"file": ("t.pdf", b"%PDF-1.4 minimal content here", "application/pdf")},
        headers=headers,
    )
    assert r.status_code != 429   # not blocked by quota


# ─── A1: keep layout blocks when OCR engine returns none (Paddle fallback) ───

def test_resolve_ocr_blocks_prefers_engine_then_native():
    from app.api.parser import _resolve_ocr_blocks

    ocr = [{"page_num": 0, "bbox": [0, 0, 1, 1], "src": "mineru"}]
    native = [{"page_num": 0, "bbox": [0, 0, 1, 1], "src": "native"}]

    # MinerU blocks present → used as-is
    assert _resolve_ocr_blocks(ocr, native) == ocr
    # Paddle fallback (no engine blocks) → fall back to native geometry
    assert _resolve_ocr_blocks([], native) == native
    assert _resolve_ocr_blocks(None, native) == native
    # Scanned PDF with neither → empty (graceful, no overlay)
    assert _resolve_ocr_blocks([], []) == []
    assert _resolve_ocr_blocks(None, None) == []


# ─── D3: PDF page-count guard at upload ──────────────────────────────────────

def _make_pdf(pages: int) -> bytes:
    import fitz
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    content = doc.tobytes()
    doc.close()
    return content


def test_pdf_page_count_from_bytes():
    from app.api.parser import _pdf_page_count_from_bytes

    assert _pdf_page_count_from_bytes(_make_pdf(3)) == 3
    assert _pdf_page_count_from_bytes(b"not a pdf at all") == 0


def test_upload_rejects_pdf_over_page_limit(client, make_teacher, monkeypatch):
    from app.core.config import settings as app_settings

    _, headers = make_teacher()
    monkeypatch.setattr(app_settings, "MAX_PDF_PAGES", 2)

    r = client.post(
        "/api/v1/parser/parse",
        params={"subject_hint": "toan"},
        files={"file": ("big.pdf", _make_pdf(3), "application/pdf")},
        headers=headers,
    )
    assert r.status_code == 413
    assert "trang" in r.json()["detail"]


def test_upload_allows_pdf_within_page_limit(client, make_teacher, monkeypatch):
    import app.api.parser as parser_mod
    from app.core.config import settings as app_settings

    _, headers = make_teacher()
    monkeypatch.setattr(app_settings, "MAX_PDF_PAGES", 5)

    async def noop_process(*a, **kw):
        return None
    monkeypatch.setattr(parser_mod, "process_file", noop_process)

    r = client.post(
        "/api/v1/parser/parse",
        params={"subject_hint": "toan"},
        files={"file": ("ok.pdf", _make_pdf(2), "application/pdf")},
        headers=headers,
    )
    assert r.status_code != 413   # within limit → not blocked


# ─── B4: drain background indexing tasks on shutdown ─────────────────────────

def test_drain_background_tasks_awaits_pending():
    import app.api.parser as P

    async def run():
        done = []

        async def slow():
            await asyncio.sleep(0.02)
            done.append(1)

        t = asyncio.create_task(slow())
        P._background_tasks.add(t)
        n = await P.drain_background_tasks(timeout=2)
        P._background_tasks.discard(t)
        return n, done, t.done()

    n, done, finished = asyncio.run(run())
    assert n >= 1
    assert done == [1]
    assert finished is True


# ─── E2: AI error classification by structured status code ───────────────────

def test_classify_ai_error_prefers_status_code():
    from app.api.parser import (
        _classify_ai_error,
        AI_ERROR_RATE_LIMIT, AI_ERROR_MAINTENANCE,
        AI_ERROR_NOT_CONFIGURED, AI_ERROR_TIMEOUT,
    )
    from app.services.ai_parser import GeminiFatalError

    class CodedError(Exception):
        def __init__(self, code):
            super().__init__("opaque provider message")
            self.code = code

    assert _classify_ai_error(CodedError(429))[0] == AI_ERROR_RATE_LIMIT
    assert _classify_ai_error(CodedError(503))[0] == AI_ERROR_MAINTENANCE
    assert _classify_ai_error(CodedError(403))[0] == AI_ERROR_NOT_CONFIGURED
    # Our explicit non-retryable wrapper
    assert _classify_ai_error(GeminiFatalError("x"))[0] == AI_ERROR_NOT_CONFIGURED
    # String heuristics still apply when there is no code
    assert _classify_ai_error(Exception("Request timed out"))[0] == AI_ERROR_TIMEOUT
    # Non-AI error → not classified
    assert _classify_ai_error(Exception("some random database error"))[0] is None


# ─── D2: retention cleanup of old uploaded files ─────────────────────────────

def test_cleanup_old_uploads_removes_only_old_top_level_files(tmp_path):
    import os
    import time as _t
    from app.api.parser import cleanup_old_uploads

    d = tmp_path / "uploads"
    d.mkdir()
    old = d / "old_a.pdf"; old.write_bytes(b"x")
    new = d / "new_b.pdf"; new.write_bytes(b"y")
    sub = d / "ocr_artifacts"; sub.mkdir()
    cached = sub / "cache.json"; cached.write_bytes(b"z")

    old_ts = _t.time() - 10 * 86400
    os.utime(old, (old_ts, old_ts))
    os.utime(cached, (old_ts, old_ts))  # old, but inside a sub-dir → must survive

    removed = cleanup_old_uploads(retention_days=7, upload_dir=str(d))
    assert removed == 1
    assert not old.exists()
    assert new.exists()
    assert cached.exists()       # ocr_artifacts cache untouched

    assert cleanup_old_uploads(0, str(d)) == 0   # disabled → no-op
