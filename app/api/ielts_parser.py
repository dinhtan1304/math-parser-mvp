"""
IELTS Exam Parser — Luồng riêng biệt với math parser.

Endpoint: POST /parser/parse-ielts
- Upload PDF đề IELTS → OCR → Gemini IELTS schema → Quiz + QuizQuestion
- Dùng chung SSE infrastructure với math parser (cùng stream token + stream endpoints)
- Không ảnh hưởng đến process_file() của math parser
"""

import os
import re
import uuid
import json
import hashlib
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.parser import (
    _publish_progress,
    ParseResponse,
    UPLOAD_DIR,
)
from app.db.session import AsyncSessionLocal, get_db
from app.db.models.exam import Exam
from app.db.models.quiz import Quiz, QuizTheory, QuizTheorySection, QuizQuestion
from app.db.models.question import Question, _question_hash
from app.db.models.user import User
from app.services.pipeline import step1_ocr
from app.services.ai_parser import AIQuestionParser

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/parse-ielts", response_model=ParseResponse)
async def parse_ielts_file_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    use_vision: bool = Query(False, description="Force Vision mode for scanned PDFs"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload IELTS exam PDF → parse → tạo Quiz tự động."""
    from app.core.config import settings

    allowed_extensions = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt"}
    file_ext = os.path.splitext(file.filename or "")[1].lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"File type '{file_ext}' not supported")

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

    # Magic byte validation
    _MAGIC = {
        ".pdf":  [b"%PDF"],
        ".docx": [b"PK\x03\x04"],
        ".doc":  [b"\xd0\xcf\x11\xe0"],
        ".png":  [b"\x89PNG"],
        ".jpg":  [b"\xff\xd8\xff"],
        ".jpeg": [b"\xff\xd8\xff"],
    }
    expected = _MAGIC.get(file_ext)
    if expected and not any(content[: len(m)] == m for m in expected):
        raise HTTPException(status_code=400, detail="File content does not match its extension.")

    # Sanitize filename
    raw_name = file.filename or "unnamed"
    if len(raw_name) > 255:
        raw_name = raw_name[:255]
    sanitized_name = re.sub(r"[^a-zA-Z0-9_\-. ]", "_", os.path.basename(raw_name)) or "unnamed"

    # Save file
    file_id = str(uuid.uuid4())[:16]
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{sanitized_name}")
    try:
        with open(file_path, "wb") as f:
            f.write(content)
    except OSError as e:
        logger.error(f"IELTS file write failed: {e}")
        raise HTTPException(status_code=500, detail="Không thể lưu file.")

    file_hash = hashlib.md5(content).hexdigest()

    exam = Exam(
        user_id=current_user.id,
        filename=file.filename,
        file_path=file_path,
        file_hash=file_hash,
        subject_code="ielts",
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
        raise HTTPException(status_code=500, detail="Không thể tạo exam record.")

    background_tasks.add_task(
        process_ielts_file, exam.id, current_user.id, use_vision
    )

    return ParseResponse(
        job_id=exam.id,
        status="pending",
        message="Đang xử lý đề IELTS. Theo dõi tiến độ qua SSE.",
    )


# ── Bank save ─────────────────────────────────────────────────────────────────

async def _save_ielts_to_bank(
    exam_id: int, user_id: int, flat_questions: list[dict]
) -> list[int | None]:
    """Lưu flat IELTS questions vào Question bank.

    Returns list[Question.id | None] indexed by flat_questions position.
    None = question bị skip (text rỗng).
    """
    seen_hashes: set[str] = set()
    bank_objects: list[Question | None] = []

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(Question.content_hash)
            .where(Question.user_id == user_id, Question.content_hash.isnot(None))
        )
        existing_hashes = {row[0] for row in existing}

        for idx, q in enumerate(flat_questions):
            q_text = (q.get("question_text") or "").strip()
            if not q_text:
                bank_objects.append(None)
                continue

            c_hash = _question_hash(q_text)
            is_dup = c_hash in existing_hashes or c_hash in seen_hashes
            seen_hashes.add(c_hash)

            answer_raw = q.get("answer", "")
            answer_str = (
                json.dumps(answer_raw, ensure_ascii=False)
                if isinstance(answer_raw, (dict, list))
                else str(answer_raw)
            )

            extra = {
                "passage_text":      q.get("passage_text", ""),
                "choices":           _parse_json_field(q.get("choices_json", "")),
                "items":             _parse_json_field(q.get("items_json", "")),
                "word_limit":        q.get("word_limit") or None,
                "global_number":     q.get("global_number"),
                "group_instruction": q.get("group_instruction", ""),
            }

            bank_q = Question(
                exam_id=exam_id,
                user_id=user_id,
                question_text=q_text,
                content_hash=c_hash,
                subject_code="ielts",
                question_type=q.get("question_type", "fill_blank"),
                grade=None,
                chapter=(q.get("section_title") or "IELTS")[:200],
                lesson_title=(q.get("group_instruction") or "")[:200],
                answer=answer_str,
                answer_source="gemini",
                extra_data=json.dumps(extra, ensure_ascii=False),
                question_order=idx + 1,
                is_bank_duplicate=is_dup,
                is_public=False,
            )
            db.add(bank_q)
            bank_objects.append(bank_q)
            if not is_dup:
                existing_hashes.add(c_hash)

        await db.flush()
        ids = [obj.id if obj is not None else None for obj in bank_objects]
        await db.commit()

    return ids


# ── Background task ───────────────────────────────────────────────────────────

async def process_ielts_file(exam_id: int, user_id: int, use_vision: bool = False):
    """OCR → Gemini IELTS parse → tạo Quiz + QuizQuestion.

    Dùng fresh DB sessions sau mỗi phase (tránh Neon idle timeout).
    """
    try:
        # Phase 1: đọc file_path, đặt status=processing
        file_path = None
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = result.scalars().first()
                if not exam:
                    return
                file_path = exam.file_path
                exam.status = "processing"
                await db.commit()
        except Exception as e:
            logger.error(f"IELTS exam {exam_id}: Cannot read exam record: {e}")
            return

        _publish_progress(exam_id, "progress", {"percent": 5, "message": "Đang trích xuất văn bản..."})

        # Phase 2: OCR
        try:
            ocr_result = await asyncio.wait_for(
                step1_ocr(file_path, "ielts"),
                timeout=120,
            )
        except asyncio.TimeoutError:
            raise ValueError("OCR timeout sau 120s. File quá lớn hoặc bị lỗi.")

        text = ocr_result.get("text", "") if isinstance(ocr_result, dict) else str(ocr_result)

        _publish_progress(exam_id, "progress", {"percent": 15, "message": "Đang gửi đến Gemini..."})

        # Phase 3: Gemini IELTS parse
        parser = AIQuestionParser()

        async def _progress_cb(pct: int, msg: str):
            _publish_progress(exam_id, "progress", {"percent": pct, "message": msg})

        text_is_short = not text or len(text.strip()) < 200

        if text_is_short or use_vision:
            # Scanned/image-based PDF (e.g. Cambridge IELTS books) — use Vision OCR
            if text_is_short:
                logger.info(f"IELTS exam {exam_id}: text too short ({len(text.strip())} chars), switching to Vision OCR")
                _publish_progress(exam_id, "progress", {"percent": 15, "message": "PDF dạng ảnh, đang dùng Vision OCR..."})
            else:
                _publish_progress(exam_id, "progress", {"percent": 15, "message": "Đang dùng Vision OCR theo yêu cầu..."})

            from app.services import file_handler as fh_module
            fh = fh_module.FileHandler()
            try:
                vision_result = await asyncio.wait_for(
                    fh.extract_text(file_path, use_vision=True),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                raise ValueError("Vision OCR timeout sau 120s. File quá lớn hoặc bị lỗi.")

            images = vision_result.get("images", [])
            if not images:
                raise ValueError("Không thể chuyển PDF sang ảnh để OCR.")

            _publish_progress(exam_id, "progress", {"percent": 25, "message": f"Đang phân tích {len(images)} trang..."})
            flat_questions = await asyncio.wait_for(
                parser.parse_ielts_vision(images, progress_callback=_progress_cb),
                timeout=300,
            )
        else:
            flat_questions = await parser.parse_ielts(text, progress_callback=_progress_cb)

        if not flat_questions:
            raise ValueError("Gemini không trích xuất được câu hỏi nào từ đề thi.")

        _publish_progress(exam_id, "progress", {
            "percent": 82,
            "message": f"Đã trích xuất {len(flat_questions)} câu hỏi. Đang lưu vào bank...",
        })

        # Phase 4a: Lưu vào Question bank (non-critical — fail thì vẫn tiếp tục)
        bank_ids: list[int | None] = []
        try:
            bank_ids = await _save_ielts_to_bank(exam_id, user_id, flat_questions)
            logger.info(f"IELTS exam {exam_id}: saved {sum(1 for x in bank_ids if x)} questions to bank")
        except Exception as bank_err:
            logger.warning(f"IELTS exam {exam_id}: bank save failed (non-critical): {bank_err}")
            bank_ids = [None] * len(flat_questions)

        _publish_progress(exam_id, "progress", {"percent": 88, "message": "Đang tạo Quiz..."})

        # Phase 4b: Group flat list → sections → groups
        sections = _group_ielts_questions(flat_questions)

        # Phase 5: Tạo Quiz + QuizQuestion (1 DB session cho toàn bộ save)
        quiz_id = None
        quiz_code = None
        total_questions = 0

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Exam).filter(Exam.id == exam_id))
            exam_obj = result.scalars().first()

            quiz = Quiz(
                name=f"IELTS — {exam_obj.filename}",
                created_by_id=user_id,
                subject_code="ielts",
                mode="exam",
                status="draft",
                settings={
                    "shuffle_questions": False,
                    "shuffle_choices": False,
                    "show_correct_after_each": False,
                    "allow_review_after_submit": True,
                    "grading_mode": "auto",
                    "time_limit_minutes": 60,
                },
            )
            db.add(quiz)
            await db.flush()

            order = 0
            flat_index = 0
            for sec_idx, section in enumerate(sections):
                # Tạo QuizTheory để lưu passage text
                theory = QuizTheory(
                    quiz_id=quiz.id,
                    title=section["title"],
                    content_type="rich_text",
                    language="en",
                    display_order=sec_idx,
                )
                db.add(theory)
                await db.flush()

                theory_sec = QuizTheorySection(
                    theory_id=theory.id,
                    order=1,
                    content=section["passage_text"] or "",
                    content_format="markdown",
                )
                db.add(theory_sec)
                await db.flush()

                for group in section["groups"]:
                    for q in group["questions"]:
                        choices = _parse_json_field(q.get("choices_json", ""))
                        items = _parse_json_field(q.get("items_json", ""))
                        answer = _parse_ielts_answer(
                            q.get("answer", ""), q.get("question_type", "fill_blank")
                        )
                        word_limit = q.get("word_limit", "") or None
                        qtype = q.get("question_type", "fill_blank")

                        quiz_q = QuizQuestion(
                            quiz_id=quiz.id,
                            order=order,
                            type=qtype,
                            question_text=q.get("question_text", ""),
                            answer=answer,
                            choices=choices,
                            items=items,
                            points=float(q.get("points", 1.0)),
                            has_correct_answer=(qtype != "essay"),
                            required=True,
                            hint_section_id=theory_sec.id,
                            scoring={
                                "mode": "all_or_nothing",
                                "word_limit": word_limit,
                            },
                            source_type="file_import",
                            origin_question_id=(
                                bank_ids[flat_index] if flat_index < len(bank_ids) else None
                            ),
                            extra_metadata={
                                "global_number": q.get("global_number", order + 1),
                                "group_instruction": group.get("instruction", ""),
                                "ielts_section": section["title"],
                            },
                        )
                        db.add(quiz_q)
                        order += 1
                        flat_index += 1

            quiz.question_count = order
            total_questions = order
            exam_obj.status = "completed"
            await db.commit()
            quiz_id = quiz.id
            quiz_code = quiz.code

        # Phase 6: SSE complete
        _publish_progress(exam_id, "complete", {
            "status": "completed",
            "quiz_id": quiz_id,
            "quiz_code": quiz_code,
            "question_count": total_questions,
        })

        logger.info(f"IELTS exam {exam_id}: created Quiz {quiz_code} with {total_questions} questions")

    except Exception as e:
        logger.error(f"IELTS exam {exam_id}: process failed: {e}", exc_info=True)
        # Fresh session để tránh Neon idle timeout
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Exam).filter(Exam.id == exam_id))
                exam = result.scalars().first()
                if exam:
                    exam.status = "failed"
                    exam.error_message = str(e)[:1000]
                    await db.commit()
        except Exception as db_err:
            logger.error(f"IELTS exam {exam_id}: Cannot update failed status: {db_err}")
        _publish_progress(exam_id, "error_event", {"message": str(e)})


# ── Helper functions ──────────────────────────────────────────────────────────

def _group_ielts_questions(flat: list[dict]) -> list[dict]:
    """Group flat Gemini output → sections → groups.

    Preserves insertion order (dict is ordered in Python 3.7+).
    """
    sections: dict[str, dict] = {}

    for q in flat:
        st = q.get("section_title", "Section 1")
        if not st:
            st = "Section 1"

        if st not in sections:
            sections[st] = {
                "title": st,
                "passage_text": q.get("passage_text", ""),
                "groups": {},
            }
        elif q.get("passage_text"):
            # Gemini đôi khi lặp passage_text — chỉ giữ lần đầu không rỗng
            if not sections[st]["passage_text"]:
                sections[st]["passage_text"] = q["passage_text"]

        gi = q.get("group_instruction", "")
        if gi not in sections[st]["groups"]:
            sections[st]["groups"][gi] = {
                "instruction": gi,
                "questions": [],
            }
        sections[st]["groups"][gi]["questions"].append(q)

    result = []
    for s in sections.values():
        s["groups"] = list(s["groups"].values())
        result.append(s)
    return result


def _parse_json_field(val) -> list | None:
    """Parse choices_json / items_json — accepts string (JSON-encoded) or list."""
    if val is None:
        return None
    if isinstance(val, list):
        return val if val else None
    if not isinstance(val, str) or val.strip() in ("", "[]", '""'):
        return None
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) and parsed else None
    except Exception:
        return None


def _parse_ielts_answer(answer_str: str, qtype: str):
    """Convert answer string → đúng Python type theo question type."""
    if qtype in ("matching", "matching_headings", "fill_blank"):
        if answer_str and answer_str.strip().startswith("{"):
            try:
                return json.loads(answer_str)
            except Exception:
                pass
    return answer_str or ""
