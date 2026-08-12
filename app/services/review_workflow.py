from __future__ import annotations

import json
import math
import inspect
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.review import (
    DocumentAsset,
    QuestionAsset,
    QuestionDraft,
    QuestionDraftAsset,
)
from app.services.asset_storage import get_asset_storage


def _load_json(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _bbox_union(boxes: list[list[float]]) -> list[float] | None:
    boxes = [b for b in boxes if b and len(b) == 4]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _bbox_center(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _bbox_intersection_area(a: list[float], b: list[float]) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _bbox_distance(a: list[float], b: list[float]) -> float:
    ax, ay = _bbox_center(a)
    bx, by = _bbox_center(b)
    return math.dist((ax, ay), (bx, by))


async def _db_add(db: AsyncSession, obj: Any) -> None:
    maybe = db.add(obj)
    if inspect.isawaitable(maybe):
        await maybe


def assign_figures_to_drafts(
    draft_payloads: list[dict[str, Any]],
    figures: list[dict[str, Any]],
) -> dict[int, list[int]]:
    """Conservative spatial assignment: only attach clear nearest figures."""
    assigned: dict[int, list[int]] = {
        int(d["draft_id"]): []
        for d in draft_payloads
        if d.get("draft_id") is not None
    }
    for fig in figures:
        fig_box = fig.get("bbox")
        fig_page = fig.get("page_num")
        if not fig_box:
            continue
        candidates: list[tuple[float, dict[str, Any]]] = []
        for draft in draft_payloads:
            box = draft.get("bbox")
            if not box or draft.get("page_num") != fig_page:
                continue
            overlap = _bbox_intersection_area(box, fig_box)
            distance = _bbox_distance(box, fig_box)
            # overlap wins; otherwise keep only close-enough figures
            score = -overlap * 10 + distance
            if overlap > 0 or distance <= 0.34:
                candidates.append((score, draft))
        candidates.sort(key=lambda item: item[0])
        if not candidates:
            continue
        best = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None
        # Ambiguous if nearest two are too close in score.
        if second and abs(second[0] - best[0]) < 0.035:
            continue
        assigned[int(best[1]["draft_id"])].append(int(fig["asset_id"]))
    return assigned


async def create_review_drafts(
    db: AsyncSession,
    *,
    exam_id: int,
    user_id: int,
    questions: list[dict[str, Any]],
    layout_snapshot: dict[str, Any],
) -> list[QuestionDraft]:
    await db.execute(delete(QuestionDraft).where(QuestionDraft.exam_id == exam_id))
    await db.flush()

    blocks_by_id = {
        str(block["block_id"]): block
        for block in layout_snapshot.get("blocks", [])
    }
    draft_payloads: list[dict[str, Any]] = []
    drafts: list[QuestionDraft] = []
    for index, q in enumerate(questions):
        source_ids = list(q.get("source_block_ids") or [])
        source_blocks = [blocks_by_id[bid] for bid in source_ids if bid in blocks_by_id]
        source_boxes = [block.get("bbox") for block in source_blocks if block.get("bbox")]
        bbox = q.get("bbox") or _bbox_union(source_boxes)
        page_num = source_blocks[0]["page_num"] if source_blocks else q.get("page_num")
        draft = QuestionDraft(
            exam_id=exam_id,
            user_id=user_id,
            cau_num=q.get("cau_num"),
            question_order=index + 1,
            page_num=page_num,
            question_text=q.get("question", ""),
            subject_code=q.get("subject", "toan"),
            question_type=q.get("type"),
            topic=q.get("topic"),
            difficulty=q.get("difficulty"),
            grade=q.get("grade"),
            chapter=q.get("chapter"),
            lesson_title=q.get("lesson_title"),
            answer=q.get("answer"),
            answer_source=q.get("answer_source"),
            solution_steps=json.dumps(q.get("solution_steps", []), ensure_ascii=False),
            bbox_json=json.dumps(bbox) if bbox else None,
            source_block_ids_json=json.dumps(source_ids),
            confidence=float(q.get("segmentation_confidence") or 0.0),
            status="pending",
        )
        await _db_add(db, draft)
        await db.flush()
        drafts.append(draft)
        draft_payloads.append(
            {
                "draft_id": draft.id,
                "bbox": bbox,
                "page_num": page_num,
            }
        )

    assigned = assign_figures_to_drafts(draft_payloads, layout_snapshot.get("figures", []))
    placeholder_to_asset = {
        fig.get("placeholder"): fig.get("asset_id")
        for fig in layout_snapshot.get("figures", [])
        if fig.get("placeholder") and fig.get("asset_id")
    }
    for draft in drafts:
        q = questions[draft.question_order - 1]
        placeholder_asset_ids = {
            placeholder_to_asset[placeholder]
            for placeholder in (q.get("images") or {}).keys()
            if placeholder in placeholder_to_asset
        }
        for asset_id in sorted(set(assigned.get(draft.id, [])) | placeholder_asset_ids):
            await _db_add(db, QuestionDraftAsset(draft_id=draft.id, asset_id=asset_id, relation_type="figure"))
    await db.flush()
    return drafts


async def recompute_draft_assignments(
    db: AsyncSession,
    *,
    exam_id: int,
    draft_id: int,
    bbox: list[float] | None,
) -> QuestionDraft:
    draft = await db.scalar(
        select(QuestionDraft)
        .where(QuestionDraft.id == draft_id, QuestionDraft.exam_id == exam_id)
        .options(selectinload(QuestionDraft.asset_links))
    )
    if not draft:
        raise ValueError("draft_not_found")
    draft.bbox_json = json.dumps(bbox) if bbox else None
    draft.updated_at = datetime.now(timezone.utc)
    await db.execute(delete(QuestionDraftAsset).where(QuestionDraftAsset.draft_id == draft.id))
    await db.flush()

    if bbox:
        assets = (
            await db.execute(
                select(DocumentAsset).where(
                    DocumentAsset.exam_id == exam_id,
                    DocumentAsset.kind == "figure_crop",
                )
            )
        ).scalars().all()
        figures = [
            {
                "asset_id": asset.id,
                "page_num": asset.page_num,
                "bbox": _load_json(asset.bbox_json, None),
            }
            for asset in assets
        ]
        assigned = assign_figures_to_drafts(
            [{"draft_id": draft.id, "bbox": bbox, "page_num": draft.page_num}],
            figures,
        )
        for asset_id in assigned.get(draft.id, []):
            await _db_add(db, QuestionDraftAsset(draft_id=draft.id, asset_id=asset_id, relation_type="figure"))
    await db.flush()
    return draft


async def build_review_payload(db: AsyncSession, *, exam_id: int) -> dict[str, Any]:
    storage = get_asset_storage()
    assets = (
        await db.execute(select(DocumentAsset).where(DocumentAsset.exam_id == exam_id))
    ).scalars().all()
    drafts = (
        await db.execute(
            select(QuestionDraft)
            .where(QuestionDraft.exam_id == exam_id)
            .options(selectinload(QuestionDraft.asset_links).selectinload(QuestionDraftAsset.asset))
            .order_by(QuestionDraft.question_order.asc(), QuestionDraft.id.asc())
        )
    ).scalars().all()

    pages = []
    figures = []
    for asset in assets:
        item = {
            "id": asset.id,
            "kind": asset.kind,
            "page_num": asset.page_num,
            "bbox": _load_json(asset.bbox_json, None),
            "url": storage.public_url(asset.storage_key),
            "width": asset.width,
            "height": asset.height,
            "provenance": _load_json(asset.provenance_json, {}),
        }
        if asset.kind == "page_render":
            pages.append(item)
        elif asset.kind == "figure_crop":
            figures.append(item)

    draft_payloads = []
    for draft in drafts:
        draft_payloads.append(
            {
                "id": draft.id,
                "cau_num": draft.cau_num,
                "question_order": draft.question_order,
                "page_num": draft.page_num,
                "question_text": draft.question_text,
                "subject_code": draft.subject_code,
                "question_type": draft.question_type,
                "topic": draft.topic,
                "difficulty": draft.difficulty,
                "grade": draft.grade,
                "chapter": draft.chapter,
                "lesson_title": draft.lesson_title,
                "answer": draft.answer,
                "answer_source": draft.answer_source,
                "solution_steps": _load_json(draft.solution_steps, []),
                "bbox": _load_json(draft.bbox_json, None),
                "source_block_ids": _load_json(draft.source_block_ids_json, []),
                "confidence": draft.confidence,
                "status": draft.status,
                "asset_ids": [link.asset_id for link in draft.asset_links],
            }
        )
    from app.db.models.exam import Exam
    exam = await db.scalar(select(Exam).where(Exam.id == exam_id))
    layout_json = _load_json(exam.layout_json if exam else None, {})
    return {
        "pages": sorted(pages, key=lambda item: item["page_num"] or 0),
        "figures": figures,
        "blocks": layout_json.get("blocks", []),
        "drafts": draft_payloads,
        "warnings": layout_json.get("warnings", []),
    }


async def link_assets_to_saved_questions(
    db: AsyncSession,
    *,
    exam_id: int,
) -> None:
    from app.db.models.question import Question

    drafts = (
        await db.execute(
            select(QuestionDraft)
            .where(QuestionDraft.exam_id == exam_id, QuestionDraft.status == "accepted")
            .options(selectinload(QuestionDraft.asset_links))
        )
    ).scalars().all()
    saved_questions = (
        await db.execute(select(Question).where(Question.exam_id == exam_id))
    ).scalars().all()
    by_order = {q.question_order: q for q in saved_questions}
    for draft in drafts:
        question = by_order.get(draft.question_order)
        if not question:
            continue
        for link in draft.asset_links:
            await _db_add(
                db,
                QuestionAsset(
                    question_id=question.id,
                    asset_id=link.asset_id,
                    bbox_json=link.asset.bbox_json if link.asset else None,
                ),
            )
    await db.flush()
