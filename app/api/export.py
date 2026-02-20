"""
Export API — Xuất đề thi dạng DOCX, LaTeX, PDF.

Endpoints:
    POST /export/docx         — Tải DOCX (đề + đáp án)
    POST /export/docx-split   — Tải DOCX tách (đề riêng, đáp án riêng) → ZIP
    POST /export/latex        — Tải file .tex
    POST /export/pdf          — Tải HTML chuẩn in PDF
    POST /export/bank/docx    — Xuất câu hỏi từ ngân hàng → DOCX
    POST /export/bank/latex   — Xuất câu hỏi từ ngân hàng → LaTeX
"""

import io
import json
import zipfile
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.question import Question
from app.db.models.user import User
from app.services.exporter import (
    export_docx, export_docx_split, export_latex, export_pdf_html,
)

router = APIRouter()


# ─── Request schemas ──────────────────────────────────────────

class ExportQuestionItem(BaseModel):
    """A single question for export (matches generator output)."""
    question: str
    type: str = "TN"
    topic: str = ""
    difficulty: str = "TH"
    answer: str = ""
    solution_steps: List[str] = []


class ExportRequest(BaseModel):
    """Request body for exporting generated questions."""
    questions: List[ExportQuestionItem]
    title: str = Field(default="ĐỀ THI TOÁN HỌC")
    subtitle: str = Field(default="")
    include_answers: bool = True
    include_solutions: bool = True
    group_by_diff: bool = True
    exam_info: Optional[Dict[str, Any]] = None


class BankExportRequest(BaseModel):
    """Request body for exporting questions from the bank."""
    question_ids: List[int] = Field(default=[], description="Specific question IDs to export")
    # Filters (used if question_ids is empty)
    topic: str = ""
    difficulty: str = ""
    question_type: str = ""
    keyword: str = ""
    limit: int = Field(default=50, le=200)
    # Options
    title: str = "NGÂN HÀNG CÂU HỎI"
    subtitle: str = ""
    include_answers: bool = True
    include_solutions: bool = True
    group_by_diff: bool = True
    exam_info: Optional[Dict[str, Any]] = None


# ─── Helper ───────────────────────────────────────────────────

async def _get_bank_questions(
    db: AsyncSession, user_id: int, req: BankExportRequest
) -> List[Dict]:
    """Fetch questions from bank by IDs or filters."""
    conditions = [Question.user_id == user_id]

    if req.question_ids:
        conditions.append(Question.id.in_(req.question_ids))
    else:
        if req.topic:
            conditions.append(Question.topic == req.topic)
        if req.difficulty:
            conditions.append(Question.difficulty == req.difficulty)
        if req.question_type:
            conditions.append(Question.question_type == req.question_type)
        if req.keyword:
            conditions.append(Question.question_text.ilike(f"%{req.keyword}%"))

    result = await db.execute(
        select(Question)
        .where(*conditions)
        .order_by(Question.question_order, Question.id)
        .limit(req.limit)
    )
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi nào")
    return rows


def _safe_filename(text: str) -> str:
    """Create safe filename from text."""
    import re
    clean = re.sub(r'[^\w\s\-]', '', text)
    clean = re.sub(r'\s+', '_', clean.strip())
    return clean[:60] or "export"


# ═══════════════════════════════════════════════════════════════
#  GENERATED QUESTIONS EXPORT
# ═══════════════════════════════════════════════════════════════

@router.post("/docx")
async def export_generated_docx(
    req: ExportRequest,
    current_user: User = Depends(deps.get_current_user),
):
    """Export generated questions to DOCX."""
    if not req.questions:
        raise HTTPException(400, "Danh sách câu hỏi trống")

    buf = export_docx(
        [q.model_dump() for q in req.questions],
        title=req.title,
        subtitle=req.subtitle,
        include_answers=req.include_answers,
        include_solutions=req.include_solutions,
        group_by_diff=req.group_by_diff,
        exam_info=req.exam_info,
    )
    filename = _safe_filename(req.subtitle or req.title) + ".docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/docx-split")
async def export_generated_docx_split(
    req: ExportRequest,
    current_user: User = Depends(deps.get_current_user),
):
    """Export generated questions to ZIP (đề riêng + đáp án riêng)."""
    if not req.questions:
        raise HTTPException(400, "Danh sách câu hỏi trống")

    result = export_docx_split(
        [q.model_dump() for q in req.questions],
        title=req.title,
        subtitle=req.subtitle,
        exam_info=req.exam_info,
    )

    # Package into ZIP
    zip_buf = io.BytesIO()
    base = _safe_filename(req.subtitle or req.title)
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base}_De.docx", result["exam"].read())
        zf.writestr(f"{base}_DapAn.docx", result["answers"].read())
    zip_buf.seek(0)

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}.zip"'},
    )


@router.post("/latex")
async def export_generated_latex(
    req: ExportRequest,
    current_user: User = Depends(deps.get_current_user),
):
    """Export generated questions to LaTeX .tex file."""
    if not req.questions:
        raise HTTPException(400, "Danh sách câu hỏi trống")

    buf = export_latex(
        [q.model_dump() for q in req.questions],
        title=req.title,
        subtitle=req.subtitle,
        include_answers=req.include_answers,
        include_solutions=req.include_solutions,
        group_by_diff=req.group_by_diff,
        exam_info=req.exam_info,
    )
    filename = _safe_filename(req.subtitle or req.title) + ".tex"
    return StreamingResponse(
        buf,
        media_type="application/x-tex",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/pdf")
async def export_generated_pdf(
    req: ExportRequest,
    current_user: User = Depends(deps.get_current_user),
):
    """Export generated questions to print-ready HTML (for Save as PDF)."""
    if not req.questions:
        raise HTTPException(400, "Danh sách câu hỏi trống")

    html = export_pdf_html(
        [q.model_dump() for q in req.questions],
        title=req.title,
        subtitle=req.subtitle,
        include_answers=req.include_answers,
        include_solutions=req.include_solutions,
        group_by_diff=req.group_by_diff,
        exam_info=req.exam_info,
    )
    return HTMLResponse(content=html)


# ═══════════════════════════════════════════════════════════════
#  BANK QUESTIONS EXPORT
# ═══════════════════════════════════════════════════════════════

@router.post("/bank/docx")
async def export_bank_docx(
    req: BankExportRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export questions from bank to DOCX."""
    rows = await _get_bank_questions(db, current_user.id, req)
    buf = export_docx(
        rows,
        title=req.title,
        subtitle=req.subtitle,
        include_answers=req.include_answers,
        include_solutions=req.include_solutions,
        group_by_diff=req.group_by_diff,
        exam_info=req.exam_info,
    )
    filename = _safe_filename(req.subtitle or req.title) + ".docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/bank/latex")
async def export_bank_latex(
    req: BankExportRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export questions from bank to LaTeX."""
    rows = await _get_bank_questions(db, current_user.id, req)
    buf = export_latex(
        rows,
        title=req.title,
        subtitle=req.subtitle,
        include_answers=req.include_answers,
        include_solutions=req.include_solutions,
        group_by_diff=req.group_by_diff,
        exam_info=req.exam_info,
    )
    filename = _safe_filename(req.subtitle or req.title) + ".tex"
    return StreamingResponse(
        buf,
        media_type="application/x-tex",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/bank/pdf")
async def export_bank_pdf(
    req: BankExportRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export questions from bank to print-ready HTML."""
    rows = await _get_bank_questions(db, current_user.id, req)
    html = export_pdf_html(
        rows,
        title=req.title,
        subtitle=req.subtitle,
        include_answers=req.include_answers,
        include_solutions=req.include_solutions,
        group_by_diff=req.group_by_diff,
        exam_info=req.exam_info,
    )
    return HTMLResponse(content=html)