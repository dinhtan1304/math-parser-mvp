"""Remote PaddleOCR-VL OCR via the AIStudio hosted job API (thin client).

Offloads the heavy 0.9B VLM to a hosted server so the local box never runs
paddle/torch/GPU. Same output shape as the local
``local_ocr_service._run_paddle_vl_pipeline`` so the cascade is engine-agnostic:
``{text, page_count, image_map, blocks, figures, warnings}``.

API contract (verified live, job-based async):
  POST {url}                    multipart file + model + optionalPayload → data.jobId
  GET  {url}/{jobId}            → data.state pending|running|done|failed
                                  running: data.extractProgress.{totalPages,extractedPages}
                                  done:    data.resultUrl.jsonUrl  (a JSONL)
  GET  jsonUrl                  JSONL; each line result.layoutParsingResults[i] =
                                  {prunedResult{parsing_res_list,width,height},
                                   markdown{text,images:{path:url}}, outputImages}

Uses ``requests`` (the server resets httpx connections) wrapped in
``asyncio.to_thread`` so the async OCR path is never blocked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.services.paddle_vl_common import blocks_to_review_shape, layout_blocks_from_res_data

logger = logging.getLogger(__name__)


def _envf(name: str, default: str) -> float:
    raw = os.getenv(name, default).split("#", 1)[0].strip()
    try:
        return float(raw or default)
    except ValueError:
        return float(default)


_API_URL = os.getenv("PADDLE_VL_API_URL", "").strip().rstrip("/")
_API_KEY = os.getenv("PADDLE_VL_API_KEY", "").strip()
_API_MODEL = os.getenv("PADDLE_VL_API_MODEL", "PaddleOCR-VL-1.6").strip()
_POLL_TIMEOUT = _envf("PADDLE_VL_API_TIMEOUT", "600")        # total wait for the job
_POLL_INTERVAL = _envf("PADDLE_VL_API_POLL_INTERVAL", "5")
_HTTP_TIMEOUT = _envf("PADDLE_VL_API_HTTP_TIMEOUT", "120")   # per-request

_EMPTY: dict[str, Any] = {
    "text": "", "page_count": 0, "image_map": {}, "blocks": [], "figures": [], "warnings": [],
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def is_api_configured() -> bool:
    """True when the remote OCR API has both a URL and a token."""
    return bool(_API_URL and _API_KEY)


# ── sync HTTP steps (run via asyncio.to_thread) ──────────────────────────────
def _submit_job(file_path: str) -> str:
    import requests

    headers = {"Authorization": f"bearer {_API_KEY}"}
    optional = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    with open(file_path, "rb") as f:
        resp = requests.post(
            _API_URL,
            headers=headers,
            data={"model": _API_MODEL, "optionalPayload": json.dumps(optional)},
            files={"file": (Path(file_path).name, f)},
            timeout=_HTTP_TIMEOUT,
        )
    resp.raise_for_status()
    return str(resp.json()["data"]["jobId"])


def _poll_job(job_id: str) -> dict[str, Any]:
    import requests

    headers = {"Authorization": f"bearer {_API_KEY}"}
    resp = requests.get(f"{_API_URL}/{job_id}", headers=headers, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["data"]


def _fetch_text(url: str) -> str:
    import requests

    resp = requests.get(url, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _download_bytes(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


# ── result parsing ───────────────────────────────────────────────────────────
def _iter_page_results(jsonl_text: str):
    """Yield each per-page layoutParsingResult across all JSONL lines (the API
    batches multiple pages per line)."""
    for line in jsonl_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            result = json.loads(line)["result"]
        except Exception:
            continue
        for entry in result.get("layoutParsingResults") or []:
            if isinstance(entry, dict):
                yield entry


async def _relocate_image(url: str, dest: Path) -> bool:
    try:
        data = await asyncio.to_thread(_download_bytes, url)
        dest.write_bytes(data)
        return True
    except Exception as exc:  # best-effort — figures are non-critical
        logger.warning("paddle_vl_api: image download failed %s: %s", url, exc)
        return False


async def run_paddle_vl_api(
    file_path: str, file_hash: str, budget_seconds: float | None = None
) -> dict[str, Any]:
    """Run OCR on ``file_path`` via the remote PaddleOCR-VL job API.

    Never raises — on any failure returns an empty artifact with warnings so the
    upstream cascade falls back to MinerU. ``budget_seconds`` (caller's remaining
    OCR budget) caps the poll deadline so we stop waiting when the parser would
    give up anyway.
    """
    if not is_api_configured():
        return {**_EMPTY, "warnings": ["paddle_vl_api_unconfigured: thiếu PADDLE_VL_API_URL/KEY"]}

    warnings: list[str] = []
    try:
        job_id = await asyncio.to_thread(_submit_job, file_path)
    except Exception as exc:
        logger.warning("paddle_vl_api: submit failed for %s: %s", file_path, exc)
        return {**_EMPTY, "warnings": [f"paddle_vl_api_submit_failed: {exc}"]}

    logger.info("paddle_vl_api: job %s submitted for %s", job_id, file_path)
    loop = asyncio.get_event_loop()
    poll_timeout = _POLL_TIMEOUT
    if budget_seconds is not None:
        poll_timeout = max(30.0, min(poll_timeout, budget_seconds))
    deadline = loop.time() + poll_timeout
    json_url = ""
    while True:
        try:
            data = await asyncio.to_thread(_poll_job, job_id)
        except Exception as exc:
            logger.warning("paddle_vl_api: poll failed job=%s: %s", job_id, exc)
            return {**_EMPTY, "warnings": [f"paddle_vl_api_poll_failed: {exc}"]}
        state = data.get("state")
        if state == "done":
            json_url = (data.get("resultUrl") or {}).get("jsonUrl", "")
            break
        if state == "failed":
            return {**_EMPTY, "warnings": [f"paddle_vl_api_failed: {data.get('errorMsg')}"]}
        if loop.time() >= deadline:
            return {**_EMPTY, "warnings": [f"paddle_vl_api_timeout: job {job_id} chưa xong sau {poll_timeout:.0f}s"]}
        await asyncio.sleep(_POLL_INTERVAL)

    if not json_url:
        return {**_EMPTY, "warnings": ["paddle_vl_api_no_result_url"]}
    try:
        jsonl_text = await asyncio.to_thread(_fetch_text, json_url)
    except Exception as exc:
        return {**_EMPTY, "warnings": [f"paddle_vl_api_fetch_failed: {exc}"]}

    # ── assemble: per-page markdown + layout blocks (normalized bbox) ──
    media_root = Path("media_uploads") / "ocr_images" / file_hash
    md_parts: list[str] = []
    layout_blocks: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    image_map: dict[str, str] = {}
    page_idx = 0
    for entry in _iter_page_results(jsonl_text):
        md = entry.get("markdown") or {}
        page_md = str(md.get("text") or "")
        pruned = entry.get("prunedResult")
        if isinstance(pruned, dict):
            layout_blocks.extend(layout_blocks_from_res_data(pruned, page_idx))
        # Relocate embedded figure images (URL → /media). Best-effort.
        for ref, url in (md.get("images") or {}).items():
            if not url:
                continue
            fname = f"p{page_idx + 1}_{_SAFE_NAME.sub('_', Path(str(ref)).name) or 'img'}"
            media_root.mkdir(parents=True, exist_ok=True)
            if await _relocate_image(str(url), media_root / fname):
                public = f"/media/ocr_images/{file_hash}/{fname}"
                page_md = page_md.replace(str(ref), public)
                image_map[public] = public
                figures.append({
                    "figure_id": f"p{page_idx + 1}_paddlevl_fig_{len(figures) + 1}",
                    "page_num": page_idx + 1,
                    "bbox": None,
                    "placeholder": fname,
                    "url": public,
                    "source": "paddle-vl",
                    "caption": None,
                })
        md_parts.append(page_md)
        page_idx += 1

    text = "\n\n".join(p for p in md_parts if p).strip()
    if not layout_blocks:
        warnings.append("paddle_vl_api_no_layout_blocks: overlay dùng native PDF geometry")
    return {
        "text": text,
        "page_count": page_idx or 1,
        "image_map": image_map,
        "blocks": blocks_to_review_shape(layout_blocks),
        "figures": figures,
        "warnings": warnings,
    }
