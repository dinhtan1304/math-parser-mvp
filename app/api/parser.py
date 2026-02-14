import os
import uuid
import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Query, Depends
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
    """
    try:
        # Xóa questions cũ nếu re-parse
        from sqlalchemy import delete
        await db.execute(
            delete(Question).where(Question.exam_id == exam_id)
        )

        # Insert từng câu
        for i, q in enumerate(questions):
            question = Question(
                exam_id=exam_id,
                user_id=user_id,
                question_text=q.get("question", ""),
                question_type=q.get("type"),
                topic=q.get("topic"),
                difficulty=q.get("difficulty"),
                answer=q.get("answer"),
                solution_steps=json.dumps(q.get("solution_steps", []), ensure_ascii=False),
                question_order=i + 1,
            )
            db.add(question)

        await db.commit()
        logger.info(f"Exam {exam_id}: Saved {len(questions)} questions to bank")

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

            # Step 1: Extract content
            extracted = await file_handler.extract_text(exam.file_path, use_vision=use_vision)
            extracted_text = extracted.get("text", "")
            images = extracted.get("images", [])

            # Auto-fallback: text mode failed → try vision
            if not use_vision and (not extracted_text.strip() or _is_math_text_poor_quality(extracted_text)):
                logger.info(f"Exam {exam_id}: Text quality poor, falling back to Vision mode")
                try:
                    extracted = await file_handler.extract_text(exam.file_path, use_vision=True)
                    images = extracted.get("images", [])
                    extracted_text = extracted.get("text", "")
                    use_vision = True
                except Exception as e:
                    logger.warning(f"Exam {exam_id}: Vision fallback failed: {e}")

            # Step 2: Parse with AI
            if use_vision and images:
                questions = await ai_parser.parse_images(images)
            elif extracted_text.strip():
                questions = await ai_parser.parse(extracted_text)
            else:
                raise ValueError("No content could be extracted from the file")

            # Step 3: Save result
            exam.status = "completed"
            exam.result_json = json.dumps(questions, ensure_ascii=False)
            await db.commit()

            # Step 4: Populate Question Bank
            await _save_questions_to_bank(db, exam.id, exam.user_id, questions)

        except Exception as e:
            logger.error(f"Error processing exam {exam_id}: {e}", exc_info=True)
            try:
                # Use a fresh query to avoid stale state after potential rollback
                await db.rollback()
                result = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = result.scalars().first()
                if exam:
                    exam.status = "failed"
                    exam.error_message = str(e)[:500]  # Truncate long errors
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
    allowed_extensions = {'.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg', '.txt', '.md'}
    file_ext = os.path.splitext(file.filename or "")[1].lower()

    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"File type '{file_ext}' not supported")

    # Save file
    file_id = str(uuid.uuid4())[:8]
    safe_filename = f"{file_id}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

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