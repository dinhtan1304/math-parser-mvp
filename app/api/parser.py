import os
import uuid
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Query, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.future import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import file_handler, ai_parser_service as ai_parser
from app.api import deps
from app.db.session import AsyncSessionLocal, get_db
from app.db.models.exam import Exam
from app.db.models.question import Question
from app.db.models.user import User

logger = logging.getLogger(__name__)

# Keep references to background tasks to prevent garbage collection
_background_tasks: set[asyncio.Task] = set()

router = APIRouter()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── SSE progress tracking (Sprint 3, Task 18) ──
# In-memory store: exam_id → asyncio.Queue of SSE events
_progress_queues: Dict[int, List[asyncio.Queue]] = {}
# FIX #11: Lock to prevent concurrent subscribe/unsubscribe corruption
_queues_lock = asyncio.Lock()


def _publish_progress(exam_id: int, event: str, data: dict):
    """Publish a progress event to all connected SSE clients.
    No lock needed here: list reads are safe in asyncio single-threaded model.
    """
    queues = _progress_queues.get(exam_id, [])
    msg = json.dumps(data, ensure_ascii=False)
    for q in list(queues):  # FIX #11: iterate copy to avoid mutation during loop
        try:
            q.put_nowait((event, msg))
        except asyncio.QueueFull:
            pass  # Drop if client is too slow


async def _subscribe(exam_id: int) -> asyncio.Queue:
    """Subscribe to progress events for an exam."""
    q = asyncio.Queue(maxsize=100)
    async with _queues_lock:  # FIX #11: protect list mutation
        if exam_id not in _progress_queues:
            _progress_queues[exam_id] = []
        _progress_queues[exam_id].append(q)
    return q


async def _unsubscribe(exam_id: int, q: asyncio.Queue):
    """Unsubscribe from progress events."""
    async with _queues_lock:  # FIX #11: protect list mutation
        queues = _progress_queues.get(exam_id, [])
        if q in queues:
            queues.remove(q)
        if not queues and exam_id in _progress_queues:
            del _progress_queues[exam_id]


# ==================== Response Models ====================

class ParseResponse(BaseModel):
    job_id: int
    status: str
    message: str


class ExamResponse(BaseModel):
    id: int
    filename: str
    status: str
    created_at: datetime
    result_json: Optional[str] = None
    error_message: Optional[str] = None
    question_count: Optional[int] = None  # populated by history endpoint

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


def _is_mock_result(questions: list) -> bool:
    """Detect if cached result was from mock parser (regex garbage).

    Mock parser signs:
    - All topics are "Toán học" (hardcoded default)
    - No grade/chapter/lesson data
    - No solution_steps
    """
    if not questions or len(questions) == 0:
        return True

    # Check first 5 questions
    sample = questions[:5]
    mock_signs = 0

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

    # If >80% of checks are mock-like, it's mock data
    return mock_signs > len(sample) * 2


def _is_math_text_poor_quality(text: str) -> bool:
    """Heuristic: check if extracted text is garbage or has broken math.

    v2: Uses deep math structure analysis from file_handler.
    Detects broken formulas like "2 2 2 a b c" that old check missed.
    """
    if not text or len(text) < 50:
        return True

    # Quick check: basic math markers
    math_markers = [
        '=', '+', '-', '/', '^', '²', '³',
        'x', 'y', 'sin', 'cos', 'tan', 'log', 'ln',
        'lim', 'sqrt', 'frac', 'pi',
        'Câu', 'Bài', 'câu', 'bài',
    ]
    marker_count = sum(1 for m in math_markers if m in text)
    if marker_count < 3:
        return True

    # Too many garbled characters
    garbled = sum(1 for c in text if ord(c) > 0xFFFF or (ord(c) < 32 and c not in '\n\r\t'))
    if len(text) > 0 and garbled / len(text) > 0.1:
        return True

    # v2: Deep math structure analysis
    try:
        analysis = file_handler.analyze_math_quality(text)
        if analysis.get("should_use_vision"):
            logger.info(
                f"Math quality analysis: score={analysis['score']}, "
                f"reason={analysis['reason']}"
            )
            return True
    except Exception as e:
        logger.debug(f"Math quality analysis failed: {e}")

    return False


async def _save_questions_to_bank(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
    questions: list,
):
    """Tách parsed questions thành individual Question records.

    Chạy sau khi parse thành công. Nếu exam đã có questions (re-parse),
    xóa cũ rồi insert mới.

    Sprint 3, Task 22: Duplicate detection via content_hash.
    """
    from app.db.models.question import _question_hash

    try:
        # Xóa questions cũ nếu re-parse
        from sqlalchemy import delete
        await db.execute(
            delete(Question).where(Question.exam_id == exam_id)
        )

        # ── Task 22: Pre-compute hashes and check duplicates ──
        # Curriculum matching: map AI grade/chapter to DB entries
        try:
            from app.services.curriculum_matcher import match_questions_to_curriculum
            questions = await match_questions_to_curriculum(db, questions)
        except Exception as e:
            logger.warning(f"Curriculum matching skipped: {e}")

        new_questions = []
        for i, q in enumerate(questions):
            q_text = q.get("question", "")
            if not q_text.strip():
                continue
            c_hash = _question_hash(q_text)
            new_questions.append((i, q, c_hash))

        # FIX: intra-batch dedup only.
        # Cross-exam dedup removed: the same question can legitimately appear in
        # multiple exams (re-upload, different class). Similarity detection
        # (background step 3) surfaces near-duplicates without data loss.
        existing_hashes: set = set()

        # Insert, skipping duplicates
        saved = 0
        skipped = 0
        for i, q, c_hash in new_questions:
            if c_hash in existing_hashes:
                skipped += 1
                continue

            question = Question(
                exam_id=exam_id,
                user_id=user_id,
                question_text=q.get("question", ""),
                content_hash=c_hash,
                question_type=q.get("type"),
                topic=None,
                difficulty=q.get("difficulty"),
                grade=q.get("grade"),
                chapter=q.get("chapter"),
                lesson_title=q.get("lesson_title"),
                answer=q.get("answer"),
                solution_steps=json.dumps(q.get("solution_steps", []), ensure_ascii=False),
                question_order=i + 1,
                is_public=False,
            )
            db.add(question)
            existing_hashes.add(c_hash)  # Prevent intra-batch duplicates
            saved += 1

        await db.commit()

        if skipped:
            logger.info(f"Exam {exam_id}: Saved {saved}, skipped {skipped} duplicates")
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

        # Keep reference to prevent garbage collection (Python asyncio gotcha)
        task = asyncio.create_task(_index_in_background(saved_ids))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    except Exception as e:
        logger.error(f"Exam {exam_id}: Failed to save questions to bank: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


async def process_file(exam_id: int, speed: str = "balanced", use_vision: bool = False):
    """Background task: extract text from file and parse with AI.

    v3: Short-lived DB sessions to survive Neon idle timeout.
    Old approach: open session → 2min AI processing → save → connection dead.
    New approach: open/close session for each DB operation.
    """
    try:
        # ── Phase 1: Read exam info (short DB session) ──
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

        _publish_progress(exam_id, "progress", {"percent": 10, "message": "Đang trích xuất nội dung..."})

        # ── Phase 2: Extract content (no DB needed) ──
        extracted = await file_handler.extract_text(file_path, use_vision=use_vision)
        extracted_text = extracted.get("text", "")
        images = extracted.get("images", [])
        file_hash = extracted.get("file_hash", "")

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

        # v2: Smart vision auto-detection for math PDFs
        if not use_vision and extracted_text.strip():
            analysis = file_handler.analyze_math_quality(extracted_text)
            if analysis.get("should_use_vision"):
                logger.info(
                    f"Exam {exam_id}: Auto-switching to Vision mode "
                    f"(score={analysis['score']}, reason={analysis['reason']})"
                )
                _publish_progress(exam_id, "progress", {
                    "percent": 15,
                    "message": f"Phát hiện công thức toán phức tạp — chuyển sang Vision mode..."
                })
                try:
                    extracted = await file_handler.extract_text(file_path, use_vision=True)
                    images = extracted.get("images", [])
                    extracted_text = extracted.get("text", "")
                    use_vision = True
                except Exception as e:
                    logger.warning(f"Exam {exam_id}: Auto-vision failed: {e}")

        _publish_progress(exam_id, "progress", {"percent": 25, "message": "Trích xuất xong. Đang phân tích..."})

        # ── Phase 3: Check cache (short DB session) ──
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

        # ── Phase 4: AI parsing (no DB needed — this takes 30s-3min) ──
        if questions is None:
            if not use_vision and (not extracted_text.strip() or _is_math_text_poor_quality(extracted_text)):
                logger.info(f"Exam {exam_id}: Text quality poor, falling back to Vision mode")
                _publish_progress(exam_id, "progress", {"percent": 30, "message": "Chuyển sang Vision mode..."})
                try:
                    extracted = await file_handler.extract_text(file_path, use_vision=True)
                    images = extracted.get("images", [])
                    extracted_text = extracted.get("text", "")
                    use_vision = True
                except Exception as e:
                    logger.warning(f"Exam {exam_id}: Vision fallback failed: {e}")

            _publish_progress(exam_id, "progress", {"percent": 40, "message": "AI đang phân tích câu hỏi..."})

            def _chunk_progress(done: int, total: int):
                pct = 40 + int((done / max(total, 1)) * 35)
                _publish_progress(exam_id, "progress", {
                    "percent": pct,
                    "message": f"AI đang xử lý... ({done}/{total} phần)",
                })

            # Heartbeat task — sends SSE every 30s to keep connection alive
            # and show the user parsing is still in progress.
            _heartbeat_active = True
            _heartbeat_elapsed = [0]

            async def _heartbeat():
                while _heartbeat_active:
                    await asyncio.sleep(30)
                    if not _heartbeat_active:
                        break
                    _heartbeat_elapsed[0] += 30
                    _publish_progress(exam_id, "progress", {
                        "percent": 50,
                        "message": f"AI đang phân tích... ({_heartbeat_elapsed[0]}s)",
                    })

            heartbeat_task = asyncio.create_task(_heartbeat())

            # This is the LONG operation — no DB session open here.
            # Global 7-minute timeout prevents indefinite hanging when Gemini
            # keeps timing out across all tiers.
            MAX_PARSE_SECONDS = 420  # 7 minutes
            try:
                if use_vision and images:
                    questions = await asyncio.wait_for(
                        ai_parser.parse_images(images, progress_callback=_chunk_progress),
                        timeout=MAX_PARSE_SECONDS,
                    )
                elif extracted_text.strip():
                    questions = await asyncio.wait_for(
                        ai_parser.parse(extracted_text, progress_callback=_chunk_progress),
                        timeout=MAX_PARSE_SECONDS,
                    )
                else:
                    raise ValueError("No content could be extracted from the file")
            except asyncio.TimeoutError:
                raise ValueError(
                    "AI phân tích quá thời gian (>7 phút). API Gemini có thể đang quá tải. "
                    "Vui lòng thử lại sau vài phút."
                )
            finally:
                _heartbeat_active = False
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            if not questions and not use_vision:
                logger.info(f"Exam {exam_id}: Text parse returned empty — trying Vision fallback")
                _publish_progress(exam_id, "progress", {
                    "percent": 60, "message": "Thử lại với Vision mode..."
                })
                try:
                    _vis = await file_handler.extract_text(file_path, use_vision=True)
                    _vis_imgs = _vis.get("images", [])
                    if _vis_imgs:
                        try:
                            questions = await asyncio.wait_for(
                                ai_parser.parse_images(_vis_imgs, progress_callback=_chunk_progress),
                                timeout=300,  # 5 min for vision fallback
                            )
                        except asyncio.TimeoutError:
                            logger.warning(f"Exam {exam_id}: Vision fallback timed out")
                        if questions:
                            use_vision = True
                            logger.info(f"Exam {exam_id}: Vision fallback found {len(questions)} questions")
                except Exception as _ve:
                    logger.warning(f"Exam {exam_id}: Vision fallback failed: {_ve}")

            if not questions:
                mode = "Vision" if use_vision else "Text"
                raise ValueError(
                    f"AI không tìm được câu hỏi nào ({mode} mode). "
                    "Thử bật Vision mode hoặc kiểm tra file có chứa đề toán không."
                )

        _publish_progress(exam_id, "progress", {"percent": 80, "message": f"Đã tìm {len(questions)} câu. Đang lưu..."})

        # ── Phase 5: Save results (FRESH DB session — connection guaranteed alive) ──
        result_json = json.dumps(questions, ensure_ascii=False)
        try:
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = r.scalars().first()
                if exam:
                    exam.status = "completed"
                    exam.result_json = result_json
                    await db.commit()

                    # Delete uploaded file after successful parse
                    try:
                        if exam.file_path and os.path.exists(exam.file_path):
                            os.remove(exam.file_path)
                            exam.file_path = None
                            await db.commit()
                            logger.info(f"Exam {exam_id}: Uploaded file deleted after successful parse")
                    except Exception as del_err:
                        logger.warning(f"Exam {exam_id}: Could not delete uploaded file: {del_err}")

            logger.info(f"Exam {exam_id}: Saved {len(questions)} questions (result_json={len(result_json)} chars)")
        except Exception as save_err:
            # Retry once with a completely new session
            logger.warning(f"Exam {exam_id}: First save attempt failed: {save_err}, retrying...")
            try:
                await asyncio.sleep(1)
                async with AsyncSessionLocal() as db2:
                    r2 = await db2.execute(select(Exam).filter(Exam.id == exam_id))
                    exam2 = r2.scalars().first()
                    if exam2:
                        exam2.status = "completed"
                        exam2.result_json = result_json
                        await db2.commit()
                        logger.info(f"Exam {exam_id}: Retry save succeeded")
            except Exception as retry_err:
                logger.error(f"Exam {exam_id}: Retry save also failed: {retry_err}")
                raise

        # ── Phase 6: Populate Question Bank (separate session) ──
        try:
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = r.scalars().first()
                if exam:
                    await _save_questions_to_bank(db, exam.id, exam.user_id, questions)
        except Exception as save_err:
            logger.error(f"Exam {exam_id}: Bank save failed (exam still complete): {save_err}")

        _publish_progress(exam_id, "complete", {
            "message": f"Hoàn tất! {len(questions)} câu hỏi.",
            "result_json": result_json,
        })

        # Push notification to user's devices
        if user_id:
            try:
                from app.services.push_notification import send_push_to_user
                async with AsyncSessionLocal() as _pdb:
                    await send_push_to_user(
                        _pdb, user_id,
                        title="Phân tích hoàn tất!",
                        body=f"Đề thi đã được phân tích xong: {len(questions)} câu hỏi.",
                        data={"exam_id": exam_id, "type": "parse_complete"},
                    )
            except Exception as _pe:
                logger.debug(f"Push notification failed (non-critical): {_pe}")

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
        try:
            async with AsyncSessionLocal() as _fail_db:
                _r = await _fail_db.execute(select(Exam).filter(Exam.id == exam_id))
                _exam = _r.scalars().first()
                if _exam:
                    _exam.status = "failed"
                    _exam.error_message = stored_msg[:500]
                    try:
                        if _exam.file_path and os.path.exists(_exam.file_path):
                            os.remove(_exam.file_path)
                            _exam.file_path = None
                    except Exception:
                        pass
                    await _fail_db.commit()
        except Exception as db_err:
            logger.error(f"Failed to update exam {exam_id} status: {db_err}")


# ==================== Endpoints ====================

@router.post("/parse", response_model=ParseResponse)
async def parse_file_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    speed: str = Query("balanced", pattern="^(fast|balanced|quality)$"),
    use_vision: bool = Query(False, description="Force Vision mode (recommended for scanned PDFs)"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a math exam file and parse it into structured JSON."""
    from app.core.config import settings

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

    # ── Save file ──
    file_id = str(uuid.uuid4())[:16]  # FIX #8: 16 hex chars = 2^64 space, avoids filename collision
    # FIX: strip path separators from user-supplied filename to prevent directory traversal
    safe_name = os.path.basename(file.filename or "unnamed")
    safe_filename = f"{file_id}_{safe_name}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    # FIX: wrap file write so we can clean up on DB failure (prevents orphaned files)
    try:
        with open(file_path, "wb") as f:
            f.write(content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Không thể lưu file: {e}")

    # Create DB record — clean up file if commit fails to avoid orphans
    exam = Exam(
        user_id=current_user.id,
        filename=file.filename,
        file_path=file_path,
        status="pending",
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

    # Images/scanned PDFs benefit from vision, but let user decide
    # Auto-fallback happens inside process_file if text quality is poor
    background_tasks.add_task(process_file, exam.id, speed, use_vision)

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

    return exam


# ── SSE Streaming endpoint (Sprint 3, Task 18) ──

@router.get("/stream/{job_id}")
async def stream_progress(
    job_id: int,
    token: str = Query(..., description="JWT token (EventSource can't use headers)"),
):
    """Stream real-time progress events via Server-Sent Events.

    EventSource doesn't support custom headers, so token is passed as query param.
    Events: progress (percent + message), complete (result_json), error_event.
    Falls back gracefully — client also has polling fallback.
    """
    from jose import jwt, JWTError
    from app.core.config import settings

    # Verify token manually (EventSource can't use Authorization header)
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
    if exam_status == "completed":
        async def _immediate_complete():
            yield f"event: complete\ndata: {json.dumps({'result_json': exam_result_json}, ensure_ascii=False)}\n\n"
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
            if _exam2 and _exam2.status == "completed":
                await _unsubscribe(job_id, queue)
                _rj = _exam2.result_json
                async def _race_complete():
                    yield f"event: complete\ndata: {json.dumps({'result_json': _rj}, ensure_ascii=False)}\n\n"
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

    items = []
    for e in exams:
        item = ExamResponse.model_validate(e)
        item.question_count = counts.get(e.id, 0)
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