"""
AI Question Generator API.

Endpoints:
    POST /generate  - Sinh de moi tu tieu chi + cau mau trong ngan hang
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.question import Question
from app.db.models.user import User
from app.schemas.generator import (
    GenerateRequest, GenerateResponse, GeneratedQuestion,
    ExamGenerateRequest,
)
from app.services.ai_generator import ai_generator

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
        query_text = f"{req.topic or 'Toan'} {req.question_type or ''} {req.difficulty or ''}"
        similar = await find_similar(
            db, query_text, current_user.id,
            topic=req.topic or None,
            difficulty=req.difficulty or None,
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