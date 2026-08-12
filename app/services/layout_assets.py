from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.review import DocumentAsset
from app.services.asset_storage import get_asset_storage

logger = logging.getLogger(__name__)


@dataclass
class LayoutSnapshot:
    pages: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    figures: list[dict[str, Any]]
    warnings: list[str]


def _norm_bbox(rect, width: float, height: float) -> list[float]:
    if not width or not height:
        return [0.0, 0.0, 1.0, 1.0]
    return [
        round(max(0.0, min(1.0, rect.x0 / width)), 6),
        round(max(0.0, min(1.0, rect.y0 / height)), 6),
        round(max(0.0, min(1.0, rect.x1 / width)), 6),
        round(max(0.0, min(1.0, rect.y1 / height)), 6),
    ]


async def capture_pdf_layout(
    db: AsyncSession,
    *,
    exam_id: int,
    user_id: int,
    file_path: str,
    render_dpi: int = 144,
) -> LayoutSnapshot:
    """Capture page renders, native text blocks and native image rectangles for review."""
    suffix = Path(file_path).suffix.lower()
    if suffix != ".pdf":
        return LayoutSnapshot(pages=[], blocks=[], figures=[], warnings=["layout_capture_non_pdf"])

    import fitz  # type: ignore

    storage = get_asset_storage()
    pages: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    warnings: list[str] = []
    doc = fitz.open(file_path)
    try:
        for page_idx, page in enumerate(doc, start=1):
            width = float(page.rect.width or 1)
            height = float(page.rect.height or 1)
            matrix = fitz.Matrix(render_dpi / 72.0, render_dpi / 72.0)
            page_pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_bytes = page_pix.tobytes("png")
            storage_key, digest = storage.put_bytes(
                page_bytes,
                suffix=".png",
                namespace=f"ocr-pages/exam-{exam_id}",
            )
            page_asset = DocumentAsset(
                exam_id=exam_id,
                user_id=user_id,
                kind="page_render",
                storage_key=storage_key,
                content_hash=digest,
                mime_type="image/png",
                width=page_pix.width,
                height=page_pix.height,
                page_num=page_idx,
                bbox_json=json.dumps([0.0, 0.0, 1.0, 1.0]),
                provenance_json=json.dumps({"source": "pymupdf_page_render", "dpi": render_dpi}),
            )
            db.add(page_asset)
            await db.flush()
            pages.append(
                {
                    "page_num": page_idx,
                    "width": page_pix.width,
                    "height": page_pix.height,
                    "asset_id": page_asset.id,
                    "url": storage.public_url(storage_key),
                }
            )

            for order, raw in enumerate(page.get_text("blocks")):
                x0, y0, x1, y1, text, *_rest = raw
                cleaned = (text or "").strip()
                if not cleaned:
                    continue
                rect = fitz.Rect(x0, y0, x1, y1)
                blocks.append(
                    {
                        "block_id": f"p{page_idx}_native_{order + 1}",
                        "page_num": page_idx,
                        "order": order,
                        "text": cleaned,
                        "source": "pymupdf",
                        "bbox": _norm_bbox(rect, width, height),
                        "kind": "paragraph",
                    }
                )

            seen_rects: set[tuple[float, float, float, float]] = set()
            for image_idx, info in enumerate(page.get_images(full=True), start=1):
                xref = info[0]
                for rect_idx, rect in enumerate(page.get_image_rects(xref), start=1):
                    bbox_norm = _norm_bbox(rect, width, height)
                    rect_key = tuple(bbox_norm)
                    if rect_key in seen_rects:
                        continue
                    seen_rects.add(rect_key)
                    clip_pix = page.get_pixmap(matrix=matrix, clip=rect, alpha=False)
                    crop_bytes = clip_pix.tobytes("png")
                    crop_key, crop_digest = storage.put_bytes(
                        crop_bytes,
                        suffix=".png",
                        namespace=f"ocr-figures/exam-{exam_id}",
                    )
                    asset = DocumentAsset(
                        exam_id=exam_id,
                        user_id=user_id,
                        kind="figure_crop",
                        storage_key=crop_key,
                        content_hash=crop_digest,
                        mime_type="image/png",
                        width=clip_pix.width,
                        height=clip_pix.height,
                        page_num=page_idx,
                        bbox_json=json.dumps(bbox_norm),
                        provenance_json=json.dumps(
                            {
                                "source": "pymupdf_image_rect",
                                "xref": xref,
                                "image_index": image_idx,
                                "rect_index": rect_idx,
                            }
                        ),
                    )
                    db.add(asset)
                    await db.flush()
                    figures.append(
                        {
                            "figure_id": f"p{page_idx}_fig_{len(figures) + 1}",
                            "page_num": page_idx,
                            "asset_id": asset.id,
                            "bbox": bbox_norm,
                            "url": storage.public_url(crop_key),
                            "caption": None,
                            "assignment_status": "unassigned_candidate",
                        }
                    )
    except Exception as exc:
        logger.warning("Layout capture failed for exam %s: %s", exam_id, exc)
        warnings.append(f"layout_capture_failed:{exc}")
    finally:
        doc.close()
    await db.flush()
    return LayoutSnapshot(pages=pages, blocks=blocks, figures=figures, warnings=warnings)


async def persist_ocr_image_map(
    db: AsyncSession,
    *,
    exam_id: int,
    user_id: int,
    image_map: dict[str, Any],
    figure_hints: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Persist OCR-extracted image placeholders, using OCR layout hints when present."""
    storage = get_asset_storage()
    figures: list[dict[str, Any]] = []
    hint_by_placeholder = {
        str(hint.get("placeholder")): hint
        for hint in (figure_hints or [])
        if hint.get("placeholder")
    }
    for placeholder, value in (image_map or {}).items():
        content: bytes | None = None
        try:
            if isinstance(value, (str, Path)) and Path(value).exists():
                content = Path(value).read_bytes()
            elif isinstance(value, bytes):
                content = value
            elif hasattr(value, "save"):
                buf = io.BytesIO()
                value.save(buf, format="PNG")
                content = buf.getvalue()
        except Exception:
            content = None
        if not content:
            continue
        key, digest = storage.put_bytes(
            content,
            suffix=".png",
            namespace=f"ocr-figures/exam-{exam_id}",
        )
        hint = hint_by_placeholder.get(str(placeholder), {})
        bbox = hint.get("bbox")
        page_num = hint.get("page_num")
        asset = DocumentAsset(
            exam_id=exam_id,
            user_id=user_id,
            kind="figure_crop",
            storage_key=key,
            content_hash=digest,
            mime_type="image/png",
            page_num=page_num,
            bbox_json=json.dumps(bbox) if bbox else None,
            provenance_json=json.dumps(
                {
                    "source": hint.get("source") or "ocr_image_map",
                    "placeholder": placeholder,
                    "marker_block_id": hint.get("marker_block_id"),
                }
            ),
        )
        db.add(asset)
        await db.flush()
        figures.append(
            {
                "figure_id": f"ocr_placeholder_{asset.id}",
                "page_num": page_num,
                "asset_id": asset.id,
                "bbox": bbox,
                "url": storage.public_url(key),
                "caption": None,
                "placeholder": placeholder,
                "assignment_status": "unassigned_candidate",
            }
        )
    return figures
