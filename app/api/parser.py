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
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import file_handler, ai_parser_service as ai_parser
from app.api import deps
from app.db.session import AsyncSessionLocal, get_db
from app.db.models.exam import Exam
from app.db.models.question import Question
from app.db.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── SSE progress tracking (Sprint 3, Task 18) ──
# In-memory store: exam_id → asyncio.Queue of SSE events
_progress_queues: Dict[int, List[asyncio.Queue]] = {}


def _publish_progress(exam_id: int, event: str, data: dict):
    """Publish a progress event to all connected SSE clients."""
    queues = _progress_queues.get(exam_id, [])
    msg = json.dumps(data, ensure_ascii=False)
    for q in queues:
        try:
            q.put_nowait((event, msg))
        except asyncio.QueueFull:
            pass  # Drop if client is too slow


def _subscribe(exam_id: int) -> asyncio.Queue:
    """Subscribe to progress events for an exam."""
    q = asyncio.Queue(maxsize=100)
    if exam_id not in _progress_queues:
        _progress_queues[exam_id] = []
    _progress_queues[exam_id].append(q)
    return q


def _unsubscribe(exam_id: int, q: asyncio.Queue):
    """Unsubscribe from progress events."""
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

    class Config:
        from_attributes = True


class ExamListResponse(BaseModel):
    items: List[ExamResponse]
    total: int
    page: int
    page_size: int


# ==================== Helpers ====================

def _is_math_text_poor_quality(text: str) -> bool:
    """Heuristic: check if extracted text is garbage or missing math symbols."""
    if not text or len(text) < 50:
        return True

    # Math-related markers that should appear in a math exam
    math_markers = [
        '=', '+', '-', '/', '^', '²', '³',
        'x', 'y', 'sin', 'cos', 'tan', 'log', 'ln',
        'lim', 'sqrt', 'frac', 'pi',
        'Câu', 'Bài', 'câu', 'bài',
    ]
    marker_count = sum(1 for m in math_markers if m in text)

    # A math exam should have at least a few math markers
    if marker_count < 3:
        return True

    # Too many garbled characters (ratio of non-printable / total)
    garbled = sum(1 for c in text if ord(c) > 0xFFFF or (ord(c) < 32 and c not in '\n\r\t'))
    if len(text) > 0 and garbled / len(text) > 0.1:
        return True

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
        new_questions = []
        for i, q in enumerate(questions):
            q_text = q.get("question", "")
            if not q_text.strip():
                continue
            c_hash = _question_hash(q_text)
            new_questions.append((i, q, c_hash))

        # Find existing hashes for this user
        if new_questions:
            hashes = [h for _, _, h in new_questions]
            existing_result = await db.execute(
                select(Question.content_hash).filter(
                    Question.user_id == user_id,
                    Question.content_hash.in_(hashes),
                )
            )
            existing_hashes = {row[0] for row in existing_result.fetchall()}
        else:
            existing_hashes = set()

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
                topic=q.get("topic"),
                difficulty=q.get("difficulty"),
                answer=q.get("answer"),
                solution_steps=json.dumps(q.get("solution_steps", []), ensure_ascii=False),
                question_order=i + 1,
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

        # Sync FTS5 index (background, non-blocking)
        try:
            from app.services.fts import sync_fts_questions
            await sync_fts_questions(db, saved_ids)
            logger.info(f"Exam {exam_id}: FTS indexed {len(saved_ids)} questions")
        except Exception as e:
            logger.debug(f"FTS sync skipped: {e}")

        # Generate vector embeddings (background, non-blocking)
        try:
            from app.services.vector_search import embed_questions
            await embed_questions(db, saved_ids)
            logger.info(f"Exam {exam_id}: Embedded {len(saved_ids)} questions")
        except Exception as e:
            logger.debug(f"Embedding skipped: {e}")

    except Exception as e:
        logger.error(f"Exam {exam_id}: Failed to save questions to bank: {e}")
        await db.rollback()


async def process_file(exam_id: int, speed: str = "balanced", use_vision: bool = False):
    """Background task: extract text from file and parse with AI."""
    async with AsyncSessionLocal() as db:
        try:
            # Get exam
            result = await db.execute(select(Exam).filter(Exam.id == exam_id))
            exam = result.scalars().first()
            if not exam:
                return

            exam.status = "processing"
            await db.commit()

            _publish_progress(exam_id, "progress", {"percent": 10, "message": "Đang trích xuất nội dung..."})

            # Step 1: Extract content
            extracted = await file_handler.extract_text(exam.file_path, use_vision=use_vision)
            extracted_text = extracted.get("text", "")
            images = extracted.get("images", [])
            file_hash = extracted.get("file_hash", "")

            # Save file hash
            if file_hash:
                exam.file_hash = file_hash
                await db.commit()

            _publish_progress(exam_id, "progress", {"percent": 25, "message": "Trích xuất xong. Đang phân tích..."})

            # ── Sprint 3, Task 19: Gemini Cache — check if same file already parsed ──
            questions = None
            if file_hash:
                cache_result = await db.execute(
                    select(Exam.result_json).filter(
                        Exam.user_id == exam.user_id,
                        Exam.file_hash == file_hash,
                        Exam.status == "completed",
                        Exam.result_json.isnot(None),
                        Exam.id != exam_id,  # Not self
                    ).order_by(Exam.created_at.desc()).limit(1)
                )
                cached_json = cache_result.scalar()
                if cached_json:
                    try:
                        questions = json.loads(cached_json)
                        logger.info(f"Exam {exam_id}: Cache HIT (hash={file_hash[:8]}), reusing {len(questions)} questions")
                        _publish_progress(exam_id, "progress", {"percent": 70, "message": f"Cache hit! Tìm thấy {len(questions)} câu đã phân tích."})
                    except json.JSONDecodeError:
                        questions = None  # Cache corrupted, re-parse

            # Auto-fallback: text mode failed → try vision
            if questions is None:
                if not use_vision and (not extracted_text.strip() or _is_math_text_poor_quality(extracted_text)):
                    logger.info(f"Exam {exam_id}: Text quality poor, falling back to Vision mode")
                    _publish_progress(exam_id, "progress", {"percent": 30, "message": "Chuyển sang Vision mode..."})
                    try:
                        extracted = await file_handler.extract_text(exam.file_path, use_vision=True)
                        images = extracted.get("images", [])
                        extracted_text = extracted.get("text", "")
                        use_vision = True
                    except Exception as e:
                        logger.warning(f"Exam {exam_id}: Vision fallback failed: {e}")

                _publish_progress(exam_id, "progress", {"percent": 40, "message": "AI đang phân tích câu hỏi..."})

                # Step 2: Parse with AI
                if use_vision and images:
                    questions = await ai_parser.parse_images(images)
                elif extracted_text.strip():
                    questions = await ai_parser.parse(extracted_text)
                else:
                    raise ValueError("No content could be extracted from the file")

                # Validate AI actually found questions
                if not questions:
                    mode = "Vision" if use_vision else "Text"
                    raise ValueError(
                        f"AI không tìm được câu hỏi nào ({mode} mode). "
                        "Thử bật Vision mode hoặc kiểm tra file có chứa đề toán không."
                    )

            _publish_progress(exam_id, "progress", {"percent": 80, "message": f"Đã tìm {len(questions)} câu. Đang lưu..."})

            # Step 3: Save result
            result_json = json.dumps(questions, ensure_ascii=False)
            exam.status = "completed"
            exam.result_json = result_json
            await db.commit()

            # Step 4: Populate Question Bank
            await _save_questions_to_bank(db, exam.id, exam.user_id, questions)

            _publish_progress(exam_id, "complete", {
                "message": f"Hoàn tất! {len(questions)} câu hỏi.",
                "result_json": result_json,
            })

        except Exception as e:
            logger.error(f"Error processing exam {exam_id}: {e}", exc_info=True)
            _publish_progress(exam_id, "error_event", {"message": str(e)[:300]})
            try:
                await db.rollback()
                result = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = result.scalars().first()
                if exam:
                    exam.status = "failed"
                    exam.error_message = str(e)[:500]
                    await db.commit()
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
    file_id = str(uuid.uuid4())[:8]
    safe_filename = f"{file_id}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    with open(file_path, "wb") as f:
        f.write(content)

    # Create DB record
    exam = Exam(
        user_id=current_user.id,
        filename=file.filename,
        file_path=file_path,
        status="pending",
    )
    db.add(exam)
    await db.commit()
    await db.refresh(exam)

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
    queue = _subscribe(job_id)

    async def _event_generator():
        try:
            # Send initial heartbeat
            yield f": connected\n\n"

            timeout_count = 0
            while timeout_count < 300:  # Max 5 min (300 * 1s)
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"event: {event}\ndata: {data}\n\n"

                    # Terminal events
                    if event in ("complete", "error_event"):
                        return
                except asyncio.TimeoutError:
                    timeout_count += 1
                    # Send keepalive every 15s
                    if timeout_count % 15 == 0:
                        yield f": keepalive\n\n"
                    continue

            # Timeout — tell client to fall back to polling
            yield f"event: error_event\ndata: {json.dumps({'message': 'Stream timeout'})}\n\n"
        finally:
            _unsubscribe(job_id, queue)

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
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List parse history for the current user with pagination."""
    from sqlalchemy import func

    # Count total
    count_result = await db.execute(
        select(func.count(Exam.id)).filter(Exam.user_id == current_user.id)
    )
    total = count_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    result = await db.execute(
        select(Exam)
        .filter(Exam.user_id == current_user.id)
        .order_by(Exam.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    exams = result.scalars().all()

    return ExamListResponse(
        items=[ExamResponse.model_validate(e) for e in exams],
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