"""
IELTS Exam Generator from Bank.

Endpoint:
    POST /generate/ielts-exam
        Compose a new IELTS Quiz by selecting passage units from the user's
        Question bank according to a template (full_test, mini_test, ...).
"""

import json
import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.quiz import Quiz, QuizTheory, QuizTheorySection, QuizQuestion
from app.services import ielts_assembler

logger = logging.getLogger(__name__)
router = APIRouter()


TemplateLiteral = Literal[
    "full_test", "reading_only", "listening_only", "writing_only", "mini_test"
]


class IeltsGenerateRequest(BaseModel):
    template: TemplateLiteral = Field(..., description="Cấu trúc đề")
    name: str = Field(..., min_length=1, max_length=200, description="Tên đề mới")
    mix_strategy: Literal["rag_generate", "reuse_passage"] = Field(
        "rag_generate",
        description="rag_generate uses Hybrid RAG; reuse_passage assembles existing bank passages",
    )
    prompt: Optional[str] = Field(default=None, max_length=2000)
    exclude_quiz_ids: Optional[list[int]] = Field(
        default=None, description="Danh sách quiz_id cần tránh trùng passage"
    )
    seed: Optional[int] = Field(default=None, description="Seed random (debug/test)")


class IeltsGenerateSection(BaseModel):
    kind: str
    title: str
    n_questions: int


class IeltsGenerateResponse(BaseModel):
    quiz_id: int
    quiz_code: str
    question_count: int
    sections: list[IeltsGenerateSection]
    message: str
    context_stats: Optional[dict] = None


@router.post("/ielts-exam", response_model=IeltsGenerateResponse)
async def generate_ielts_exam(
    req: IeltsGenerateRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.mix_strategy == "rag_generate":
        try:
            return await _generate_ielts_exam_with_rag(db, current_user.id, req)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logger.error(f"IELTS RAG generate failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Tạo đề IELTS bằng RAG thất bại: {e}")

    try:
        quiz = await ielts_assembler.assemble_ielts_quiz(
            db,
            user_id=current_user.id,
            template=req.template,
            name=req.name.strip(),
            exclude_quiz_ids=req.exclude_quiz_ids or [],
            seed=req.seed,
        )
    except ielts_assembler.NotEnoughBankError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"IELTS generate failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Tạo đề thất bại: {e}")

    # Build section summary by reloading theories (committed in assembler)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.db.models.quiz import Quiz, QuizTheory, QuizQuestion

    res = await db.execute(
        select(Quiz)
        .options(selectinload(Quiz.theories))
        .where(Quiz.id == quiz.id)
    )
    refreshed = res.scalars().first()

    # Count questions per theory by mapping hint_section_id → theory.title
    qres = await db.execute(
        select(QuizQuestion).where(QuizQuestion.quiz_id == quiz.id)
    )
    quiz_qs = qres.scalars().all()
    theory_id_to_count: dict[int, int] = {}
    for q in quiz_qs:
        if q.hint_section_id is not None:
            theory_id_to_count[q.hint_section_id] = (
                theory_id_to_count.get(q.hint_section_id, 0) + 1
            )

    sections: list[IeltsGenerateSection] = []
    for t in (refreshed.theories or []):
        # Find the theory's first section id (1:1 in our assembler)
        from app.db.models.quiz import QuizTheorySection
        sres = await db.execute(
            select(QuizTheorySection).where(QuizTheorySection.theory_id == t.id)
        )
        secs = sres.scalars().all()
        sec_id = secs[0].id if secs else None
        n = theory_id_to_count.get(sec_id, 0) if sec_id is not None else 0
        kind = ielts_assembler._classify_skill(t.title) or "unknown"
        sections.append(IeltsGenerateSection(
            kind=kind, title=t.title, n_questions=n,
        ))

    return IeltsGenerateResponse(
        quiz_id=quiz.id,
        quiz_code=quiz.code,
        question_count=quiz.question_count,
        sections=sections,
        message=f"Đã tạo đề '{quiz.name}' với {quiz.question_count} câu.",
    )


async def _generate_ielts_exam_with_rag(
    db: AsyncSession,
    user_id: int,
    req: IeltsGenerateRequest,
) -> IeltsGenerateResponse:
    from google.genai import types
    from app.services.ai_generator import ai_generator
    from app.services.ai_parser import _SAFETY_SETTINGS
    from app.services.document_rag import hybrid_search

    if not ai_generator._client:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    template_spec = _ielts_template_spec(req.template)
    user_prompt = req.prompt or f"Create an IELTS {req.template.replace('_', ' ')} practice test."
    rag = await hybrid_search(
        db, user_prompt, user_id,
        subject_code="ielts", grade=None,
        doc_limit=8, q_limit=8, min_similarity=0.2,
    )
    prompt = _IELTS_RAG_PROMPT.format(
        template=req.template,
        spec=json.dumps(template_spec, ensure_ascii=False),
        user_prompt=user_prompt,
        doc_context=_format_ielts_doc_context(rag.get("doc_chunks", [])) or "(No uploaded IELTS document context found.)",
        sample_context=_format_ielts_question_context(rag.get("similar_questions", [])) or "(No similar IELTS questions found.)",
    )
    response = await ai_generator._client.aio.models.generate_content(
        model=ai_generator.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.55,
            max_output_tokens=32000,
            safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
        ),
    )
    payload = _extract_json_object(ai_generator._safe_text(response))
    sections_payload = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections_payload, list) or not sections_payload:
        raise RuntimeError("Gemini không trả về cấu trúc IELTS hợp lệ.")

    quiz = Quiz(
        name=req.name.strip(),
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
            "generation_source": "hybrid_rag",
        },
    )
    db.add(quiz)
    await db.flush()

    order = 0
    summaries: list[IeltsGenerateSection] = []
    for sec_idx, section in enumerate(sections_payload):
        title = str(section.get("title") or f"IELTS Section {sec_idx + 1}")[:200]
        kind = str(section.get("kind") or "reading").lower()
        passage_text = str(section.get("passage_text") or section.get("prompt") or "")
        theory = QuizTheory(
            quiz_id=quiz.id,
            title=title,
            content_type="rich_text",
            language="en",
            display_order=sec_idx,
        )
        db.add(theory)
        await db.flush()
        theory_sec = QuizTheorySection(
            theory_id=theory.id,
            order=1,
            content=passage_text,
            content_format="markdown",
        )
        db.add(theory_sec)
        await db.flush()

        n_questions = 0
        for group in section.get("groups") or []:
            instruction = str(group.get("instruction") or "")
            for q in group.get("questions") or []:
                qtype = str(q.get("question_type") or q.get("type") or "fill_blank")
                if kind == "writing":
                    qtype = "essay"
                quiz_q = QuizQuestion(
                    quiz_id=quiz.id,
                    order=order,
                    type=qtype,
                    question_text=str(q.get("question_text") or q.get("question") or ""),
                    answer=str(q.get("answer") or "") if qtype != "essay" else "",
                    choices=q.get("choices") if isinstance(q.get("choices"), list) else [],
                    items=q.get("items") if isinstance(q.get("items"), list) else [],
                    points=float(q.get("points") or 1.0),
                    has_correct_answer=(qtype != "essay"),
                    required=True,
                    hint_section_id=theory_sec.id,
                    scoring={"mode": "all_or_nothing", "word_limit": q.get("word_limit")},
                    source_type="ai_generated",
                    extra_metadata={
                        "global_number": q.get("global_number") or order + 1,
                        "group_instruction": instruction,
                        "ielts_section": title,
                        "generation_source": "hybrid_rag",
                    },
                )
                db.add(quiz_q)
                order += 1
                n_questions += 1
        summaries.append(IeltsGenerateSection(kind=kind, title=title, n_questions=n_questions))

    if order == 0:
        raise RuntimeError("Gemini không sinh được câu hỏi IELTS nào.")
    quiz.question_count = order
    await db.commit()

    return IeltsGenerateResponse(
        quiz_id=quiz.id,
        quiz_code=quiz.code,
        question_count=order,
        sections=summaries,
        message=f"Đã tạo đề IELTS bằng Hybrid RAG với {order} câu.",
        context_stats={
            "doc_chunks_used": len(rag.get("doc_chunks", [])),
            "questions_used": len(rag.get("similar_questions", [])),
        },
    )


_IELTS_RAG_PROMPT = """You are an expert IELTS test writer. Create a NEW IELTS quiz using the uploaded context and sample questions.

Template: {template}
Required section spec JSON: {spec}
User request: {user_prompt}

Uploaded document context:
{doc_context}

Similar IELTS questions:
{sample_context}

Return JSON object only:
{{
  "sections": [
    {{
      "kind": "reading|listening|writing",
      "title": "Reading Passage 1",
      "passage_text": "passage/transcript/task prompt",
      "groups": [
        {{
          "instruction": "Questions 1-5...",
          "questions": [
            {{
              "global_number": 1,
              "question_type": "fill_blank|multiple_choice|essay",
              "question_text": "...",
              "answer": "...",
              "choices": [],
              "items": [],
              "word_limit": "ONE WORD ONLY"
            }}
          ]
        }}
      ]
    }}
  ]
}}

Rules: generate new content, do not copy long passages verbatim, keep answers deterministic, use essay only for writing sections."""


def _ielts_template_spec(template: str) -> dict:
    if template == "reading_only":
        return {"reading": 10}
    if template == "listening_only":
        return {"listening": 10}
    if template == "writing_only":
        return {"writing": 2}
    if template == "mini_test":
        return {"reading": 5, "listening": 5, "writing": 1}
    return {"reading": 12, "listening": 12, "writing": 2}


def _format_ielts_doc_context(chunks: list[dict], max_chars: int = 6000) -> str:
    parts = []
    total = 0
    for idx, chunk in enumerate(chunks, 1):
        text = str(chunk.get("text") or "")
        remaining = max_chars - total
        if remaining <= 100:
            break
        text = text[:remaining]
        parts.append(f"[Document {idx}] {chunk.get('section_title') or ''}\n{text}")
        total += len(text)
    return "\n\n".join(parts)


def _format_ielts_question_context(questions: list[dict], max_items: int = 8) -> str:
    parts = []
    for idx, q in enumerate(questions[:max_items], 1):
        parts.append(f"[Sample {idx}] {q.get('question_text','')[:700]}\nAnswer: {q.get('answer','')}")
    return "\n\n".join(parts)


def _extract_json_object(content: str) -> dict:
    if not content:
        return {}
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
