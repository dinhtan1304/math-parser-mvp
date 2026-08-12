"""Pure helpers shared by the local PaddleOCR-VL engine and the remote API client.

No torch/paddle imports — safe to import from anywhere. These convert PaddleOCR-VL
layout output (``parsing_res_list`` with pixel ``block_bbox``) into the review
block shape the segmentation + bank overlay consume (normalized [0,1] bbox).
"""
from __future__ import annotations

from typing import Any

# PaddleOCR-VL layout label → block ``kind`` (mirror MinerU's content_list kinds).
# Unmapped labels (title/header/footer/paragraph_title/abstract/…) fall to "text".
PADDLE_LABEL_KIND = {
    "formula": "equation",
    "formula_number": "equation",
    "equation": "equation",
    "table": "table",
    "image": "figure",
    "figure": "figure",
    "chart": "figure",
    "seal": "figure",
}


def normalize_bbox_to_unit(bbox: Any, width: float, height: float) -> list[float] | None:
    """Normalize a pixel bbox ``[x0,y0,x1,y1]`` to [0,1] fractions of the page.

    The review overlay (FE) renders bbox as ``left: x*100%`` so coordinates must
    be unit-normalized. PaddleOCR-VL reports per-page ``width``/``height``; we
    divide by those. Already-normalized values (max <= 1.5) pass through clamped.
    Returns ``None`` when bbox/dims are unusable or degenerate.
    """
    if not bbox or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return None
    mx = max(abs(x0), abs(y0), abs(x1), abs(y1))
    if mx <= 1.5:  # already normalized
        coords = [x0, y0, x1, y1]
    elif width > 0 and height > 0:
        coords = [x0 / width, y0 / height, x1 / width, y1 / height]
    else:
        return None
    clamped = [max(0.0, min(1.0, c)) for c in coords]
    if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
        return None  # degenerate / inverted box
    return [round(c, 6) for c in clamped]


def layout_blocks_from_res_data(data: Any, page_idx: int) -> list[dict[str, Any]]:
    """Parse one page's result dict (``parsing_res_list``/``width``/``height``)
    into ``[{page_index, label, content, bbox}]`` with normalized bbox.

    ``data`` is either a local ``res.json["res"]`` dict or the remote API's
    ``prunedResult``. ``page_idx`` is the fallback page index (the remote API's
    ``prunedResult`` has no ``page_index``). Best-effort — [] on bad shape.
    """
    if not isinstance(data, dict):
        return []
    width = float(data.get("width") or 0)
    height = float(data.get("height") or 0)
    page_index = int(data.get("page_index") if data.get("page_index") is not None else page_idx)
    blocks: list[dict[str, Any]] = []
    for item in data.get("parsing_res_list") or []:
        if not isinstance(item, dict):
            continue
        blocks.append({
            "page_index": page_index,
            "label": str(item.get("block_label") or "text"),
            "content": str(item.get("block_content") or ""),
            "bbox": normalize_bbox_to_unit(item.get("block_bbox"), width, height),
            "order": item.get("block_order"),
        })
    return blocks


def blocks_to_review_shape(layout_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ``layout_blocks`` (page_index/label/content/bbox) into the
    MinerU-style review block shape consumed by segmentation + overlay:
    ``{block_id, page_num (1-indexed), order, text, source, bbox, kind}``.
    """
    blocks: list[dict[str, Any]] = []
    for item in layout_blocks or []:
        if not isinstance(item, dict):
            continue
        page_num = int(item.get("page_index") or 0) + 1  # paddle page_index is 0-based
        kind = PADDLE_LABEL_KIND.get(str(item.get("label") or "").lower(), "text")
        blocks.append({
            "block_id": f"p{page_num}_paddlevl_{len(blocks)}",
            "page_num": page_num,
            "order": len(blocks),
            "text": str(item.get("content") or ""),
            "source": "paddle-vl",
            "bbox": item.get("bbox"),
            "kind": kind,
        })
    return blocks
