import os
import uuid
import json
import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional, Dict

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Query, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.future import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import file_handler, ai_parser_service as ai_parser
from app.services.pipeline import step2_preprocess
from app.services.subject_prompts import VALID_SUBJECT_CODES
from app.api import deps
from app.core import content_origin
from app.core.config import settings
from app.db.session import AsyncSessionLocal, get_db
from app.db.models.exam import Exam
from app.db.models.question import Question
from app.db.models.review import DocumentAsset, QuestionDraft, QuestionDraftAsset
from app.db.models.user import User
from app.schemas.review import (
    DraftMergeRequest,
    DraftQuestionPatch,
    DraftQuestionResponse,
    DraftSplitRequest,
    ReviewCommitResponse,
    ReviewPayload,
)

logger = logging.getLogger(__name__)

# Kênh SSE + sổ background task nay nằm ở app/core/progress_bus.py để
# api/ielts_parser.py không phải với sang lấy nội tạng của module này.
# Giữ bí danh dấu gạch dưới cho các chỗ gọi sẵn có trong file (2500+ dòng).
from app.core.progress_bus import (  # noqa: E402
    _background_tasks,
    _progress_queues,
    _sse_redis_bridges,
    drain_background_tasks,
    publish_progress as _publish_progress,
    subscribe as _subscribe,
    track_task,
    unsubscribe as _unsubscribe,
)

RAG_WAIT_TIMEOUT_SECONDS = 20

# Phase 3.5: per-user save lock — serialize dedup+insert cho cùng 1 user để tránh
# TOCTOU race (2 lần parse song song cùng user → câu trùng lọt qua dedup, sinh
# bản trùng không được flag).
_user_save_locks: Dict[int, asyncio.Lock] = {}

# Lock TTL: phải dài hơn thời gian save tối đa để không tự nhả giữa chừng, nhưng
# hữu hạn để worker chết không khoá vĩnh viễn.
_SAVE_LOCK_TTL_S = 180
_SAVE_LOCK_WAIT_S = 90


def _get_user_save_lock(user_id: int) -> asyncio.Lock:
    # setdefault là atomic trong 1 event loop (không await giữa chừng) — tránh
    # race check-then-create tạo 2 lock khác nhau cho cùng user.
    return _user_save_locks.setdefault(user_id, asyncio.Lock())


@asynccontextmanager
async def user_save_lock(user_id: int):
    """Serialize per-user save. Uses a Redis lock when available so it holds
    across multiple workers; falls back to an in-memory asyncio.Lock (single
    process) when Redis is not configured/unreachable."""
    from app.core.redis_client import get_redis

    r = get_redis()
    if r is not None:
        try:
            lock = r.lock(
                f"savelock:user:{user_id}",
                timeout=_SAVE_LOCK_TTL_S,
                blocking_timeout=_SAVE_LOCK_WAIT_S,
            )
            acquired = await lock.acquire()
            try:
                yield
            finally:
                if acquired:
                    try:
                        await lock.release()
                    except Exception:
                        pass  # already expired / released
            return
        except Exception as e:
            logger.warning("Redis save lock failed, using in-memory: %s", e)

    async with _get_user_save_lock(user_id):
        yield


def cleanup_old_uploads(retention_days: int, upload_dir: str = "uploads") -> int:
    """D2: delete original uploaded files older than ``retention_days`` (by mtime).

    Only top-level files in ``upload_dir`` are removed — sub-directories such as
    ``ocr_artifacts/`` (the file-hash OCR cache) are left intact so re-uploading
    the same document still skips OCR. Best-effort; returns the count removed.
    """
    import time as _t

    if retention_days <= 0:
        return 0
    cutoff = _t.time() - retention_days * 86400
    removed = 0
    try:
        entries = list(os.scandir(upload_dir))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("cleanup_old_uploads: removed %d file(s) older than %d days", removed, retention_days)
    return removed


def _pdf_page_count_from_bytes(content: bytes) -> int:
    """Best-effort PDF page count from raw bytes (0 if not a parseable PDF).
    Used by the D3 upload page-limit guard to fail fast before saving/OCR."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=content, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return 0


def _resolve_ocr_blocks(ocr_blocks: list | None, native_blocks: list | None) -> list:
    """A1: choose layout blocks for question segmentation.

    OCR-engine blocks (MinerU content_list, with per-element bbox) win. When the
    engine returns none — notably the PaddleOCR-VL fallback, which is markdown-
    only — fall back to the engine-INDEPENDENT native geometry captured straight
    from the PDF, so questions keep page_num/bbox and the review overlay survives
    instead of silently degrading.
    """
    if ocr_blocks:
        return ocr_blocks
    if native_blocks:
        return native_blocks
    return []


async def _user_tokens_today(db: AsyncSession, user_id: int) -> int:
    """Sum estimated input+output AI tokens across the user's exams created
    today (UTC). Used by the per-user daily token quota guard."""
    from app.core.llm_pricing import extract_tokens_from_result_json

    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await db.execute(
        select(Exam.result_json).where(
            Exam.user_id == user_id,
            Exam.created_at >= since,
        )
    )
    total = 0
    for (result_json,) in rows.all():
        inp, out, _ = extract_tokens_from_result_json(result_json)
        total += inp + out
    return total

router = APIRouter()

# Đường dẫn upload nay ở app/core/upload_paths.py — dùng chung với ielts_parser.
from app.core.upload_paths import UPLOAD_DIR, OCR_REVIEW_DIR  # noqa: E402

# OCR review step (PaddleOCR-style PDF|markdown). The OCR phase writes the
# editable markdown + the parse context here; the user edits it in /upload/ocr,
# then a parse-trigger re-runs process_file which picks up the edited markdown.
os.makedirs(OCR_REVIEW_DIR, exist_ok=True)
# Gate: insert a markdown-review step after OCR (default on for K12 uploads).
OCR_REVIEW_STEP = os.getenv("OCR_REVIEW_STEP", "1").strip().lower() not in {"0", "false", "no", "off"}


def _ocr_review_md_path(exam_id: int) -> str:
    return os.path.join(OCR_REVIEW_DIR, f"{exam_id}.md")


def _ocr_review_json_path(exam_id: int) -> str:
    return os.path.join(OCR_REVIEW_DIR, f"{exam_id}.json")


def _read_ocr_review_md(exam_id: int) -> Optional[str]:
    """Return the edited markdown if an OCR-review file exists (→ we're resuming
    a parse from user-corrected markdown), else None (fresh OCR run)."""
    path = _ocr_review_md_path(exam_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Exam %s: read ocr_review md failed: %s", exam_id, exc)
    return None


def _load_review_json(exam_id: int) -> dict:
    """Load the persisted parse context (layout_snapshot, ocr meta) for resume."""
    path = _ocr_review_json_path(exam_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("Exam %s: read ocr_review json failed: %s", exam_id, exc)
    return {}


def _write_ocr_review(
    exam_id: int,
    *,
    markdown: str,
    ocr_result: dict,
    layout_snapshot: dict,
    ingest_stats: dict,
    ingest_warnings: list,
    file_hash: str,
    page_count: int,
) -> None:
    """Persist the editable markdown (.md) + parse context (.json) for the review step."""
    try:
        with open(_ocr_review_md_path(exam_id), "w", encoding="utf-8") as f:
            f.write(markdown or "")
        ctx = {
            "blocks": ocr_result.get("blocks", []),
            "figures": ocr_result.get("figures", []),
            "image_map": ocr_result.get("image_map", {}),
            "method": ocr_result.get("method"),
            "layout_source": ocr_result.get("layout_source"),
            "layout_snapshot": layout_snapshot,
            "ingest_stats": ingest_stats,
            "ingest_warnings": ingest_warnings,
            "file_hash": file_hash,
            "page_count": page_count,
        }
        with open(_ocr_review_json_path(exam_id), "w", encoding="utf-8") as f:
            json.dump(ctx, f, ensure_ascii=False)
    except Exception as exc:
        logger.error("Exam %s: write ocr_review failed: %s", exam_id, exc)
        raise


def _cleanup_ocr_review(exam_id: int) -> None:
    for path in (_ocr_review_md_path(exam_id), _ocr_review_json_path(exam_id)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ── SSE one-time tokens (replaces JWT in URL) ──
# Cơ chế phát/nghe sự kiện SSE đã chuyển sang app/core/progress_bus.py (import
# ở đầu file). Phần token dùng-một-lần dưới đây vẫn thuộc về router vì nó gắn
# với xác thực HTTP, không phải với việc truyền sự kiện.
import time as _time
import secrets as _secrets
from app.core.redis_client import get_redis
# In-memory fallback store: {token_str: (user_id, job_id, created_at)}
# When Redis is configured the token lives in Redis instead (key "sse:{token}").
_sse_tokens: Dict[str, tuple] = {}
_SSE_TOKEN_TTL = 300  # 5 minutes


# ==================== Response Models ====================

# ParseResponse chuyển sang app/schemas/parser.py để ielts_parser không phải
# import model từ router này.
from app.schemas.parser import ParseResponse  # noqa: E402,F401


class ExamResponse(BaseModel):
    id: int
    filename: str
    status: str
    created_at: datetime
    result_json: Optional[str] = None
    error_message: Optional[str] = None
    question_count: Optional[int] = None  # populated by history endpoint
    warnings: Optional[List[str]] = None
    ingest_stats: Optional[dict] = None
    # Nhãn nguồn gốc nội dung — FE hiển thị badge AI (Điều 44 Luật CN CNS)
    origin: str = "HUMAN"
    ai_model: Optional[str] = None
    reviewed_by_user: bool = False

    class Config:
        from_attributes = True


class ExamListResponse(BaseModel):
    items: List[ExamResponse]
    total: int
    page: int
    page_size: int


# ==================== Helpers ====================

# AI error types — used by FE to show appropriate UI
AI_ERROR_NOT_CONFIGURED = "ai_not_configured"
AI_ERROR_RATE_LIMIT     = "ai_rate_limit"
AI_ERROR_MAINTENANCE    = "ai_maintenance"
AI_ERROR_TIMEOUT        = "ai_timeout"
AI_ERROR_CONTENT        = "ai_content_blocked"
AI_ERROR_NETWORK        = "ai_network"


def _classify_ai_error(e: Exception) -> tuple[str, str] | tuple[None, None]:
    """Phân loại lỗi AI và trả về (error_type, thông báo thân thiện).

    Trả về (None, None) nếu không phải lỗi AI — để caller xử lý như lỗi thường.

    error_type được đưa vào SSE event 'error_event' để FE hiển thị đúng UI:
      - ai_not_configured  → hướng dẫn admin cấu hình API key
      - ai_rate_limit      → thông báo quá tải, thử lại sau
      - ai_maintenance     → dịch vụ bảo trì, thử lại sau
      - ai_timeout         → timeout, thử lại
      - ai_content_blocked → nội dung bị lọc bởi bộ lọc an toàn
      - ai_network         → lỗi kết nối mạng
    """
    # E2: prefer the structured HTTP status code when present (google.genai
    # APIError exposes `.code`) — robust to provider message wording changes.
    # Falls through to the string heuristics below for non-SDK errors.
    _code = getattr(e, "code", None)
    if _code is None:
        _code = getattr(getattr(e, "response", None), "status_code", None)
    try:
        _code = int(_code) if _code is not None else None
    except (TypeError, ValueError):
        _code = None
    if _code == 429:
        return AI_ERROR_RATE_LIMIT, (
            "Dịch vụ AI đang quá tải (rate limit). Vui lòng thử lại sau 1–2 phút."
        )
    if _code is not None and 500 <= _code < 600:
        return AI_ERROR_MAINTENANCE, (
            "Dịch vụ AI đang bảo trì. Vui lòng thử lại sau ít phút."
        )
    if _code in {400, 401, 403}:
        return AI_ERROR_NOT_CONFIGURED, (
            "Dịch vụ AI không khả dụng hoặc API key/quota không hợp lệ. "
            "Vui lòng liên hệ quản trị viên."
        )
    try:
        from app.services.ai_parser import GeminiFatalError
        if isinstance(e, GeminiFatalError):
            return AI_ERROR_NOT_CONFIGURED, (
                "Dịch vụ AI không khả dụng hoặc API key/quota không hợp lệ. "
                "Vui lòng liên hệ quản trị viên."
            )
    except Exception:
        pass

    err = str(e)
    err_lower = err.lower()

    # API key chưa cấu hình
    if (
        "google_api_key" in err_lower
        or "chưa được cấu hình" in err
        or "api key" in err_lower
        or "no google" in err_lower
    ):
        return AI_ERROR_NOT_CONFIGURED, (
            "Dịch vụ AI chưa được cấu hình. "
            "Vui lòng liên hệ quản trị viên để thiết lập Google API key."
        )

    # Quá tải / hết quota (429 / RESOURCE_EXHAUSTED)
    if (
        "429" in err
        or "resource_exhausted" in err_lower
        or "quota" in err_lower
        or "rate limit" in err_lower
        or "too many requests" in err_lower
    ):
        return AI_ERROR_RATE_LIMIT, (
            "Dịch vụ AI đang quá tải (rate limit). "
            "Vui lòng thử lại sau 1–2 phút."
        )

    # Dịch vụ tạm dừng (503 / SERVICE_UNAVAILABLE / UNAVAILABLE)
    if (
        "503" in err
        or "service_unavailable" in err_lower
        or "unavailable" in err_lower
        or "server error" in err_lower
        or "internal error" in err_lower
        or "500" in err
    ):
        return AI_ERROR_MAINTENANCE, (
            "Dịch vụ AI đang bảo trì. "
            "Vui lòng thử lại sau ít phút."
        )

    # Timeout / deadline exceeded
    if (
        "timeout" in err_lower
        or "deadline" in err_lower
        or "timed out" in err_lower
        or "deadline_exceeded" in err_lower
    ):
        return AI_ERROR_TIMEOUT, (
            "Dịch vụ AI phản hồi quá chậm (timeout). "
            "File có thể quá dài — hãy thử lại hoặc dùng file nhỏ hơn."
        )

    # Nội dung bị chặn bởi safety filter
    if (
        "safety" in err_lower
        or "blocked" in err_lower
        or "harm" in err_lower
        or "recitation" in err_lower
    ):
        return AI_ERROR_CONTENT, (
            "Nội dung file bị chặn bởi bộ lọc an toàn AI. "
            "Vui lòng kiểm tra lại nội dung đề thi."
        )

    # Lỗi kết nối mạng
    if (
        "connection" in err_lower
        or "network" in err_lower
        or "dns" in err_lower
        or "connect" in err_lower
        or "ssl" in err_lower
    ):
        return AI_ERROR_NETWORK, (
            "Không thể kết nối đến dịch vụ AI. "
            "Kiểm tra kết nối mạng của server hoặc thử lại sau."
        )

    return None, None  # Không phải lỗi AI


def _parser_for_speed(speed: str):
    """Compatibility shim for older tests that patch this symbol."""
    return ai_parser


def _is_mock_result(questions: list) -> bool:
    """Detect if cached result was from mock parser (regex garbage).

    Mock parser signs:
    - All topics are "Toán học" (hardcoded default)
    - No grade/chapter/lesson data
    - No solution_steps
    """
    if not questions or len(questions) == 0:
        return True

    # Phase 3.6: guard mẫu quá nhỏ — cache <3 câu không đủ tin cậy → coi như mock,
    # buộc parse lại thay vì tin cache mỏng.
    if len(questions) < 3:
        return True

    # Check first 5 questions
    sample = questions[:5]
    mock_signs = 0
    max_signs = len(sample) * 3  # 3 dấu hiệu / câu

    for q in sample:
        topic = q.get("topic", "")
        grade = q.get("grade")
        chapter = q.get("chapter", "")
        steps = q.get("solution_steps", [])

        if topic == "Toán học":
            mock_signs += 1
        if not grade and not chapter:
            mock_signs += 1
        if not steps:
            mock_signs += 1

    # Phase 3.6: nâng ngưỡng ≥75% dấu hiệu mock (cũ là >2/câu ≈ 53% → quá lỏng,
    # data kém vẫn lọt). Mock thật sự sẽ chạm gần như mọi dấu hiệu.
    return mock_signs >= max_signs * 0.75


def _is_text_poor_quality(text: str) -> bool:
    """Heuristic: check if extracted text is garbage or has broken formulas.

    v3: Multi-subject — detects K12 documents broadly (not just math).
    """
    if not text or len(text) < 50:
        return True

    # Quick check: K12 document markers (multi-subject)
    doc_markers = [
        # Structure markers
        'Câu', 'Bài', 'câu', 'bài', 'Question', 'Đề',
        # Math/science
        '=', '+', '-', '/', '^', '²', '³',
        'sin', 'cos', 'sqrt', 'frac',
        # Chemistry
        'mol', 'pH', 'Fe', 'Cu', 'NaOH', 'HCl', 'H₂', 'O₂',
        'phản ứng', 'dung dịch', 'nguyên tử', 'phân tử',
        # Literature / general
        'đọc', 'viết', 'nghị luận', 'đoạn văn', 'tác phẩm',
        # English
        'Read', 'Choose', 'answer', 'passage',
        # Science
        'thí nghiệm', 'hiện tượng', 'năng lượng',
    ]
    marker_count = sum(1 for m in doc_markers if m in text)
    if marker_count < 3:
        return True

    # Too many garbled characters
    garbled = sum(1 for c in text if ord(c) > 0xFFFF or (ord(c) < 32 and c not in '\n\r\t'))
    if len(text) > 0 and garbled / len(text) > 0.1:
        return True

    # Deep structure analysis (math-specific — only triggers for math docs)
    try:
        analysis = file_handler.analyze_math_quality(text)
        if analysis.get("should_use_vision"):
            logger.info(
                f"Text quality analysis: score={analysis['score']}, "
                f"reason={analysis['reason']}"
            )
            return True
    except Exception as e:
        logger.debug(f"Text quality analysis failed: {e}")

    return False


async def _save_questions_to_bank(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
    questions: list,
    *,
    reviewed: bool = False,
):
    """Tách parsed questions thành individual Question records.

    Chạy sau khi parse thành công. Nếu exam đã có questions (re-parse),
    xóa cũ rồi insert mới.

    Sprint 3, Task 22: Duplicate detection via content_hash.
    """
    from app.db.models.question import _question_hash

    try:
        # Xóa questions cũ nếu re-parse — commit ngay để batch save sau này
        # không bị cuốn theo rollback nếu 1 batch lỗi (Sprint 4 defensive save).
        from sqlalchemy import delete
        await db.execute(
            delete(Question).where(Question.exam_id == exam_id)
        )
        await db.commit()

        # ── Task 22: Pre-compute hashes and check duplicates ──
        # Curriculum matching: map AI grade/chapter to DB entries
        try:
            from app.services.curriculum_matcher import match_questions_to_curriculum
            questions = await match_questions_to_curriculum(db, questions)
        except Exception as e:
            logger.warning(f"Curriculum matching skipped: {e}")

        new_questions = []
        skipped_empty = 0
        for i, q in enumerate(questions):
            q_text = q.get("question", "")
            if not q_text.strip():
                skipped_empty += 1
                logger.warning(
                    "Exam %s: skip empty question (idx=%d, type=%s, answer=%r)",
                    exam_id, i, q.get("type"), q.get("answer"),
                )
                continue
            c_hash = _question_hash(q_text)
            new_questions.append((i, q, c_hash))

        # ── B1: Cross-exam duplicate detection ──
        # Query existing content_hashes in user's bank (excluding this exam)
        new_hashes = [c_hash for _, _, c_hash in new_questions]
        existing_db_hashes: set = set()
        if new_hashes:
            try:
                existing_result = await db.execute(
                    select(Question.content_hash).filter(
                        Question.user_id == user_id,
                        Question.content_hash.in_(new_hashes),
                        Question.exam_id != exam_id,
                    )
                )
                existing_db_hashes = set(row[0] for row in existing_result.fetchall())
            except Exception as e:
                logger.debug(f"Exam {exam_id}: cross-exam hash check failed: {e}")

        # Intra-batch dedup set
        seen_hashes: set = set()

        # Build list of pending Question objects (chưa add vào session)
        pending: list[Question] = []
        skipped = 0
        dup_count = 0
        for i, q, c_hash in new_questions:
            if c_hash in seen_hashes:
                skipped += 1
                logger.warning(
                    "Exam %s: skip intra-batch dup hash=%s text=%r",
                    exam_id, c_hash[:12], q.get("question", "")[:80],
                )
                continue

            is_dup = c_hash in existing_db_hashes

            _bbox = q.get("bbox")
            pending.append(Question(
                exam_id=exam_id,
                user_id=user_id,
                question_text=q.get("question", ""),
                content_hash=c_hash,
                subject_code=q.get("subject", "toan"),
                question_type=q.get("type"),
                topic=None,
                difficulty=q.get("difficulty"),
                grade=q.get("grade"),
                chapter=q.get("chapter"),
                lesson_title=q.get("lesson_title"),
                answer=q.get("answer"),
                answer_source=q.get("answer_source"),
                solution_steps=json.dumps(q.get("solution_steps", []), ensure_ascii=False),
                question_order=i + 1,
                is_public=False,
                is_bank_duplicate=is_dup,
                page_num=q.get("page_num"),
                bbox_json=json.dumps(list(_bbox), ensure_ascii=False) if _bbox else None,
                # Trích xuất từ tài liệu người dùng tải lên qua OCR + parse.
                # `reviewed=True` khi giáo viên đã duyệt draft trong màn review.
                origin=content_origin.AI_ASSISTED if reviewed else content_origin.OCR_IMPORT,
                ai_model=settings.GEMINI_MODEL,
                reviewed_by_user=reviewed,
                reviewed_at=datetime.now(timezone.utc) if reviewed else None,
            ))
            seen_hashes.add(c_hash)
            if is_dup:
                dup_count += 1

        # Sprint 4 — Defensive batch DB save: commit theo lô để 1 lỗi không
        # nuốt toàn bộ. Batch size env-configurable (default 50).
        try:
            batch_size = max(1, int(os.getenv("QUESTION_SAVE_BATCH_SIZE", "50")))
        except (TypeError, ValueError):
            batch_size = 50

        saved = 0
        batch_failures = 0
        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start:batch_start + batch_size]
            try:
                for obj in batch:
                    db.add(obj)
                await db.commit()
                saved += len(batch)
            except Exception as batch_err:
                # Rollback BATCH này, log, continue với batch sau
                batch_failures += 1
                logger.warning(
                    "Exam %s: batch %d-%d save failed (%s) — continuing with remaining batches",
                    exam_id, batch_start, batch_start + len(batch),
                    str(batch_err)[:120],
                )
                try:
                    await db.rollback()
                except Exception:
                    pass

        if batch_failures:
            logger.error(
                "Exam %s: %d/%d batches FAILED to commit (saved %d/%d questions)",
                exam_id, batch_failures, (len(pending) + batch_size - 1) // batch_size,
                saved, len(pending),
            )

        if skipped or dup_count or skipped_empty or batch_failures:
            logger.info(
                "Exam %s: Saved %d/%d, skipped %d empty, %d intra-batch dupes, %d cross-exam dupes, %d batch failures",
                exam_id, saved, len(pending), skipped_empty, skipped, dup_count, batch_failures,
            )
        else:
            logger.info(f"Exam {exam_id}: Saved {saved} questions to bank")

        # Get saved question IDs for FTS + vector indexing
        result = await db.execute(
            select(Question.id).where(Question.exam_id == exam_id)
        )
        saved_ids = [row[0] for row in result.fetchall()]

        # OPT: FTS + embedding as truly non-blocking background tasks.
        # FIX: _save_questions_to_bank is called inside `async with AsyncSessionLocal() as db`
        # in process_file. The task outlives that context, so we open a NEW session.
        async def _index_in_background(ids):
            # Each operation uses its own session — a failure in one (e.g. FTS on
            # PostgreSQL) does NOT poison the transaction for subsequent operations.
            from app.db.session import AsyncSessionLocal

            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.fts import sync_fts_questions
                    await sync_fts_questions(_db, ids)
                    logger.info(f"Exam {exam_id}: FTS indexed {len(ids)} questions")
            except Exception as e:
                logger.warning(f"Exam {exam_id}: FTS sync failed: {e}")

            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.vector_search import embed_questions
                    await embed_questions(_db, ids)
                    logger.info(f"Exam {exam_id}: Embedded {len(ids)} questions")
            except Exception as e:
                logger.warning(f"Exam {exam_id}: Embedding failed: {e}")

            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.similarity_detector import detect_similar_for_exam
                    found = await detect_similar_for_exam(_db, exam_id, user_id)
                    if found:
                        logger.info(f"Exam {exam_id}: {found} similar question pairs detected")
            except Exception as e:
                logger.warning(f"Exam {exam_id}: Similarity detection failed: {e}")

            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.difficulty_inferrer import infer_difficulty_for_exam
                    inferred = await infer_difficulty_for_exam(_db, exam_id, user_id)
                    if inferred:
                        logger.info(f"Exam {exam_id}: difficulty inferred for {inferred} questions")
            except Exception as e:
                logger.warning(f"Exam {exam_id}: Difficulty inference failed: {e}")

            # Document RAG is handled by the foreground hybrid ingest branch in
            # process_file so completion can include warnings and ingest stats.

        # Keep reference to prevent garbage collection (Python asyncio gotcha)
        task = asyncio.create_task(_index_in_background(saved_ids))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        return saved, skipped, dup_count

    except Exception as e:
        logger.error(f"Exam {exam_id}: Failed to save questions to bank: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return 0, 0, 0


async def process_file(exam_id: int, speed: str = "balanced", use_vision: bool = False, subject_hint: Optional[str] = None, ocr_engine: str = "auto", request_id: Optional[str] = None, review_markdown: bool = False):
    """Background task: extract text from file and parse with AI.

    v3: Short-lived DB sessions to survive Neon idle timeout.
    Old approach: open session → 2min AI processing → save → connection dead.
    New approach: open/close session for each DB operation.

    ``request_id`` (E1) is the originating HTTP request's X-Request-ID, logged so
    background-task logs can be correlated with the upload request.
    """
    logger.info("process_file start: exam=%s req=%s subject=%s engine=%s", exam_id, request_id, subject_hint, ocr_engine)
    try:
        # ── Phase 1: Read exam info (short DB session) ──
        ingest_warnings: list[str] = []
        ingest_stats: dict = {
            "questions_saved": 0,
            "question_embeddings": 0,
            "document_chunks": 0,
            "rag_index_status": "pending",
        }
        rag_task: Optional[asyncio.Task] = None
        # Phase 3.2: exam kết thúc với 0 câu (no_questions_detected) → đánh dấu
        # needs_review thay vì "completed" im lặng để user biết cần rà soát.
        final_status = "completed"

        file_path = None
        user_id = None
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = result.scalars().first()
                if not exam:
                    return
                file_path = exam.file_path
                user_id = exam.user_id
                exam.status = "processing"
                await db.commit()
        except Exception as e:
            logger.error(f"Exam {exam_id}: Failed to read exam: {e}")
            return

        # ── Phase 2: OCR (route to backend by subject) ──
        _publish_progress(exam_id, "progress", {"percent": 10, "message": "Đang OCR file..."})

        async def _run_document_rag_branch(markdown_override=None):
            async with AsyncSessionLocal() as rag_db:
                from app.services.hybrid_ingest import index_document_for_rag
                return await index_document_for_rag(
                    rag_db,
                    file_path=file_path,
                    exam_id=exam_id,
                    user_id=user_id,
                    subject_code=subject_hint or "toan",
                    grade=None,
                    markdown_override=markdown_override,
                )

        try:
            from app.services.local_ocr_service import extract_local_ocr_artifact
            from app.services.marker_ocr import _estimate_page_count

            # Sprint 7.1 + hotfix: adaptive OCR timeout dùng REAL page count
            # (mở PDF qua PyMuPDF để đếm — fast). Marker với Surya texify ~50s/page
            # cho math-heavy đề HSG. Base 180s + 90s/page, cap 30 phút.
            _ocr_base = int(os.getenv("OCR_TOTAL_TIMEOUT_BASE", "180"))
            _ocr_per_page = int(os.getenv("OCR_TOTAL_TIMEOUT_PER_PAGE", "90"))
            _ocr_cap = int(os.getenv("OCR_TOTAL_TIMEOUT_CAP", "1800"))
            real_pages = _estimate_page_count(file_path)  # actual count via fitz, fallback=1
            ocr_timeout = min(_ocr_cap, _ocr_base + _ocr_per_page * real_pages)
            logger.info(
                "OCR adaptive timeout: %ds (real_pages=%d, base=%d + per_page=%d*pages)",
                ocr_timeout, real_pages, _ocr_base, _ocr_per_page,
            )
            # Budget truyền XUỐNG engine (subprocess MinerU bị kill đúng hạn qua
            # process-tree kill; Paddle API ngừng poll). wait_for chỉ là backstop
            # với grace 120s — nếu nó nổ nghĩa là engine không tự thoát được
            # (vd Paddle local in-process); semaphore khi đó được giữ đến khi
            # thread thật sự xong (_ocr_limited) để upload sau không chạy chồng.
            ocr_artifact = await asyncio.wait_for(
                extract_local_ocr_artifact(
                    file_path, subject_hint or "toan", engine=ocr_engine, timeout=ocr_timeout
                ),
                timeout=ocr_timeout + 120,
            )
        except asyncio.TimeoutError:
            raise ValueError(
                f"Local OCR qua thoi gian ({ocr_timeout}s) cho file {real_pages} trang. "
                "OCR engine khong tu thoat dung han. "
                "Tang env OCR_TOTAL_TIMEOUT_PER_PAGE (mac dinh 90s/page)."
            )

        extracted_text = ocr_artifact.get("text", "")
        file_hash = ocr_artifact.get("file_hash", "")
        # OCR-review resume: nếu user đã sửa markdown (file review tồn tại) → dùng
        # bản đã sửa để parse. Blocks/layout vẫn lấy từ artifact cache (re-OCR cache
        # hit), nên ta CHỈ override text. _resuming cũng bỏ qua RAG + capture (đã
        # chạy ở lần OCR đầu) để khỏi index/render trùng.
        _edited_md = _read_ocr_review_md(exam_id)
        _resuming = _edited_md is not None
        if _resuming:
            extracted_text = _edited_md
        ocr_result = {
            "text": extracted_text,
            "image_map": ocr_artifact.get("image_map", {}),
            "method": ocr_artifact.get("method", "local-ocr"),
            "blocks": ocr_artifact.get("blocks", []),
            "figures": ocr_artifact.get("figures", []),
            "layout_source": ocr_artifact.get("layout_source"),
        }
        ingest_warnings.extend(ocr_artifact.get("warnings") or [])
        ingest_stats.update({
            "ocr_method": ocr_artifact.get("method"),
            "ocr_fallbacks_used": ocr_artifact.get("fallbacks_used", []),
            "ocr_quality_score": ocr_artifact.get("quality_score"),
            "ocr_chars": ocr_artifact.get("char_count", 0),
            "ocr_pages": ocr_artifact.get("page_count", 0),
            "cache_hit": bool(ocr_artifact.get("cache_hit")),
            "ai_text_calls": 0,
            "ai_vision_calls": 0,
            # Sprint 5 — math optimization: expose LaTeX quality cho FE/audit.
            "latex_quality": ocr_artifact.get("latex_quality"),
            # Formula fidelity pilot: diagnostics only, public question schema unchanged.
            "native_text_math_audit": ocr_artifact.get("native_text_math_audit"),
            "formula_audit": ocr_artifact.get("formula_audit"),
            "ocr_document_status": ocr_artifact.get("document_status"),
            "ocr_publishable": ocr_artifact.get("publishable"),
        })

        # RAG index: fresh run dùng OCR artifact; resume (đã sửa markdown) thì
        # RE-INDEX từ markdown đã sửa (delete chunks cũ + chunk lại).
        if file_path:
            _rag_md_override = extracted_text if _resuming else None
            rag_task = asyncio.create_task(_run_document_rag_branch(_rag_md_override))
            _background_tasks.add(rag_task)
            rag_task.add_done_callback(_background_tasks.discard)
            _publish_progress(exam_id, "progress", {
                "percent": 18,
                "branch": "rag",
                "message": ("Đang re-index RAG từ markdown đã sửa..." if _resuming
                            else "Dang index tai lieu cho RAG tu OCR artifact..."),
            })

        # C4: file_hash đã có từ ocr_artifact — không cần re-compute.
        # Fallback only nếu artifact thiếu (corner-case khi OCR cache hit cũ).
        if not file_hash:
            try:
                file_hash = await file_handler._compute_hash(file_path)
            except Exception:
                pass

        # Save file hash (short DB session)
        if file_hash:
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        sa_text("UPDATE exam SET file_hash = :hash WHERE id = :eid"),
                        {"hash": file_hash, "eid": exam_id}
                    )
                    await db.commit()
            except Exception as e:
                logger.debug(f"Exam {exam_id}: file_hash save failed: {e}")

        logger.info(
            f"Exam {exam_id}: OCR done — {len(extracted_text)} chars, "
            f"method={ocr_result.get('method')}, subject={subject_hint}"
        )

        # Capture page renders + native layout geometry for review. OCR text remains
        # authoritative for math-heavy Marker output; geometry is layered on top.
        # On a parse resume (_resuming) the assets + layout already exist from the
        # first OCR run — re-running capture_pdf_layout would DUPLICATE page assets,
        # so we reload the persisted layout_snapshot instead.
        layout_snapshot = {"pages": [], "blocks": [], "figures": [], "warnings": []}
        if _resuming:
            layout_snapshot = _load_review_json(exam_id).get("layout_snapshot") or layout_snapshot
        else:
            try:
                from app.services.layout_assets import capture_pdf_layout, persist_ocr_image_map
                async with AsyncSessionLocal() as layout_db:
                    captured = await capture_pdf_layout(
                        layout_db,
                        exam_id=exam_id,
                        user_id=user_id,
                        file_path=file_path,
                    )
                    placeholder_figures = await persist_ocr_image_map(
                        layout_db,
                        exam_id=exam_id,
                        user_id=user_id,
                        image_map=ocr_result.get("image_map", {}),
                        figure_hints=ocr_result.get("figures", []),
                    )
                    await layout_db.commit()
                layout_snapshot = {
                    "pages": captured.pages,
                    "blocks": captured.blocks,
                    "figures": captured.figures + placeholder_figures,
                    "warnings": captured.warnings,
                }
            except Exception as layout_err:
                logger.warning("Exam %s: layout capture skipped: %s", exam_id, layout_err)
                layout_snapshot["warnings"].append(f"layout_capture_skipped:{layout_err}")

        # A1: reuse engine-independent native geometry when the OCR engine gave
        # no blocks (e.g. PaddleOCR-VL fallback) so segmentation keeps bbox.
        _chosen_blocks = _resolve_ocr_blocks(
            ocr_result.get("blocks"), layout_snapshot.get("blocks")
        )
        ocr_result["blocks"] = _chosen_blocks
        layout_snapshot["blocks"] = _chosen_blocks

        # Phase 3.10: cờ cho FE biết layout/bbox overlay có dùng được không.
        # Capture fail → pages rỗng → FE tắt overlay thay vì hiện trống im lặng.
        ingest_stats["layout_available"] = bool(layout_snapshot.get("pages"))

        # ── Phase 3: markdown MinerU (kèm LaTeX) → Gemini parse JSON ──
        if not extracted_text.strip():
            raise ValueError(
                "Local OCR khong doc duoc noi dung file. Kiem tra MinerU/PaddleOCR-VL "
                "hoac thu file scan ro hon."
            )

        # ── OCR review gate (fresh run only): dừng lại để user xem/sửa markdown ──
        # (PDF | markdown editable, giống web PaddleOCR). Persist markdown + parse
        # context, set status=ocr_review, FE mở /upload/ocr/[id]. Khi user bấm "Tạo
        # câu hỏi" → parse_exam_from_markdown re-run process_file (lúc đó _resuming).
        if review_markdown and not _resuming:
            _write_ocr_review(
                exam_id,
                markdown=extracted_text,
                ocr_result=ocr_result,
                layout_snapshot=layout_snapshot,
                ingest_stats=ingest_stats,
                ingest_warnings=ingest_warnings,
                file_hash=file_hash,
                page_count=int(ocr_artifact.get("page_count", 0) or 0),
            )
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = r.scalars().first()
                if exam:
                    exam.status = "ocr_review"
                    exam.layout_json = json.dumps(layout_snapshot, ensure_ascii=False)
                    await db.commit()
            _publish_progress(exam_id, "complete", {
                "message": "OCR xong — xem lại / sửa markdown trước khi tạo câu hỏi.",
                "exam_id": exam_id,
                "status": "ocr_review",
                "needs_review": False,
                "ingest_stats": ingest_stats,
                "warnings": ingest_warnings,
            })
            logger.info("Exam %s: OCR review gate → status=ocr_review", exam_id)
            return

        _publish_progress(exam_id, "progress", {"percent": 35, "message": "Đang chuẩn hoá nội dung OCR..."})

        # step2_preprocess CHỈ còn để ước lượng số câu (quality_report) + shadow
        # diagnostics — KHÔNG gate luồng nữa.
        try:
            structured = step2_preprocess(ocr_result)
        except Exception as _seg_err:
            logger.warning("Exam %s: step2_preprocess diagnostics skipped: %s", exam_id, _seg_err)
            structured = []
        if ocr_result.get("segmentation_report"):
            ingest_stats["segmentation_report"] = ocr_result["segmentation_report"]
            ingest_warnings.extend(ocr_result["segmentation_report"].get("warnings") or [])

        # ── Tách phần ĐỀ và phần ĐÁP ÁN/HƯỚNG DẪN CHẤM (deterministic, nhẹ) ──
        # Lý do: đề thi VN lặp "Câu N" ở cả phần đề lẫn phần đáp án → nếu đưa CẢ
        # tài liệu cho Gemini, nó hay coi đáp án "Câu N" là câu hỏi riêng (đề/đáp án
        # bị tách thành 2 câu). Vì vậy: chỉ gửi phần ĐỀ cho Gemini parse câu hỏi
        # (input ngắn hơn → JSON ổn định hơn), rồi map lời giải/đáp án vào theo
        # cau_num. Chỉ split ở tiêu đề SECTION (không segment từng câu bằng regex).
        from app.services.pipeline import (
            _split_question_and_answer_key,
            _parse_answer_key_by_cau_num,
            _solution_to_steps as _ak_to_steps,
            _extract_final_answer_from_solution as _ak_final_answer,
        )
        question_md, answer_key_md = _split_question_and_answer_key(extracted_text)
        gemini_input = question_md if question_md.strip() else extracted_text
        answer_key_map = _parse_answer_key_by_cau_num(answer_key_md) if answer_key_md.strip() else {}
        if answer_key_map:
            logger.info("Exam %s: tách answer-key section, %d câu có lời giải", exam_id, len(answer_key_map))

        # ── Phase 3b: Cache theo file_hash (tái dùng kết quả Gemini cũ) ──
        questions = None
        if file_hash and ai_parser._client:
            try:
                async with AsyncSessionLocal() as db:
                    cache_result = await db.execute(
                        select(Exam.result_json).filter(
                            Exam.file_hash == file_hash,
                            Exam.status == "completed",
                            Exam.result_json.isnot(None),
                            Exam.id != exam_id,
                        ).order_by(Exam.created_at.desc()).limit(1)
                    )
                    cached_json = cache_result.scalar()
                    if cached_json:
                        try:
                            from app.services.hybrid_ingest import extract_questions_payload
                            cached_questions = extract_questions_payload(cached_json)
                            if cached_questions is None:
                                cached_questions = json.loads(cached_json)
                            if cached_questions and not _is_mock_result(cached_questions):
                                questions = cached_questions
                                logger.info(f"Exam {exam_id}: Cache HIT (hash={file_hash[:8]}), reusing {len(questions)} questions")
                                _publish_progress(exam_id, "progress", {"percent": 70, "message": f"Cache hit! Tìm thấy {len(questions)} câu đã phân tích."})
                            else:
                                logger.info(f"Exam {exam_id}: Cache rejected (low quality mock data)")
                        except json.JSONDecodeError:
                            questions = None
            except Exception as e:
                logger.debug(f"Exam {exam_id}: cache check failed: {e}")

        # ── Phase 4: Gemini full-document parse (markdown → JSON) ──
        if questions is None:
            _publish_progress(exam_id, "progress", {"percent": 50, "message": "Gemini đang đọc đề và đáp án..."})

            def _chunk_progress(done: int, total: int):
                pct = 50 + int((done / max(total, 1)) * 38)
                _publish_progress(exam_id, "progress", {
                    "percent": pct,
                    "message": f"Gemini đang phân tích... ({done}/{total})",
                })

            # Heartbeat — keeps SSE alive while Gemini works.
            _heartbeat_active = True
            _heartbeat_elapsed = [0]

            async def _heartbeat():
                while _heartbeat_active:
                    await asyncio.sleep(30)
                    if not _heartbeat_active:
                        break
                    _heartbeat_elapsed[0] += 30
                    _publish_progress(exam_id, "progress", {
                        "percent": 60,
                        "message": f"Gemini đang xử lý... ({_heartbeat_elapsed[0]}s)",
                    })

            heartbeat_task = asyncio.create_task(_heartbeat())

            # Adaptive timeout theo độ dài input gửi Gemini (~60-90s cho 5-15K chars), cap 600s.
            classify_timeout = min(600, 180 + (len(gemini_input) // 1000) * 8)
            logger.info("Gemini parse timeout: %ds (gemini_input_chars=%d/%d)", classify_timeout, len(gemini_input), len(extracted_text))
            try:
                # Chỉ gửi phần ĐỀ cho Gemini (đáp án map riêng theo cau_num bên dưới).
                questions = await asyncio.wait_for(
                    ai_parser.parse(
                        text=gemini_input,
                        progress_callback=_chunk_progress,
                        subject_hint=subject_hint,
                    ),
                    timeout=classify_timeout,
                )
                try:
                    usage = getattr(ai_parser, "_token_usage", {}) or {}
                    _in = int(usage.get("input") or 0)
                    _out = int(usage.get("output") or 0)
                    ingest_stats["ai_text_calls"] = int(usage.get("calls") or 0)
                    ingest_stats["estimated_input_tokens"] = _in
                    ingest_stats["estimated_output_tokens"] = _out
                    ingest_stats["ai_vision_calls"] = 0
                    # C3a: surface estimated cost so dashboards / quotas don't
                    # have to recompute from raw token counts.
                    from app.core.llm_pricing import cost_usd
                    ingest_stats["cost_usd"] = cost_usd(_in, _out)
                except Exception:
                    pass
            except asyncio.TimeoutError:
                raise ValueError(
                    f"Gemini xử lý quá thời gian ({classify_timeout}s). "
                    "API có thể đang quá tải. Vui lòng thử lại sau."
                )
            finally:
                _heartbeat_active = False
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        # Gemini không tìm được câu nào → needs_review (không "completed" rỗng im lặng).
        if not questions:
            questions = []
            final_status = "needs_review"
            ingest_warnings.append(
                "Gemini không tìm được câu hỏi nào trong tài liệu; đã đánh dấu cần rà soát."
            )

        # ── Map lời giải/đáp án (phần ĐÁP ÁN tách ở trên) vào từng câu theo cau_num ──
        # Deterministic — đáng tin hơn để Gemini tự map; chỉ điền khi câu còn thiếu.
        if questions and answer_key_map:
            from app.services.ai_parser import _extract_question_number as _q_num
            _mapped = 0
            for q in questions:
                n = _q_num(str(q.get("question") or ""))
                ak = answer_key_map.get(n) if n is not None else None
                if not ak:
                    continue
                _mapped += 1
                if not (q.get("solution_steps")):
                    q["solution_steps"] = _ak_to_steps(ak)
                if not str(q.get("answer") or "").strip():
                    _fa = _ak_final_answer(ak)
                    if _fa:
                        q["answer"] = _fa
                        q["answer_source"] = "answer_key"
            logger.info("Exam %s: mapped answer-key (regex) vào %d/%d câu", exam_id, _mapped, len(questions))

        # Câu nào vẫn thiếu "answer" (thường là tự luận) → nhờ Gemini trích đáp án
        # cuối từ phần ĐÁP ÁN. Output nhỏ {cau_num: answer} nên JSON ổn định.
        if questions and answer_key_md.strip() and any(
            not str(q.get("answer") or "").strip() for q in questions
        ):
            try:
                from app.services.ai_parser import _extract_question_number as _q_num
                gemini_answers = await asyncio.wait_for(
                    ai_parser.extract_answer_key(answer_key_md, subject_hint=subject_hint),
                    timeout=120,
                )
                _filled = 0
                for q in questions:
                    if str(q.get("answer") or "").strip():
                        continue
                    n = _q_num(str(q.get("question") or ""))
                    a = gemini_answers.get(n) if n is not None else None
                    if a:
                        q["answer"] = a
                        q["answer_source"] = "answer_key_gemini"
                        _filled += 1
                if _filled:
                    logger.info("Exam %s: Gemini answer-key điền %d đáp án còn thiếu", exam_id, _filled)
            except Exception as _ake:
                logger.warning("Exam %s: Gemini extract_answer_key skipped: %s", exam_id, _ake)

        from app.services.ai_parser import _detect_numbering_gaps as _detect_gaps
        try:
            ai_qr = getattr(ai_parser, "_last_quality_report", {}) or {}
            estimated = (
                len(structured) if structured
                else int(ai_qr.get("estimated_count") or 0)
            )
            gap_info = _detect_gaps(questions)
            parsed = len(questions)
            ratio = (parsed / estimated) if estimated > 0 else 1.0
            quality_report = {
                "estimated_count": estimated,
                "parsed_count": parsed,
                "completeness_ratio": round(ratio, 3),
                "is_incomplete": estimated > 0 and ratio < 0.80,
                "missing_numbers": gap_info["missing_numbers"],
                "duplicate_numbers": gap_info["duplicate_numbers"],
                "number_range": [gap_info["min_number"], gap_info["max_number"]],
                "numbered_questions": gap_info["numbered_questions"],
            }
            ingest_stats["quality_report"] = quality_report
            # Emit SSE event để FE có thể hiện banner ngay (không cần đợi complete)
            _publish_progress(exam_id, "quality_report", quality_report)
            if quality_report["is_incomplete"]:
                ingest_warnings.append(
                    f"Có thể thiếu câu hỏi: ước tính {estimated}, "
                    f"lấy được {parsed} ({ratio*100:.0f}%)"
                )
                logger.warning(
                    "Exam %s: incomplete parse %d/%d, missing %s",
                    exam_id, parsed, estimated, quality_report["missing_numbers"][:10],
                )
            elif quality_report["missing_numbers"]:
                ingest_warnings.append(
                    f"Có gap thứ tự câu: thiếu Câu {quality_report['missing_numbers'][:10]}"
                )
        except Exception as qr_err:
            logger.warning(f"Exam {exam_id}: quality_report build failed: {qr_err}")

        _publish_progress(exam_id, "progress", {
            "percent": 80,
            "message": f"Đã tìm {len(questions)} câu. Đang lưu vào ngân hàng đề...",
        })

        # Phase 5: save questions directly to bank (skip review drafts).
        # New flow: Gemini đã restore dấu VN trong step3_classify nên không cần
        # markdown review step nữa. User edit từng câu trên trang Questions sau.
        # _save_questions_to_bank returns a tuple (saved, skipped, duplicates).
        saved_count = 0
        skipped_count = 0
        bank_dup_count = 0

        # Phase 4.9: validate + normalize fields before persisting (clamp grade,
        # canonicalize difficulty, flag MC questions without an answer).
        try:
            from app.services.question_validation import validate_and_normalize_questions
            questions, _vstats, _vwarn = validate_and_normalize_questions(questions)
            ingest_stats["validation"] = _vstats
            ingest_warnings.extend(_vwarn)
        except Exception as _ve:
            logger.debug("Question validation pass skipped: %s", _ve)

        # Phase 3.5: serialize save per-user để dedup cross-exam không bị TOCTOU race.
        async with user_save_lock(user_id):
            async with AsyncSessionLocal() as save_db:
                saved_count, skipped_count, bank_dup_count = await _save_questions_to_bank(
                    save_db,
                    exam_id=exam_id,
                    user_id=user_id,
                    questions=questions,
                )
        logger.info(
            "Exam %s: save_to_bank → saved=%d skipped=%d duplicates=%d",
            exam_id, saved_count, skipped_count, bank_dup_count,
        )

        # Phase 3.9: answer-coverage diagnostic — surface tỉ lệ câu có đáp án
        # (đáp án rỗng là hợp lệ theo design, nhưng coverage thấp đáng để cảnh báo).
        try:
            _ans = sum(1 for q in questions if str(q.get("answer") or "").strip())
            ingest_stats["answer_coverage"] = round(_ans / max(len(questions), 1), 3)
        except Exception:
            pass

        # Fetch saved question IDs so the FE complete event can link to them.
        async with AsyncSessionLocal() as q_db:
            id_rows = await q_db.execute(
                select(Question.id).where(Question.exam_id == exam_id)
            )
            saved_question_ids = [row[0] for row in id_rows.all()]

        ingest_stats["questions_saved"] = saved_count
        ingest_stats["questions_skipped"] = skipped_count
        ingest_stats["draft_questions"] = 0  # legacy field, kept for FE compat
        ingest_stats["bank_duplicates"] = bank_dup_count
        ingest_stats["layout_page_count"] = len(layout_snapshot.get("pages", []))
        ingest_stats["layout_figure_count"] = len(layout_snapshot.get("figures", []))
        ingest_warnings.extend(layout_snapshot.get("warnings") or [])

        if rag_task:
            try:
                rag_stats, rag_warnings = await asyncio.wait_for(
                    asyncio.shield(rag_task),
                    timeout=RAG_WAIT_TIMEOUT_SECONDS,
                )
                ingest_stats.update(rag_stats or {})
                ingest_warnings.extend(rag_warnings or [])
            except asyncio.TimeoutError:
                ingest_stats["rag_index_status"] = "warning"
                ingest_warnings.append(
                    "Index tai lieu RAG dang chay nen; quiz da san sang truoc."
                )
            except Exception as rag_err:
                ingest_stats["rag_index_status"] = "failed"
                ingest_warnings.append(f"Index tài liệu RAG thất bại: {rag_err}")

        # Phase 3.3: wrap result_json MỘT LẦN (sau khi ingest_stats/warnings đã
        # finalize gồm RAG + layout) để cả 2 lần save (kể cả retry) đều lưu JSON
        # đã gắn metadata — tránh bug retry lưu JSON thô mất warnings/quality_report.
        from app.services.hybrid_ingest import merge_ingest_metadata, normalize_warnings
        ingest_warnings = normalize_warnings(ingest_warnings)
        result_json = merge_ingest_metadata(
            questions,
            warnings=ingest_warnings,
            ingest_stats=ingest_stats,
            payload_type="k12",
        )
        layout_json = json.dumps(layout_snapshot, ensure_ascii=False)

        # Phase 5b: persist exam artifact. final_status="completed" (hoặc
        # "needs_review" nếu doc 0 câu, Phase 3.2) → FE điều hướng phù hợp.
        try:
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = r.scalars().first()
                if exam:
                    exam.status = final_status
                    exam.layout_json = layout_json
                    exam.result_json = result_json
                    await db.commit()

            logger.info(f"Exam {exam_id}: {final_status} ({saved_count} questions saved to bank, {bank_dup_count} duplicates, {len(saved_question_ids)} ids fetched)")
        except Exception as save_err:
            # Retry once with a completely new session — dùng CÙNG result_json đã-wrap.
            logger.warning(f"Exam {exam_id}: First save attempt failed: {save_err}, retrying...")
            try:
                await asyncio.sleep(1)
                async with AsyncSessionLocal() as db2:
                    r2 = await db2.execute(select(Exam).filter(Exam.id == exam_id))
                    exam2 = r2.scalars().first()
                    if exam2:
                        exam2.status = final_status
                        exam2.layout_json = layout_json
                        exam2.result_json = result_json
                        await db2.commit()
                        logger.info(f"Exam {exam_id}: Retry save succeeded")
            except Exception as retry_err:
                logger.error(f"Exam {exam_id}: Retry save also failed: {retry_err}")
                raise

        _publish_progress(exam_id, "complete", {
            "message": f"Đã lưu {saved_count} câu vào ngân hàng đề.",
            "exam_id": exam_id,
            "question_count": saved_count,
            "question_ids": saved_question_ids,
            "duplicate_count": bank_dup_count,
            "warnings": ingest_warnings,
            "ingest_stats": ingest_stats,
            "status": final_status,
            "needs_review": final_status == "needs_review",
        })

    except Exception as e:
        logger.error(f"Error processing exam {exam_id}: {e}", exc_info=True)

        error_type, friendly_msg = _classify_ai_error(e)
        user_message = friendly_msg if friendly_msg else str(e)[:300]
        event_data: dict = {"message": user_message}
        if error_type:
            event_data["error_type"] = error_type

        _publish_progress(exam_id, "error_event", event_data)

        # Always use a FRESH session to update exam status
        stored_msg = f"[{error_type}] {user_message}" if error_type else user_message
        # Phase 3.4: commit status='failed' TRƯỚC, xoá file SAU (atomic-ish). Nếu
        # commit fail → giữ file để có thể retry; không xoá-rồi-mới-commit như cũ
        # (sẽ mất file + status kẹt 'processing' nếu commit lỗi). Không nuốt OSError.
        file_to_remove = None
        try:
            async with AsyncSessionLocal() as _fail_db:
                _r = await _fail_db.execute(select(Exam).filter(Exam.id == exam_id))
                _exam = _r.scalars().first()
                if _exam:
                    _exam.status = "failed"
                    _exam.error_message = stored_msg[:500]
                    file_to_remove = _exam.file_path
                    await _fail_db.commit()
        except Exception as db_err:
            logger.error(f"Failed to update exam {exam_id} status: {db_err}")
            file_to_remove = None  # DB chưa commit → giữ file để retry

        # Xoá file SAU khi DB đã commit 'failed'. Guard prefix uploads/ để không
        # lỡ xoá nhầm; log WARNING thay vì nuốt im.
        if file_to_remove and str(file_to_remove).replace("\\", "/").startswith(UPLOAD_DIR):
            try:
                if os.path.exists(file_to_remove):
                    os.remove(file_to_remove)
                    async with AsyncSessionLocal() as _fp_db:
                        await _fp_db.execute(
                            sa_text("UPDATE exam SET file_path = NULL WHERE id = :eid"),
                            {"eid": exam_id},
                        )
                        await _fp_db.commit()
            except OSError as rm_err:
                logger.warning("Exam %s: failed to remove upload file %s: %s", exam_id, file_to_remove, rm_err)
            except Exception as fp_err:
                logger.warning("Exam %s: file_path NULL cleanup failed: %s", exam_id, fp_err)


async def parse_exam_from_markdown(exam_id: int, request_id: Optional[str] = None):
    """Resume parsing from the user-edited OCR markdown (review step → questions).

    Re-runs :func:`process_file`; because the edited review ``.md`` exists, that
    run takes the ``_resuming`` path (cache-hit OCR for blocks, edited markdown for
    Gemini, no duplicate RAG/capture). Cleans up the review files on success.
    """
    subject = None
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Exam).filter(Exam.id == exam_id))
            exam = r.scalars().first()
            if not exam:
                return
            subject = exam.subject_code or "toan"
    except Exception as exc:
        logger.error("parse_exam_from_markdown: read exam %s failed: %s", exam_id, exc)
        return
    await process_file(
        exam_id,
        subject_hint=subject,
        ocr_engine="auto",
        request_id=request_id,
        review_markdown=False,  # resume → straight to parse
    )
    # Success path cleans up the review scratch files; on failure process_file set
    # status=failed and we keep the files so the user can retry the edit.
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Exam.status).filter(Exam.id == exam_id))
            status = r.scalar()
        if status in {"completed", "needs_review"}:
            _cleanup_ocr_review(exam_id)
    except Exception:
        pass


# ==================== Endpoints ====================

@router.post("/parse", response_model=ParseResponse)
async def parse_file_endpoint(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    speed: str = Query("balanced", pattern="^(fast|balanced|quality)$"),
    use_vision: bool = Query(False, description="Deprecated; Gemini Vision is disabled and local OCR is always used"),
    ocr_engine: str = Query("auto", pattern="^(auto|mineru|paddle-vl)$", description="OCR engine: auto (MinerU+PaddleOCR-VL fallback), mineru, paddle-vl"),
    subject_hint: str = Query(..., description="Mã môn học (bắt buộc): toan, vat-li, hoa-hoc, ngu-van, tieng-anh, ..."),
    grade: int | None = Query(None, ge=6, le=12, description="Khối lớp 6-12 (tuỳ chọn cho IELTS/legacy)"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload an exam file and parse it into structured JSON."""
    from app.core.config import settings

    if subject_hint not in VALID_SUBJECT_CODES:
        raise HTTPException(
            status_code=422,
            detail=f"Môn học '{subject_hint}' không hợp lệ. Vui lòng chọn môn học từ danh sách.",
        )

    # C3b: per-user daily AI token quota (0 = disabled) — fail fast before any work.
    if settings.DAILY_TOKEN_QUOTA > 0:
        used_today = await _user_tokens_today(db, current_user.id)
        if used_today >= settings.DAILY_TOKEN_QUOTA:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Đã đạt hạn mức token AI trong ngày "
                    f"({used_today:,}/{settings.DAILY_TOKEN_QUOTA:,}). "
                    "Vui lòng thử lại vào ngày mai."
                ),
            )

    allowed_extensions = {'.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg', '.txt', '.md'}
    file_ext = os.path.splitext(file.filename or "")[1].lower()

    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"File type '{file_ext}' not supported")

    # ── Read + validate size ──
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File trống")

    max_bytes = settings.MAX_UPLOAD_BYTES
    if len(content) > max_bytes:
        size_mb = len(content) / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn ({size_mb:.1f}MB). Tối đa {settings.MAX_UPLOAD_SIZE_MB}MB.",
        )

    # ── Validate file magic bytes ──
    _MAGIC_BYTES = {
        '.pdf':  [b'%PDF'],
        '.docx': [b'PK\x03\x04'],
        '.doc':  [b'\xd0\xcf\x11\xe0'],  # OLE2 compound document
        '.png':  [b'\x89PNG'],
        '.jpg':  [b'\xff\xd8\xff'],
        '.jpeg': [b'\xff\xd8\xff'],
    }
    expected_magics = _MAGIC_BYTES.get(file_ext)
    if expected_magics:
        if not any(content[:len(m)] == m for m in expected_magics):
            raise HTTPException(
                status_code=400,
                detail=f"File content does not match '{file_ext}' format. The file may be corrupted or renamed.",
            )

    # D3: reject oversized PDFs before saving/OCR (0 = disabled).
    if file_ext == ".pdf" and settings.MAX_PDF_PAGES > 0:
        n_pages = _pdf_page_count_from_bytes(content)
        if n_pages > settings.MAX_PDF_PAGES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"PDF có {n_pages} trang, vượt giới hạn {settings.MAX_PDF_PAGES} trang. "
                    "Vui lòng tách nhỏ tài liệu trước khi tải lên."
                ),
            )

    # ── Sanitize filename ──
    import re as _re
    raw_name = file.filename or "unnamed"
    if len(raw_name) > 255:
        raw_name = raw_name[:255]
    # Only allow safe characters in filename
    sanitized_name = _re.sub(r'[^a-zA-Z0-9_\-. ]', '_', os.path.basename(raw_name))
    if not sanitized_name or sanitized_name.startswith('.'):
        sanitized_name = "unnamed"

    # ── Save file ──
    file_id = str(uuid.uuid4())[:16]  # FIX #8: 16 hex chars = 2^64 space, avoids filename collision
    safe_filename = f"{file_id}_{sanitized_name}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    # FIX: wrap file write so we can clean up on DB failure (prevents orphaned files)
    try:
        with open(file_path, "wb") as f:
            f.write(content)
    except OSError as e:
        logger.error(f"File write failed for exam upload: {e}")
        raise HTTPException(status_code=500, detail="Không thể lưu file. Vui lòng thử lại.")

    # Compute file hash early so it's stored immediately on the Exam record
    file_hash = hashlib.md5(content).hexdigest()

    # Create DB record — clean up file if commit fails to avoid orphans
    exam = Exam(
        user_id=current_user.id,
        filename=file.filename,
        file_path=file_path,
        file_hash=file_hash,
        subject_code=subject_hint,
        grade=grade,
        status="pending",
        # Đề số hóa từ tài liệu tải lên — nội dung qua OCR/AI, cần gắn nhãn.
        origin=content_origin.OCR_IMPORT,
    )
    db.add(exam)
    try:
        await db.commit()
        await db.refresh(exam)
    except Exception:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise

    # use_vision is accepted for backward compatibility only. process_file
    # ignores it except for a warning; local OCR is always used.
    _req_id = getattr(request.state, "request_id", None)
    # OCR review step (PDF | markdown editable) bật mặc định cho K12 (gate sau OCR).
    _review = OCR_REVIEW_STEP
    background_tasks.add_task(
        process_file, exam.id, speed, use_vision, subject_hint, ocr_engine,
        request_id=_req_id, review_markdown=_review,
    )

    return ParseResponse(job_id=exam.id, status="pending", message="File queued for processing")


@router.get("/status/{job_id}", response_model=ExamResponse)
async def get_status(
    job_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the status and result of a parse job."""
    result = await db.execute(
        select(Exam).filter(Exam.id == job_id, Exam.user_id == current_user.id)
    )
    exam = result.scalars().first()

    if not exam:
        raise HTTPException(status_code=404, detail="Job not found")

    item = ExamResponse.model_validate(exam)
    try:
        from app.services.hybrid_ingest import extract_ingest_metadata
        warnings, ingest_stats = extract_ingest_metadata(exam.result_json)
        item.warnings = warnings or None
        item.ingest_stats = ingest_stats or None
    except Exception:
        pass
    try:
        from sqlalchemy import func
        if exam.status == "needs_review":
            cnt = (await db.execute(
                select(func.count(QuestionDraft.id)).where(QuestionDraft.exam_id == job_id)
            )).scalar() or 0
        else:
            cnt = (await db.execute(
                select(func.count(Question.id)).where(Question.exam_id == job_id)
            )).scalar() or 0
        item.question_count = int(cnt)
    except Exception:
        pass
    return item


@router.get("/{exam_id}/review", response_model=ReviewPayload)
async def get_review_payload(
    exam_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    if exam.status not in {"needs_review", "completed"}:
        raise HTTPException(status_code=409, detail="Review is not ready yet")
    from app.services.review_workflow import build_review_payload
    return ReviewPayload(**await build_review_payload(db, exam_id=exam_id))


@router.get("/{exam_id}/page-assets")
async def get_exam_page_assets(
    exam_id: int,
    page_num: Optional[int] = Query(None, description="Filter to a specific page (1-based)"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return page-render images of the source PDF for bbox overlay on bank/exam views.

    Works for both `needs_review` and `completed` exams (assets are persisted).
    """
    from app.services.asset_storage import get_asset_storage

    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    stmt = select(DocumentAsset).where(
        DocumentAsset.exam_id == exam_id,
        DocumentAsset.kind == "page_render",
    )
    if page_num is not None:
        stmt = stmt.where(DocumentAsset.page_num == page_num)
    stmt = stmt.order_by(DocumentAsset.page_num.asc())

    assets = (await db.execute(stmt)).scalars().all()
    storage = get_asset_storage()
    return {
        "pages": [
            {
                "page_num": a.page_num,
                "url": storage.public_url(a.storage_key),
                "width": a.width,
                "height": a.height,
            }
            for a in assets
        ]
    }


def _ocr_from_artifact(exam: Exam) -> tuple[str, dict]:
    """Best-effort fallback when the editable review file is gone (already parsed
    or gate off): read the cached OCR artifact by file_hash for markdown + blocks."""
    import glob as _glob
    if not exam.file_hash:
        return "", {}
    pattern = os.path.join("uploads", "ocr_artifacts", f"{exam.file_hash}_*.json")
    for path in sorted(_glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                art = json.load(f)
            return art.get("text", "") or "", {
                "blocks": art.get("blocks", []),
                "figures": art.get("figures", []),
                "page_count": art.get("page_count", 0),
                "method": art.get("method"),
            }
        except Exception:
            continue
    return "", {}


@router.get("/{exam_id}/ocr")
async def get_ocr_review(
    exam_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """OCR markdown + layout JSON for the PDF|markdown review view (/upload/ocr)."""
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    md = _read_ocr_review_md(exam_id)
    ctx = _load_review_json(exam_id)
    if md is None:
        md, ctx = _ocr_from_artifact(exam)
    return {
        "exam_id": exam_id,
        "status": exam.status,
        "markdown": md or "",
        "blocks": ctx.get("blocks", []),
        "figures": ctx.get("figures", []),
        "page_count": ctx.get("page_count", 0),
        "method": ctx.get("method"),
        "editable": _read_ocr_review_md(exam_id) is not None,
    }


class OcrMarkdownPatch(BaseModel):
    markdown: str


@router.patch("/{exam_id}/ocr")
async def save_ocr_review(
    exam_id: int,
    patch: OcrMarkdownPatch,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the user-corrected OCR markdown (used by the parse step)."""
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    if not os.path.exists(_ocr_review_md_path(exam_id)):
        raise HTTPException(status_code=409, detail="Tài liệu này không ở bước review OCR")
    md = patch.markdown or ""
    if len(md) > 2_000_000:
        raise HTTPException(status_code=413, detail="Markdown quá dài (>2MB)")
    try:
        with open(_ocr_review_md_path(exam_id), "w", encoding="utf-8") as f:
            f.write(md)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Lưu markdown thất bại: {exc}")
    return {"ok": True, "chars": len(md)}


@router.post("/{exam_id}/ocr/parse", response_model=ParseResponse)
async def parse_ocr_review(
    exam_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger Gemini parse from the (edited) OCR markdown → questions."""
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    if not os.path.exists(_ocr_review_md_path(exam_id)):
        raise HTTPException(status_code=409, detail="Chưa có markdown OCR để tạo câu hỏi")
    exam.status = "processing"
    await db.commit()
    background_tasks.add_task(parse_exam_from_markdown, exam_id)
    return ParseResponse(job_id=exam_id, status="processing", message="Đang tạo câu hỏi từ markdown")


@router.patch("/{exam_id}/review/questions/{draft_id}", response_model=DraftQuestionResponse)
async def patch_review_draft(
    exam_id: int,
    draft_id: int,
    patch: DraftQuestionPatch,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    draft = await db.scalar(select(QuestionDraft).where(QuestionDraft.id == draft_id, QuestionDraft.exam_id == exam_id))
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    data = patch.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in {"pending", "accepted", "rejected"}:
        raise HTTPException(status_code=422, detail="Invalid draft status")
    if "question_text" in data:
        draft.question_text = data["question_text"] or ""
    if "subject_code" in data:
        draft.subject_code = data["subject_code"]
    if "question_type" in data:
        draft.question_type = data["question_type"]
    if "topic" in data:
        draft.topic = data["topic"]
    if "difficulty" in data:
        draft.difficulty = data["difficulty"]
    if "grade" in data:
        draft.grade = data["grade"]
    if "chapter" in data:
        draft.chapter = data["chapter"]
    if "lesson_title" in data:
        draft.lesson_title = data["lesson_title"]
    if "answer" in data:
        draft.answer = data["answer"]
    if "solution_steps" in data:
        draft.solution_steps = json.dumps(data["solution_steps"] or [], ensure_ascii=False)
    if "status" in data:
        draft.status = data["status"]
    if "bbox" in data:
        bbox = data["bbox"]
        if bbox is not None and (len(bbox) != 4 or any(v < 0 or v > 1 for v in bbox)):
            raise HTTPException(status_code=422, detail="bbox must contain 4 normalized coordinates")
        from app.services.review_workflow import recompute_draft_assignments
        try:
            draft = await recompute_draft_assignments(db, exam_id=exam_id, draft_id=draft_id, bbox=bbox)
        except ValueError:
            raise HTTPException(status_code=404, detail="Draft not found")
    if "asset_ids" in data:
        requested = set(data["asset_ids"] or [])
        assets = (
            await db.execute(
                select(DocumentAsset).where(
                    DocumentAsset.exam_id == exam_id,
                    DocumentAsset.id.in_(requested) if requested else False,
                )
            )
        ).scalars().all() if requested else []
        valid_ids = {asset.id for asset in assets}
        if valid_ids != requested:
            raise HTTPException(status_code=422, detail="One or more assets do not belong to this exam")
        from sqlalchemy import delete
        await db.execute(delete(QuestionDraftAsset).where(QuestionDraftAsset.draft_id == draft_id))
        for asset_id in sorted(valid_ids):
            db.add(QuestionDraftAsset(draft_id=draft_id, asset_id=asset_id, relation_type="figure"))
    draft.updated_at = datetime.utcnow()
    await db.commit()
    from app.services.review_workflow import build_review_payload
    payload = await build_review_payload(db, exam_id=exam_id)
    row = next(item for item in payload["drafts"] if item["id"] == draft_id)
    return DraftQuestionResponse(**row)


@router.post("/{exam_id}/review/questions/{draft_id}/split", response_model=list[DraftQuestionResponse])
async def split_review_draft(
    exam_id: int,
    draft_id: int,
    body: DraftSplitRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    draft = await db.scalar(select(QuestionDraft).where(QuestionDraft.id == draft_id, QuestionDraft.exam_id == exam_id))
    if not exam or not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    source_ids = json.loads(draft.source_block_ids_json or "[]")
    if body.split_after_block_id not in source_ids:
        raise HTTPException(status_code=422, detail="split_after_block_id is not part of this draft")
    idx = source_ids.index(body.split_after_block_id) + 1
    left_ids, right_ids = source_ids[:idx], source_ids[idx:]
    if not right_ids:
        raise HTTPException(status_code=422, detail="Split point leaves no blocks on the right")
    layout = json.loads(exam.layout_json or "{}")
    blocks = {str(block["block_id"]): block for block in layout.get("blocks", [])}
    draft.source_block_ids_json = json.dumps(left_ids)
    draft.question_text = "\n\n".join(blocks[i]["text"] for i in left_ids if i in blocks) or draft.question_text
    right = QuestionDraft(
        exam_id=draft.exam_id,
        user_id=draft.user_id,
        cau_num=None,
        question_order=draft.question_order + 1,
        page_num=blocks[right_ids[0]]["page_num"] if right_ids and right_ids[0] in blocks else draft.page_num,
        question_text="\n\n".join(blocks[i]["text"] for i in right_ids if i in blocks),
        subject_code=draft.subject_code,
        question_type=draft.question_type,
        topic=draft.topic,
        difficulty=draft.difficulty,
        grade=draft.grade,
        chapter=draft.chapter,
        lesson_title=draft.lesson_title,
        answer=None,
        answer_source=None,
        solution_steps="[]",
        source_block_ids_json=json.dumps(right_ids),
        confidence=draft.confidence,
        status="pending",
    )
    db.add(right)
    await db.flush()
    later = (
        await db.execute(
            select(QuestionDraft).where(
                QuestionDraft.exam_id == exam_id,
                QuestionDraft.id != draft.id,
                QuestionDraft.question_order > draft.question_order,
            )
        )
    ).scalars().all()
    for item in later:
        item.question_order += 1
    await db.commit()
    from app.services.review_workflow import build_review_payload
    payload = await build_review_payload(db, exam_id=exam_id)
    return [DraftQuestionResponse(**item) for item in payload["drafts"] if item["id"] in {draft.id, right.id}]


@router.post("/{exam_id}/review/questions/merge", response_model=DraftQuestionResponse)
async def merge_review_drafts(
    exam_id: int,
    body: DraftMergeRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if len(body.draft_ids) < 2:
        raise HTTPException(status_code=422, detail="Need at least two drafts to merge")
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    drafts = (
        await db.execute(
            select(QuestionDraft)
            .where(QuestionDraft.exam_id == exam_id, QuestionDraft.id.in_(body.draft_ids))
            .order_by(QuestionDraft.question_order.asc())
        )
    ).scalars().all()
    if len(drafts) != len(set(body.draft_ids)):
        raise HTTPException(status_code=404, detail="One or more drafts not found")
    target = drafts[0]
    source_ids: list[str] = []
    texts: list[str] = []
    asset_ids: set[int] = set()
    for draft in drafts:
        source_ids.extend(json.loads(draft.source_block_ids_json or "[]"))
        texts.append(draft.question_text)
        links = (
            await db.execute(select(QuestionDraftAsset).where(QuestionDraftAsset.draft_id == draft.id))
        ).scalars().all()
        asset_ids.update(link.asset_id for link in links)
    target.question_text = "\n\n".join(text for text in texts if text.strip())
    target.source_block_ids_json = json.dumps(source_ids)
    from sqlalchemy import delete
    await db.execute(delete(QuestionDraftAsset).where(QuestionDraftAsset.draft_id == target.id))
    for asset_id in sorted(asset_ids):
        db.add(QuestionDraftAsset(draft_id=target.id, asset_id=asset_id, relation_type="figure"))
    for draft in drafts[1:]:
        await db.delete(draft)
    await db.commit()
    from app.services.review_workflow import build_review_payload
    payload = await build_review_payload(db, exam_id=exam_id)
    row = next(item for item in payload["drafts"] if item["id"] == target.id)
    return DraftQuestionResponse(**row)


@router.post("/{exam_id}/review/commit", response_model=ReviewCommitResponse)
async def commit_review(
    exam_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id))
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    drafts = (
        await db.execute(
            select(QuestionDraft)
            .where(QuestionDraft.exam_id == exam_id, QuestionDraft.status == "accepted")
            .order_by(QuestionDraft.question_order.asc())
        )
    ).scalars().all()
    if not drafts:
        raise HTTPException(status_code=422, detail="No accepted drafts to commit")
    questions = [
        {
            "question": draft.question_text,
            "subject": draft.subject_code or exam.subject_code or "toan",
            "type": draft.question_type,
            "topic": draft.topic,
            "difficulty": draft.difficulty,
            "grade": draft.grade,
            "chapter": draft.chapter,
            "lesson_title": draft.lesson_title,
            "answer": draft.answer,
            "answer_source": draft.answer_source,
            "solution_steps": json.loads(draft.solution_steps or "[]"),
            "page_num": draft.page_num,
            "bbox": json.loads(draft.bbox_json) if draft.bbox_json else None,
        }
        for draft in drafts
    ]
    # Giáo viên đã duyệt từng draft trong màn review → nội dung là "AI có người duyệt".
    saved, skipped, duplicate_count = await _save_questions_to_bank(
        db, exam.id, exam.user_id, questions, reviewed=True,
    )
    if not saved:
        raise HTTPException(status_code=422, detail="No accepted questions were saved")
    from app.services.review_workflow import link_assets_to_saved_questions
    await link_assets_to_saved_questions(db, exam_id=exam_id)
    exam.status = "completed"
    exam.origin = content_origin.AI_ASSISTED
    exam.reviewed_by_user = True
    exam.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return ReviewCommitResponse(saved=saved, skipped=skipped, duplicate_count=duplicate_count)


# ── SSE one-time token endpoint ──

@router.post("/stream-token/{job_id}")
async def create_stream_token(
    job_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a one-time token for SSE streaming (avoids JWT in URL)."""
    # Verify user owns this job
    result = await db.execute(
        select(Exam).filter(Exam.id == job_id, Exam.user_id == current_user.id)
    )
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Job not found")

    now = _time.time()
    sse_token = _secrets.token_urlsafe(32)

    # Redis-backed (shared across workers); TTL handled by SETEX
    r = get_redis()
    if r is not None:
        try:
            await r.setex(f"sse:{sse_token}", _SSE_TOKEN_TTL, f"{current_user.id}:{job_id}")
            return {"token": sse_token}
        except Exception as e:
            logger.warning("Redis SSE token set failed, using in-memory: %s", e)

    # In-memory fallback — cleanup expired tokens then store
    expired = [k for k, v in _sse_tokens.items() if now - v[2] > _SSE_TOKEN_TTL]
    for k in expired:
        del _sse_tokens[k]
    _sse_tokens[sse_token] = (current_user.id, job_id, now)
    return {"token": sse_token}


# ── SSE Streaming endpoint (Sprint 3, Task 18) ──

@router.get("/stream/{job_id}")
async def stream_progress(
    job_id: int,
    token: str = Query(..., description="One-time SSE token or JWT (backward compatible)"),
):
    """Stream real-time progress events via Server-Sent Events.

    Accepts one-time tokens (from POST /stream-token) or JWT (backward compatible).
    Events: progress (percent + message), complete (result_json), error_event.
    Falls back gracefully — client also has polling fallback.
    """
    from jose import jwt, JWTError
    from app.core.config import settings

    user_id = None

    # Try one-time SSE token first — Redis (consume via get+del), else in-memory
    sse_data = None
    r = get_redis()
    if r is not None:
        try:
            val = await r.get(f"sse:{token}")
            if val is not None:
                await r.delete(f"sse:{token}")  # one-time use
                uid_s, jid_s = val.split(":", 1)
                # Redis SETEX already enforces TTL → created_at = now
                sse_data = (int(uid_s), int(jid_s), _time.time())
        except Exception as e:
            logger.debug("Redis SSE token read failed, using in-memory: %s", e)
    if sse_data is None:
        sse_data = _sse_tokens.pop(token, None)

    if sse_data:
        uid, expected_job_id, created_at = sse_data
        if _time.time() - created_at > _SSE_TOKEN_TTL:
            raise HTTPException(status_code=401, detail="SSE token expired")
        if expected_job_id != job_id:
            raise HTTPException(status_code=403, detail="Token does not match job")
        user_id = uid
    else:
        # Fallback: verify JWT (backward compatible)
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            user_id = payload.get("sub")
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid token")
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid token")

    # Short-lived DB session — release immediately after auth check
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Exam).filter(Exam.id == job_id, Exam.user_id == int(user_id))
        )
        exam = result.scalars().first()
        if not exam:
            raise HTTPException(status_code=404, detail="Job not found")
        # Capture state before closing session
        exam_status = exam.status
        exam_result_json = exam.result_json
        exam_error_msg = exam.error_message

    # If already completed/failed, send final event immediately
    if exam_status in {"completed", "needs_review"}:
        async def _immediate_complete():
            yield f"event: complete\ndata: {json.dumps({'result_json': exam_result_json, 'needs_review': exam_status == 'needs_review'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_immediate_complete(), media_type="text/event-stream")

    if exam_status == "failed":
        async def _immediate_error():
            yield f"event: error_event\ndata: {json.dumps({'message': exam_error_msg or 'Failed'})}\n\n"
        return StreamingResponse(_immediate_error(), media_type="text/event-stream")

    # Subscribe to progress events
    queue = await _subscribe(job_id)

    # Re-check status after subscribing to close race window.
    # process_file may have completed between our first DB read and subscription.
    try:
        async with AsyncSessionLocal() as _db2:
            _r2 = await _db2.execute(select(Exam).filter(Exam.id == job_id))
            _exam2 = _r2.scalars().first()
            if _exam2 and _exam2.status in {"completed", "needs_review"}:
                await _unsubscribe(job_id, queue)
                _rj = _exam2.result_json
                async def _race_complete():
                    yield f"event: complete\ndata: {json.dumps({'result_json': _rj, 'needs_review': _exam2.status == 'needs_review'}, ensure_ascii=False)}\n\n"
                return StreamingResponse(_race_complete(), media_type="text/event-stream")
            if _exam2 and _exam2.status == "failed":
                await _unsubscribe(job_id, queue)
                _em = _exam2.error_message or "Failed"
                async def _race_error():
                    yield f"event: error_event\ndata: {json.dumps({'message': _em})}\n\n"
                return StreamingResponse(_race_error(), media_type="text/event-stream")
    except Exception as _race_err:
        # DB query failed — fall through to event generator; polling will catch completion
        logger.warning(f"Stream race-check DB error for exam {job_id}: {_race_err}")

    async def _event_generator():
        try:
            # Send initial heartbeat
            yield f": connected\n\n"

            timeout_count = 0
            while timeout_count < 600:  # Max 10 min — reset on each event
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"event: {event}\ndata: {data}\n\n"
                    timeout_count = 0  # Reset: any event resets the idle clock

                    # Terminal events
                    if event in ("complete", "error_event"):
                        return
                except asyncio.TimeoutError:
                    timeout_count += 1
                    # Send keepalive every 15s
                    if timeout_count % 15 == 0:
                        yield f": keepalive\n\n"
                    continue

            # 10 min of silence — tell client to fall back to polling
            yield f"event: stream_timeout\ndata: {json.dumps({'message': 'Stream timeout'})}\n\n"
        finally:
            await _unsubscribe(job_id, queue)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.get("/history", response_model=ExamListResponse)
async def list_exams(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Search by filename"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List parse history for the current user with pagination and optional search."""
    from sqlalchemy import func

    base_filter = [Exam.user_id == current_user.id]
    if search and search.strip():
        base_filter.append(Exam.filename.ilike(f"%{search.strip()}%"))

    # Count total
    count_result = await db.execute(
        select(func.count(Exam.id)).where(*base_filter)
    )
    total = count_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    # OPT: ORDER BY exam.created_at DESC — covered by ix_exam_user_created index
    result = await db.execute(
        select(Exam)
        .where(*base_filter)
        .order_by(Exam.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    exams = result.scalars().all()

    # Count questions per exam in one query
    exam_ids = [e.id for e in exams]
    counts: dict[int, int] = {}
    if exam_ids:
        from app.db.models.question import Question as _Q
        count_rows = await db.execute(
            select(_Q.exam_id, func.count(_Q.id).label("cnt"))
            .where(_Q.exam_id.in_(exam_ids))
            .group_by(_Q.exam_id)
        )
        counts = {row.exam_id: row.cnt for row in count_rows}
        draft_rows = await db.execute(
            select(QuestionDraft.exam_id, func.count(QuestionDraft.id).label("cnt"))
            .where(QuestionDraft.exam_id.in_(exam_ids))
            .group_by(QuestionDraft.exam_id)
        )
        draft_counts = {row.exam_id: row.cnt for row in draft_rows}
    else:
        draft_counts = {}

    items = []
    for e in exams:
        item = ExamResponse.model_validate(e)
        item.question_count = draft_counts.get(e.id, 0) if e.status == "needs_review" else counts.get(e.id, 0)
        try:
            from app.services.hybrid_ingest import extract_ingest_metadata
            warnings, ingest_stats = extract_ingest_metadata(e.result_json)
            item.warnings = warnings or None
            item.ingest_stats = ingest_stats or None
        except Exception:
            pass
        items.append(item)

    return ExamListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete("/{job_id}")
async def delete_exam(
    job_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a parse job and its uploaded file."""
    result = await db.execute(
        select(Exam).filter(Exam.id == job_id, Exam.user_id == current_user.id)
    )
    exam = result.scalars().first()

    if not exam:
        raise HTTPException(status_code=404, detail="Job not found")

    # Delete file
    if exam.file_path and os.path.exists(exam.file_path):
        try:
            os.remove(exam.file_path)
        except OSError:
            pass
    _cleanup_ocr_review(job_id)

    await db.delete(exam)
    await db.commit()

    return {"detail": "Deleted"}


class ExamRenameRequest(BaseModel):
    name: str


@router.patch("/{job_id}", response_model=ExamResponse)
async def rename_exam(
    job_id: int,
    body: ExamRenameRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename an exam (update display filename)."""
    result = await db.execute(
        select(Exam).filter(Exam.id == job_id, Exam.user_id == current_user.id)
    )
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Tên không được để trống")

    exam.filename = name
    await db.commit()
    await db.refresh(exam)

    # Count questions for response
    from sqlalchemy import func as _func
    cnt_result = await db.execute(
        select(_func.count(Question.id)).where(Question.exam_id == exam.id)
    )
    item = ExamResponse.model_validate(exam)
    item.question_count = cnt_result.scalar() or 0
    return item


@router.get("/{exam_id}/similar")
async def get_similar_questions(
    exam_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Trả về danh sách câu hỏi tương tự trong ngân hàng cho đề vừa upload.

    Kết quả có thể rỗng nếu:
    - Background similarity detection chưa chạy xong (thử lại sau vài giây)
    - Không có câu nào đủ ngưỡng tương tự (>= 0.82 cosine)
    """
    from app.services.similarity_detector import get_exam_similarities

    # Verify exam belongs to user
    result = await db.execute(
        select(Exam).where(Exam.id == exam_id, Exam.user_id == current_user.id)
    )
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Đề thi không tồn tại")

    similarities = await get_exam_similarities(db, exam_id, current_user.id)
    return {
        "exam_id": exam_id,
        "total_questions_with_similar": len(similarities),
        "results": similarities,
    }


# ==================== Admin: Re-index embeddings ====================

@router.post("/admin/reindex")
async def reindex_embeddings(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-generate embeddings for ALL questions in the bank.

    Use after:
    - Enabling pgvector on Neon
    - First deployment with embedding support
    - Fixing embedding API issues
    """
    # Get all question IDs for this user
    result = await db.execute(
        select(Question.id).where(Question.user_id == current_user.id)
    )
    all_ids = [row[0] for row in result.fetchall()]

    if not all_ids:
        return {"detail": "No questions to embed", "total": 0}

    # Check existing embeddings
    from sqlalchemy import text as sa_txt
    try:
        placeholders = ",".join(str(int(qid)) for qid in all_ids)
        existing = await db.execute(sa_txt(
            f"SELECT question_id FROM question_embedding WHERE question_id IN ({placeholders})"
        ))
        existing_ids = {row[0] for row in existing.fetchall()}
        missing_ids = [qid for qid in all_ids if qid not in existing_ids]
    except Exception:
        missing_ids = all_ids
        existing_ids = set()

    if not missing_ids:
        return {
            "detail": "All questions already have embeddings",
            "total": len(all_ids),
            "already_embedded": len(existing_ids),
        }

    # Run embedding in background
    async def _reindex_bg(ids):
        from app.db.session import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as _db:
                from app.services.vector_search import embed_questions
                await embed_questions(_db, ids)
                logger.info(f"Reindex: embedded {len(ids)} questions for user {current_user.id}")
        except Exception as e:
            logger.error(f"Reindex failed: {e}")

    task = asyncio.create_task(_reindex_bg(missing_ids))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "detail": f"Embedding started for {len(missing_ids)} questions (background)",
        "total": len(all_ids),
        "already_embedded": len(existing_ids),
        "queued": len(missing_ids),
    }
