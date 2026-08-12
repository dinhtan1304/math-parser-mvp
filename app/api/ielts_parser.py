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
from sqlalchemy import text as sa_text
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import content_origin
from app.core.config import settings
from app.api.parser import (
    _publish_progress,
    _background_tasks,
    ParseResponse,
    UPLOAD_DIR,
)
from app.db.session import AsyncSessionLocal, get_db
from app.db.models.exam import Exam
from app.db.models.quiz import Quiz, QuizTheory, QuizTheorySection, QuizQuestion
from app.db.models.question import Question, _question_hash
from app.db.models.user import User
from app.services.ai_parser import AIQuestionParser

logger = logging.getLogger(__name__)

router = APIRouter()

RAG_WAIT_TIMEOUT_SECONDS = 20


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/parse-ielts", response_model=ParseResponse)
async def parse_ielts_file_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    use_vision: bool = Query(False, description="Deprecated; Gemini Vision is disabled and local OCR is always used"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload IELTS exam PDF → parse → tạo Quiz tự động."""
    from app.core.config import settings

    allowed_extensions = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt", ".md"}
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
                # Trích xuất từ đề IELTS người dùng tải lên (OCR + AI parse).
                origin=content_origin.OCR_IMPORT,
                ai_model=settings.GEMINI_MODEL,
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
        ingest_warnings: list[str] = []
        ingest_stats: dict = {
            "questions_saved": 0,
            "question_embeddings": 0,
            "document_chunks": 0,
            "rag_index_status": "pending",
        }
        rag_task: asyncio.Task | None = None

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

        async def _run_document_rag_branch():
            async with AsyncSessionLocal() as rag_db:
                from app.services.hybrid_ingest import index_document_for_rag
                return await index_document_for_rag(
                    rag_db,
                    file_path=file_path,
                    exam_id=exam_id,
                    user_id=user_id,
                    subject_code="ielts",
                    grade=None,
                )

        # Phase 2: Local OCR only (Gemini Vision đã loại bỏ)
        try:
            from app.services.local_ocr_service import extract_local_ocr_artifact
            from app.services.marker_ocr import _estimate_page_count

            # Sprint 7.1 + hotfix: dùng real page count thay heuristic file size
            _base = int(os.getenv("OCR_TOTAL_TIMEOUT_BASE", "180"))
            _per_page = int(os.getenv("OCR_TOTAL_TIMEOUT_PER_PAGE", "90"))
            _cap = int(os.getenv("OCR_TOTAL_TIMEOUT_CAP", "1800"))
            real_pages = _estimate_page_count(file_path)
            ocr_timeout = min(_cap, _base + _per_page * real_pages)
            logger.info("IELTS OCR adaptive timeout: %ds (real_pages=%d)", ocr_timeout, real_pages)
            ocr_artifact = await asyncio.wait_for(
                extract_local_ocr_artifact(file_path, "ielts"),
                timeout=ocr_timeout,
            )
        except asyncio.TimeoutError:
            raise ValueError(
                f"Local OCR timeout sau {ocr_timeout}s cho file {real_pages} trang. "
                "Marker chay cham. Tang env OCR_TOTAL_TIMEOUT_PER_PAGE."
            )

        text = ocr_artifact.get("text", "")
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
        })
        if file_path:
            rag_task = asyncio.create_task(_run_document_rag_branch())
            _background_tasks.add(rag_task)
            rag_task.add_done_callback(_background_tasks.discard)
            _publish_progress(exam_id, "progress", {
                "percent": 18,
                "branch": "rag",
                "message": "Dang index tai lieu IELTS cho RAG tu OCR artifact...",
            })
        if not text.strip():
            raise ValueError(
                "Local OCR khong doc duoc noi dung file IELTS. Gemini Vision da tat; "
                "hay kiem tra Marker/Pix2Text/Docling hoac thu file scan ro hon."
            )

        _publish_progress(exam_id, "progress", {"percent": 15, "message": "Dang chuan hoa IELTS JSON bang Gemini text..."})

        # Phase 3: Gemini IELTS text parse
        parser = AIQuestionParser()

        async def _progress_cb(pct: int, msg: str):
            _publish_progress(exam_id, "progress", {"percent": pct, "message": msg})

        flat_questions = await parser.parse_ielts(text, progress_callback=_progress_cb)
        try:
            usage = getattr(parser, "_token_usage", {}) or {}
            ingest_stats["ai_text_calls"] = int(usage.get("calls") or 0)
            ingest_stats["estimated_input_tokens"] = int(usage.get("input") or 0)
            ingest_stats["estimated_output_tokens"] = int(usage.get("output") or 0)
            ingest_stats["ai_vision_calls"] = 0
        except Exception:
            pass

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
            saved_bank_ids = [int(qid) for qid in bank_ids if qid]
            ingest_stats["questions_saved"] = len(saved_bank_ids)
            if saved_bank_ids:
                try:
                    async with AsyncSessionLocal() as emb_db:
                        from app.services.vector_search import embed_questions
                        await embed_questions(emb_db, saved_bank_ids)
                        placeholders = ",".join(str(i) for i in saved_bank_ids)
                        emb_count = (await emb_db.execute(sa_text(
                            f"SELECT COUNT(*) FROM question_embedding WHERE question_id IN ({placeholders})"
                        ))).scalar() or 0
                        ingest_stats["question_embeddings"] = int(emb_count)
                except Exception as emb_err:
                    ingest_warnings.append(f"Tạo IELTS question embeddings thất bại: {emb_err}")
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
                _publish_progress(exam_id, "progress", {
                    "percent": 90,
                    "branch": "rag",
                    "message": "Index RAG dang chay nen, tiep tuc tao quiz...",
                })
            except Exception as rag_err:
                ingest_stats["rag_index_status"] = "failed"
                ingest_warnings.append(f"Index tài liệu IELTS RAG thất bại: {rag_err}")

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
            from app.services.hybrid_ingest import merge_ingest_metadata, normalize_warnings
            ingest_warnings = normalize_warnings(ingest_warnings)
            exam_obj.result_json = merge_ingest_metadata({
                "type": "ielts",
                "quiz_id": quiz.id,
                "quiz_code": quiz.code,
                "question_count": total_questions,
            }, warnings=ingest_warnings, ingest_stats=ingest_stats)
            try:
                keep_for_retry = ingest_stats.get("rag_index_status") in {"failed", "warning"}
                if not keep_for_retry and exam_obj.file_path and os.path.exists(exam_obj.file_path):
                    os.remove(exam_obj.file_path)
                    exam_obj.file_path = None
            except Exception as del_err:
                logger.warning(f"IELTS exam {exam_id}: could not delete uploaded file: {del_err}")
            await db.commit()
            quiz_id = quiz.id
            quiz_code = quiz.code

        # Phase 6: SSE complete
        _publish_progress(exam_id, "complete", {
            "status": "completed",
            "quiz_id": quiz_id,
            "quiz_code": quiz_code,
            "question_count": total_questions,
            "warnings": ingest_warnings,
            "ingest_stats": ingest_stats,
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
                    try:
                        if exam.file_path and os.path.exists(exam.file_path):
                            os.remove(exam.file_path)
                            exam.file_path = None
                    except Exception as del_err:
                        logger.warning(f"IELTS exam {exam_id}: could not delete failed upload: {del_err}")
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
