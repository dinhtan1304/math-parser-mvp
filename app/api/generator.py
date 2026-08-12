"""
AI Question Generator API.

Endpoints:
    POST /generate  - Sinh de moi tu tieu chi + cau mau trong ngan hang
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import content_origin
from app.core.config import settings
from app.db.session import get_db
from app.db.models.exam import Exam
from app.db.models.question import Question
from app.db.models.user import User
from app.schemas.generator import (
    GenerateRequest, GenerateResponse, GeneratedQuestion,
    ExamGenerateRequest, PromptGenerateRequest,
    SaveAsExamRequest, SaveAsExamResponse,
    ExamMatrixRequest, ExamMatrixResponse, MatrixDeficit,
)
from app.services.ai_generator import ai_generator

# Difficulty buckets in increasing order; adjacency = immediate neighbours.
_MATRIX_DIFFS = ["NB", "TH", "VD", "VDC"]
_MATRIX_ADJACENCY = {
    "NB": ["TH"],
    "TH": ["NB", "VD"],
    "VD": ["TH", "VDC"],
    "VDC": ["VD"],
}

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_SAMPLES = 5


@router.post("", response_model=GenerateResponse)
async def generate_questions(
    req: GenerateRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate new questions based on criteria.

    1. Try vector similarity search for best samples
    2. Fallback to SQL filter if vectors unavailable
    3. Send samples + criteria to Gemini
    4. Return generated questions
    """

    sample_dicts = []

    def _to_dict(s) -> dict:
        return {
            "question_text": s.question_text,
            "type": s.question_type,
            "topic": s.topic,
            "difficulty": s.difficulty,
            "answer": s.answer or "",
            "solution_steps": s.solution_steps or "[]",
        }

    # Step 1a: Try vector similarity search (finds semantically similar questions)
    try:
        from app.services.vector_search import find_similar
        query_text = f"{req.topic or 'Toan hoc'} {req.question_type or ''} {req.difficulty or ''}".strip()
        similar = await find_similar(
            db, query_text, current_user.id,
            subject_code=req.subject_code or None,
            topic=req.topic if req.topic else None,
            difficulty=req.difficulty if req.difficulty else None,
            limit=MAX_SAMPLES,
        )
        if similar:
            sim_ids = [s["question_id"] for s in similar]
            result = await db.execute(
                select(Question).where(Question.id.in_(sim_ids))
            )
            samples = result.scalars().all()
            sample_dicts = [_to_dict(s) for s in samples]
            logger.info(f"Vector search found {len(sample_dicts)} samples")
    except Exception as e:
        logger.debug(f"Vector search skipped: {e}")

    # Step 1b: Fallback to SQL filter if vector search didn't find enough
    if not sample_dicts:
        conditions = [Question.user_id == current_user.id]
        if req.question_type:
            conditions.append(Question.question_type == req.question_type)
        if req.topic:
            conditions.append(Question.topic == req.topic)
        if req.difficulty:
            conditions.append(Question.difficulty == req.difficulty)

        result = await db.execute(
            select(Question)
            .where(*conditions)
            .order_by(Question.created_at.desc())
            .limit(MAX_SAMPLES)
        )
        samples = result.scalars().all()
        sample_dicts = [_to_dict(s) for s in samples]

    # Step 2: Generate with AI
    try:
        generated = await ai_generator.generate(
            samples=sample_dicts,
            count=req.count,
            q_type=req.question_type or "TN",
            topic=req.topic or "Toan",
            difficulty=req.difficulty or "TH",
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"AI generation failed: {e}")

    # Step 3: Build response
    try:
        questions = [GeneratedQuestion(**q) for q in generated]
    except Exception as e:
        logger.error(f"Failed to parse generated questions: {e}")
        # Fallback: return raw dicts
        questions = []
        for q in generated:
            questions.append(GeneratedQuestion(
                question=q.get("question", ""),
                type=q.get("type", req.question_type or "TN"),
                topic=q.get("topic", req.topic or ""),
                difficulty=q.get("difficulty", req.difficulty or "TH"),
                answer=q.get("answer", ""),
                solution_steps=q.get("solution_steps", []),
            ))

    msg = f"Sinh {len(questions)} cau"
    if sample_dicts:
        msg += f" (tham khao {len(sample_dicts)} cau mau tu ngan hang)"
    else:
        msg += " (khong co cau mau, sinh tu dau)"

    return GenerateResponse(
        questions=questions,
        sample_count=len(sample_dicts),
        message=msg,
    )


@router.post("/exam", response_model=GenerateResponse)
async def generate_exam(
    req: ExamGenerateRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a mixed-difficulty exam.

    Sections define how many questions per difficulty level.
    Uses vector search for smarter sample selection.
    """
    # Try vector search first
    sample_dicts = []
    try:
        from app.services.vector_search import find_similar
        query_text = f"{req.topic or 'Toan'} {req.question_type or ''}"
        similar = await find_similar(
            db, query_text, current_user.id,
            subject_code=req.subject_code or None,
            topic=req.topic or None,
            limit=10,
        )
        if similar:
            sim_ids = [s["question_id"] for s in similar]
            result = await db.execute(
                select(Question).where(Question.id.in_(sim_ids))
            )
            samples = result.scalars().all()
            sample_dicts = [
                {
                    "question_text": s.question_text,
                    "type": s.question_type,
                    "topic": s.topic,
                    "difficulty": s.difficulty,
                    "answer": s.answer or "",
                }
                for s in samples
            ]
            logger.info(f"Exam: vector search found {len(sample_dicts)} samples")
    except Exception as e:
        logger.debug(f"Exam vector search skipped: {e}")

    # Fallback to SQL filter
    if not sample_dicts:
        conditions = [Question.user_id == current_user.id]
        if req.topic:
            conditions.append(Question.topic == req.topic)
        if req.question_type:
            conditions.append(Question.question_type == req.question_type)

        result = await db.execute(
            select(Question)
            .where(*conditions)
            .order_by(Question.created_at.desc())
            .limit(10)
        )
        samples = result.scalars().all()
        sample_dicts = [
            {
                "question_text": s.question_text,
                "type": s.question_type,
                "topic": s.topic,
                "difficulty": s.difficulty,
                "answer": s.answer or "",
            }
            for s in samples
        ]

    sections = [{"difficulty": s.difficulty, "count": s.count} for s in req.sections]

    try:
        generated = await ai_generator.generate_exam(
            samples=sample_dicts,
            sections=sections,
            topic=req.topic,
            q_type=req.question_type,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Exam generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"AI exam generation failed: {e}")

    questions = []
    for q in generated:
        questions.append(GeneratedQuestion(
            question=q.get("question", ""),
            type=q.get("type", ""),
            topic=q.get("topic", req.topic),
            difficulty=q.get("difficulty", ""),
            answer=q.get("answer", ""),
            solution_steps=q.get("solution_steps", []),
        ))

    total = sum(s.count for s in req.sections)
    return GenerateResponse(
        questions=questions,
        sample_count=len(sample_dicts),
        message=f"De kiem tra {len(questions)}/{total} cau ({len(sample_dicts)} cau mau)",
    )

@router.post("/from-prompt", response_model=GenerateResponse)
async def generate_from_prompt(
    req: PromptGenerateRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """RAG: Sinh đề từ mô tả tự do tiếng Việt.

    Enhanced with hybrid document RAG:
    - Searches uploaded documents (SGK, tài liệu) for background knowledge
    - Searches question bank for similar example questions
    - Combines both into rich context for AI generation

    Ví dụ prompt:
    - "10 câu TN lớp 8 về hằng đẳng thức và phân thức, mix NB/TH/VD"
    - "Tạo đề ôn thi HK2 lớp 9 tập trung hệ phương trình và bất phương trình"
    - "5 câu tự luận lớp 12 về đạo hàm mức VDC"
    """
    result = None

    # Try hybrid document RAG first (searches doc chunks + question embeddings)
    try:
        from app.services.document_rag import rag_generate_exam
        result = await rag_generate_exam(
            db=db,
            prompt=req.prompt,
            user_id=current_user.id,
            subject_code_override=req.subject_code,
            grade_override=req.grade,
            count_override=req.count,
        )
        logger.info(
            f"Document RAG: {len(result.get('questions', []))} questions, "
            f"context={result.get('context_stats', {})}"
        )
    except Exception as e:
        logger.warning(f"Document RAG failed, falling back to standard RAG: {e}")

    # Fallback to standard rag_generator (question-only RAG)
    if not result or not result.get("questions"):
        try:
            from app.services.rag_generator import generate_from_prompt as _rag_generate
            result = await _rag_generate(
                db=db,
                prompt=req.prompt,
                user_id=current_user.id,
                grade_override=req.grade,
                count_override=req.count,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logger.error(f"RAG generation failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"RAG generation failed: {e}")

    questions = []
    for q in result["questions"]:
        questions.append(GeneratedQuestion(
            question=q.get("question", ""),
            type=q.get("type", "TN"),
            topic=q.get("topic", ""),
            difficulty=q.get("difficulty", "TH"),
            grade=q.get("grade"),
            chapter=q.get("chapter", ""),
            lesson_title=q.get("lesson_title", ""),
            answer=q.get("answer", ""),
            solution_steps=q.get("solution_steps", []),
        ))

    return GenerateResponse(
        questions=questions,
        sample_count=result.get("sample_count", 0),
        message=result.get("message", ""),
        context_stats=result.get("context_stats"),
    )


@router.post("/save-as-exam", response_model=SaveAsExamResponse)
async def save_generated_as_exam(
    req: SaveAsExamRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save AI-generated questions as an Exam in the DB so it can be assigned to a class."""
    # Detect dominant subject from questions
    subj = "toan"
    if req.questions:
        subj = req.questions[0].subject_code or "toan"
    exam = Exam(
        user_id=current_user.id,
        filename=f"AI: {req.title}",
        subject_code=subj,
        status="completed",
        origin=content_origin.AI_GENERATED,
        ai_model=settings.GEMINI_MODEL,
    )
    db.add(exam)
    await db.flush()  # get exam.id

    for i, q in enumerate(req.questions):
        steps_json = json.dumps(q.solution_steps, ensure_ascii=False) if q.solution_steps else None
        question = Question(
            exam_id=exam.id,
            user_id=current_user.id,
            question_text=q.question,
            subject_code=q.subject_code or "toan",
            question_type=q.type or "TN",
            topic=q.topic or None,
            difficulty=q.difficulty or None,
            answer=q.answer or None,
            solution_steps=steps_json,
            question_order=i,
            # Nội dung do AI sinh — giáo viên lưu lại nghĩa là đã xem qua, nhưng
            # chỉ đánh dấu đã duyệt khi đi qua luồng review tường minh.
            origin=content_origin.AI_GENERATED,
            ai_model=settings.GEMINI_MODEL,
        )
        db.add(question)

    await db.flush()
    # Collect IDs before commit
    q_result = await db.execute(
        select(Question.id).where(Question.exam_id == exam.id)
    )
    created_ids = [row[0] for row in q_result.fetchall()]

    await db.commit()
    await db.refresh(exam)

    # Background: generate embeddings for duplicate detection
    if created_ids:
        import asyncio as _aio

        _ids_snapshot = list(created_ids)

        async def _embed_saved():
            from app.db.session import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.vector_search import embed_questions
                    await embed_questions(_db, _ids_snapshot)
            except Exception as e:
                logger.debug(f"Embed after save-as-exam: {e}")

        _aio.create_task(_embed_saved())

    return SaveAsExamResponse(exam_id=exam.id, question_count=len(req.questions))


@router.post("/exam-matrix", response_model=ExamMatrixResponse)
async def generate_exam_from_matrix(
    req: ExamMatrixRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build an exam from the teacher's bank using a chapter × difficulty matrix.

    For each (chapter, difficulty) cell, randomly pulls `count` matching bank
    questions (subject_code + grade scoped to the current user). Copies the
    selected questions into a new Exam so the bank stays intact. Reports any
    cells that could not be fully filled as `deficits`.
    """

    async def _pick(chapter: str, difficulty: str, count: int, used: set[int]) -> list[Question]:
        if count <= 0:
            return []
        stmt = select(Question).where(
            Question.user_id == current_user.id,
            Question.subject_code == req.subject_code,
            Question.grade == req.grade,
            Question.chapter == chapter,
            Question.difficulty == difficulty,
        )
        if used:
            stmt = stmt.where(Question.id.notin_(used))
        stmt = stmt.order_by(func.random()).limit(count)
        rows = list((await db.execute(stmt)).scalars().all())
        for r in rows:
            used.add(r.id)
        return rows

    used_ids: set[int] = set()
    picks: list[Question] = []
    deficits: list[MatrixDeficit] = []

    for cell in req.cells:
        for diff in _MATRIX_DIFFS:
            requested = int(cell.counts.get(diff, 0) or 0)
            if requested <= 0:
                continue
            rows = await _pick(cell.chapter, diff, requested, used_ids)
            picks.extend(rows)
            short = requested - len(rows)

            # Optionally backfill the shortfall from adjacent difficulties.
            if short > 0 and req.fill_from_adjacent_difficulty:
                for adj in _MATRIX_ADJACENCY.get(diff, []):
                    if short <= 0:
                        break
                    extra = await _pick(cell.chapter, adj, short, used_ids)
                    picks.extend(extra)
                    short -= len(extra)

            if short > 0:
                deficits.append(MatrixDeficit(
                    chapter=cell.chapter, difficulty=diff,
                    requested=requested, found=requested - short,
                ))

    if deficits and not req.allow_partial:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Ngân hàng không đủ câu cho ma trận yêu cầu.",
                "deficits": [d.model_dump() for d in deficits],
            },
        )

    if not picks:
        raise HTTPException(
            status_code=400,
            detail="Không tìm thấy câu hỏi nào khớp ma trận trong ngân hàng của bạn.",
        )

    # Đề ráp từ ngân hàng = bản sao câu nguồn → nhãn nguồn gốc phải đi theo câu
    # gốc. Đề mang nhãn AI nếu chứa bất kỳ câu nào có AI tham gia.
    exam_has_ai = any(content_origin.is_ai_origin(src.origin) for src in picks)
    exam_ai_model = next((src.ai_model for src in picks if src.ai_model), None)

    exam = Exam(
        user_id=current_user.id,
        filename=f"Ma trận: {req.title}",
        subject_code=req.subject_code,
        grade=req.grade,
        status="completed",
        origin=content_origin.AI_ASSISTED if exam_has_ai else content_origin.HUMAN,
        ai_model=exam_ai_model,
    )
    db.add(exam)
    await db.flush()  # get exam.id

    for i, src in enumerate(picks):
        db.add(Question(
            exam_id=exam.id,
            user_id=current_user.id,
            question_text=src.question_text,
            content_hash=src.content_hash,
            subject_code=src.subject_code or req.subject_code,
            question_type=src.question_type,
            topic=src.topic,
            difficulty=src.difficulty,
            grade=src.grade,
            chapter=src.chapter,
            lesson_title=src.lesson_title,
            answer=src.answer,
            answer_source=src.answer_source,
            solution_steps=src.solution_steps,
            is_public=False,
            question_order=i,
            origin=src.origin or content_origin.HUMAN,
            ai_model=src.ai_model,
            reviewed_by_user=src.reviewed_by_user,
            reviewed_at=src.reviewed_at,
        ))

    await db.flush()
    q_result = await db.execute(select(Question.id).where(Question.exam_id == exam.id))
    created_ids = [row[0] for row in q_result.fetchall()]

    await db.commit()
    await db.refresh(exam)

    if created_ids:
        import asyncio as _aio

        _ids_snapshot = list(created_ids)

        async def _embed_saved():
            from app.db.session import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.vector_search import embed_questions
                    await embed_questions(_db, _ids_snapshot)
            except Exception as e:
                logger.debug(f"Embed after exam-matrix: {e}")

        _aio.create_task(_embed_saved())

    return ExamMatrixResponse(
        exam_id=exam.id,
        question_count=len(picks),
        deficits=deficits,
    )
