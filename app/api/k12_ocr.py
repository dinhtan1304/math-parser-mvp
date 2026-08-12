"""K12 OCR — admin-facing upload endpoint that runs MinerU + formula validator.

This is the FE-facing wrapper around stages 1-2 of the K12 batch pipeline:
the user uploads a PDF, we run MinerU OCR, validate formulas, and return
the raw markdown (with LaTeX) plus extracted images. We deliberately skip
the regex segmenter, Gemini finalize, and answer extractor — the goal is
to expose the OCR layer for inspection, not to materialize questions.

Output is written under ``media_uploads/k12_ocr_jobs/{job_id}/`` so the
existing ``/media`` static mount serves images directly.

Endpoints
---------
* ``POST /admin/k12-ocr/process`` — sync upload + OCR; returns markdown,
  asset URLs and formula stats. Sync because admin runs are interactive
  and a typical 5-10 page PDF finishes in 30-90s on GPU.
* ``GET  /admin/k12-ocr/job/{job_id}``  — re-fetch a previously produced
  result (page reload safety).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.api.deps import get_current_active_superuser
from app.benchmark.engines.base import OcrEngine
from app.benchmark.engines.marker_engine import MarkerEngine
from app.benchmark.engines.mineru_engine import MinerUEngine
from app.benchmark.engines.paddle_engine import PaddleEngine
from app.db.models.user import User
from app.services.k12_batch.config import load_config
from app.services.k12_batch.formula_validator import revalidate_markdown
from app.services.k12_batch.pipeline import _read_content_list


logger = logging.getLogger(__name__)
router = APIRouter()


# Output root — relative to repo root, lives under media_uploads so the
# existing FastAPI static mount serves the assets via /media/...
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOBS_DIR = _REPO_ROOT / "media_uploads" / "k12_ocr_jobs"
_JOBS_DIR.mkdir(parents=True, exist_ok=True)


SUBJECT_CHOICES = ("toan", "ly", "hoa", "sinh", "van", "su", "dia", "anh")
ENGINE_CHOICES = ("mineru", "marker", "paddle")
# MinerU 3.x has no Vietnamese tokenizer (`--lang latin` strips diacritics);
# Marker delegates to Surya OCR which is multilingual and keeps Vietnamese
# accents intact, at the cost of ~50% extra latency. PaddleOCR-VL would also
# work but is currently disabled on Windows due to a cuDNN DLL conflict.


class AssetOut(BaseModel):
    """One extracted image, as referenced from the markdown."""

    filename: str
    url: str  # /media/k12_ocr_jobs/<job>/<filename>
    page: int | None = None
    bbox: list[float] | None = None
    caption: str | None = None


class FormulaStatsOut(BaseModel):
    scanned: int
    invalid: int
    retried: int
    recovered: int
    no_bbox: int = 0
    crop_failed: int = 0
    paddle_empty: int = 0


class K12OcrResult(BaseModel):
    job_id: str
    filename: str
    subject: str
    grade: int
    markdown: str
    assets: list[AssetOut]
    formula_stats: FormulaStatsOut
    ocr_engine: str
    ocr_latency_ms: int
    content_block_count: int
    total_elapsed_seconds: float


def _job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9-]{8,}", job_id):
        raise HTTPException(status_code=400, detail="invalid job_id")
    return _JOBS_DIR / job_id


def _result_path(job_id: str) -> Path:
    return _job_dir(job_id) / "result.json"


@router.post("/k12-ocr/process", response_model=K12OcrResult)
async def k12_ocr_process(
    file: UploadFile = File(...),
    subject: str = Form(...),
    grade: int = Form(...),
    engine: str = Form("mineru"),
    enable_formula_retry: bool = Form(True),
    _user: User = Depends(get_current_active_superuser),
) -> K12OcrResult:
    """Upload a PDF, run the chosen OCR engine + formula validation, return markdown.

    Admin-only. Runs synchronously; a 5-10 page PDF on GPU takes 30-90s.
    Larger documents will time out at the reverse-proxy layer — for those,
    use the CLI (``scripts/ocr_k12_batch.py``) instead.
    """
    if subject not in SUBJECT_CHOICES:
        raise HTTPException(status_code=422, detail=f"subject must be one of {SUBJECT_CHOICES}")
    if not (6 <= grade <= 12):
        raise HTTPException(status_code=422, detail="grade must be in 6..12")
    if engine not in ENGINE_CHOICES:
        raise HTTPException(status_code=422, detail=f"engine must be one of {ENGINE_CHOICES}")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="only PDF uploads are supported")

    job_id = uuid.uuid4().hex
    job_dir = _JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = job_dir / "input.pdf"
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    pdf_path.write_bytes(contents)

    start = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            _run_ocr_pipeline,
            pdf_path=pdf_path,
            job_dir=job_dir,
            job_id=job_id,
            original_filename=file.filename,
            subject=subject,
            grade=grade,
            engine_name=engine,
            enable_formula_retry=enable_formula_retry,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("k12_ocr_process failed for job=%s: %s", job_id, exc)
        # Don't leave a half-baked job directory around.
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"OCR failed: {exc}")

    result.total_elapsed_seconds = round(time.perf_counter() - start, 2)
    _result_path(job_id).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "k12_ocr job=%s done: %d assets, %d block items, %.1fs",
        job_id, len(result.assets), result.content_block_count, result.total_elapsed_seconds,
    )
    return result


@router.get("/k12-ocr/job/{job_id}", response_model=K12OcrResult)
async def k12_ocr_get_job(
    job_id: str,
    _user: User = Depends(get_current_active_superuser),
) -> K12OcrResult:
    """Reload a previously produced result (FE page-reload safety)."""
    path = _result_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    return K12OcrResult.model_validate_json(path.read_text(encoding="utf-8"))


class K12OcrUpdate(BaseModel):
    """Payload for PATCH /k12-ocr/job/{job_id}.

    Full-overwrite of the markdown body — payload is small (a few KB),
    and the alternative (diff-based) would be more complex with little
    benefit at this scale.
    """

    markdown: str


@router.patch("/k12-ocr/job/{job_id}", response_model=K12OcrResult)
async def k12_ocr_update_job(
    job_id: str,
    payload: K12OcrUpdate,
    _user: User = Depends(get_current_active_superuser),
) -> K12OcrResult:
    """Persist a user-edited markdown body for an existing job.

    Overwrites both ``result.json`` (the FE-facing payload) and
    ``raw.md`` (the convenience file under ``/media/...``). Other
    metadata (assets, formula stats, latency) is preserved as-is —
    edits are scoped to the text body.
    """
    path = _result_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    result = K12OcrResult.model_validate_json(path.read_text(encoding="utf-8"))
    result.markdown = payload.markdown
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (_job_dir(job_id) / "raw.md").write_text(payload.markdown, encoding="utf-8")
    logger.info("k12_ocr job=%s markdown updated: %d chars", job_id, len(payload.markdown))
    return result


class K12OcrRestoreRequest(BaseModel):
    """Optional override — defaults to the job's saved markdown."""

    markdown: str | None = None


_RESTORE_PROMPT = """\
Bạn là chuyên gia khôi phục dấu tiếng Việt cho văn bản OCR.

NHIỆM VỤ: Khôi phục đầy đủ DẤU TIẾNG VIỆT trong markdown sau (OCR của đề thi K12).

QUY TẮC BẮT BUỘC:
1. CHỈ thêm/sửa dấu tiếng Việt cho từ bị mất dấu (vd: "đim" → "điểm", "Thi gian" → "Thời gian", "đng" → "đồng", "Chng minh" → "Chứng minh", "đáp s" → "đáp số").
2. GIỮ NGUYÊN 100%:
   - Mọi block LaTeX `$...$` và `$$...$$` (KHÔNG đổi 1 ký tự nào bên trong).
   - Mọi link ảnh `![alt](path)` — giữ đúng path.
   - Cấu trúc markdown (heading, list, table, blank lines).
   - Tên riêng tiếng Anh / công thức hóa học.
   - Mọi con số và ký hiệu toán.
3. KHÔNG dịch, KHÔNG diễn giải, KHÔNG thêm câu mới, KHÔNG xóa câu nào.
4. KHÔNG sửa typo không phải về dấu (vd ký tự sai chính tả không có dấu vẫn giữ nguyên).
5. Output CHỈ là markdown đã khôi phục dấu — không thêm giải thích, không wrap trong code block.

INPUT MARKDOWN (đề thi {subject_label} lớp {grade}):
---
{markdown}
---

OUTPUT (chỉ markdown, không thêm gì):"""


_SUBJECT_LABEL = {
    "toan": "Toán", "ly": "Vật lý", "hoa": "Hóa học", "sinh": "Sinh học",
    "van": "Ngữ văn", "su": "Lịch sử", "dia": "Địa lý", "anh": "Tiếng Anh",
}


async def _gemini_restore_diacritics(markdown: str, subject: str, grade: int) -> str:
    """Send markdown to Gemini-flash for Vietnamese diacritic restoration.

    Preserves LaTeX, image refs and structure verbatim — Gemini is
    instructed to only touch tokens that look like de-accented Vietnamese.
    Falls back to the input markdown on any error so the caller can still
    persist *something* and the operator can fix manually.
    """
    from app.core.config import settings
    if not settings.GOOGLE_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY chưa cấu hình")
    try:
        from google import genai
        from google.genai import types as gen_types
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"google-genai not installed: {exc}")

    client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    prompt = _RESTORE_PROMPT.format(
        subject_label=_SUBJECT_LABEL.get(subject, subject),
        grade=grade,
        markdown=markdown,
    )
    # Use Gemini 2.5 flash (settings.GEMINI_MODEL) — fast + cheap for this
    # text-only refinement.
    model = getattr(settings, "GEMINI_MODEL", None) or "gemini-2.5-flash"
    cfg = gen_types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=max(2048, min(32768, len(markdown))),
        safety_settings=[
            gen_types.SafetySetting(category=c, threshold="BLOCK_NONE")
            for c in ("HARM_CATEGORY_DANGEROUS_CONTENT", "HARM_CATEGORY_HARASSMENT",
                      "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT")
        ],
    )
    try:
        response = await client.aio.models.generate_content(
            model=model, contents=prompt, config=cfg,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini error: {exc}")

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="Gemini returned empty restoration")
    # Strip accidental code-block wrappers.
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline > 0:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return text


@router.post("/k12-ocr/job/{job_id}/restore", response_model=K12OcrResult)
async def k12_ocr_restore_diacritics(
    job_id: str,
    payload: K12OcrRestoreRequest | None = None,
    _user: User = Depends(get_current_active_superuser),
) -> K12OcrResult:
    """Restore Vietnamese diacritics on the job's markdown via Gemini.

    Reads the current job result, sends the markdown (or the caller's
    override) to Gemini for diacritic restoration, then **does NOT auto-
    persist** — the response carries the restored markdown so the FE can
    drop it into the Edit-tab draft for review before the user clicks
    Save. This preserves the undo path: if Gemini regresses something,
    the operator can still hit Reset.
    """
    path = _result_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    result = K12OcrResult.model_validate_json(path.read_text(encoding="utf-8"))
    source_md = (payload.markdown if payload and payload.markdown else result.markdown) or ""
    if not source_md.strip():
        raise HTTPException(status_code=422, detail="markdown rỗng — không có gì để restore")
    logger.info("k12_ocr restore: job=%s input=%d chars", job_id, len(source_md))
    restored = await _gemini_restore_diacritics(source_md, result.subject, result.grade)
    logger.info("k12_ocr restore: job=%s output=%d chars", job_id, len(restored))
    # Return the restored markdown without writing to disk; FE handles
    # the Save flow (PATCH /job/{id}) when the user accepts the result.
    result.markdown = restored
    return result


def _build_engine(name: str) -> OcrEngine:
    """Instantiate the chosen OCR engine, with a helpful error if missing."""
    if name == "mineru":
        engine = MinerUEngine()
        if not engine.is_available():
            raise RuntimeError("MinerU not available — install `mineru` CLI or set MINERU_CMD")
        return engine
    if name == "marker":
        engine = MarkerEngine()
        if not engine.is_available():
            raise RuntimeError(
                "Marker not available — install `marker-pdf` package or set MARKER_CMD"
            )
        return engine
    if name == "paddle":
        engine = PaddleEngine()
        if not engine.is_available():
            raise RuntimeError(
                "PaddleOCR-VL not available — install `paddleocr` + `paddlepaddle` "
                "(or `paddlepaddle-gpu` for CUDA)"
            )
        return engine
    raise RuntimeError(f"unknown engine: {name}")


def _run_ocr_pipeline(
    *,
    pdf_path: Path,
    job_dir: Path,
    job_id: str,
    original_filename: str,
    subject: str,
    grade: int,
    engine_name: str,
    enable_formula_retry: bool,
) -> K12OcrResult:
    """Synchronous OCR pipeline — runs in a worker thread."""
    config = load_config(_REPO_ROOT / "config" / "ocr_k12_batch.yaml")

    # Wire OCR engine env knobs from config. These are *our* tunables — use
    # direct assignment so a stale shell value (e.g. the old 600s default
    # baked in by `OCR_BENCHMARK_PER_ENGINE_TIMEOUT`) can't shadow what the
    # admin set in `config/ocr_k12_batch.yaml`. setdefault would respect the
    # pre-existing value and silently keep a 570s subprocess timeout, which
    # is the root cause Marker just hit on a Vietnamese exam.
    import os
    timeout_str = str(config.mineru.timeout_seconds)
    os.environ.setdefault("MINERU_METHOD", config.mineru.method)
    if config.mineru.backend:
        os.environ.setdefault("MINERU_BACKEND", config.mineru.backend)
    os.environ["OCR_BENCHMARK_PER_ENGINE_TIMEOUT"] = timeout_str
    os.environ["MINERU_BENCHMARK_TIMEOUT_SECONDS"] = timeout_str
    # Marker's `benchmark_endpoint_timeout_seconds("marker")` checks
    # MARKER_BENCHMARK_PER_ENGINE_TIMEOUT *first* and short-circuits before
    # falling back to OCR_BENCHMARK_PER_ENGINE_TIMEOUT (see
    # app/benchmark/engines/base.py:124). The repo `.env` ships a 600s value
    # — overwrite explicitly here, not via setdefault.
    os.environ["MARKER_BENCHMARK_PER_ENGINE_TIMEOUT"] = timeout_str
    os.environ["MARKER_BENCHMARK_TIMEOUT_SECONDS"] = timeout_str
    # Keep Surya at default DPI (144) — 300 DPI was tried but blows past the
    # GTX 1650 4GB VRAM budget and grinds for >30min. Surya's VN tokenizer
    # already handles diacritics fine at 144; the tradeoff buys us ~3-5min
    # OCR runs instead of timeouts.
    os.environ["MARKER_BENCHMARK_HIGHRES_DPI"] = "144"
    os.environ["MARKER_BENCHMARK_LOWRES_DPI"] = "72"
    # Marker passes `--disable_image_extraction` by default (env 0) — the
    # K12 admin flow needs figures (mạch điện, hình hình học) extracted as
    # `![](images/...)` so the FE renderer + Edit preview can show them.
    os.environ["MARKER_BENCHMARK_EXTRACT_IMAGES"] = "1"

    work_dir = job_dir / f"_{engine_name}"
    work_dir.mkdir(parents=True, exist_ok=True)

    engine = _build_engine(engine_name)
    ocr_result = engine.run(pdf_path, work_dir)
    if ocr_result.status != "success":
        raise RuntimeError(f"{engine_name} failed: {ocr_result.error}")

    markdown = ocr_result.markdown or ""
    # content_list.json only ships from MinerU — Paddle doesn't write it.
    # That's fine: formula crop retry just won't find bboxes (recorded as
    # `no_bbox` in stats) and falls through cleanly.
    content_list = _read_content_list(work_dir)

    # Formula validation (and optional PaddleOCR-VL crop retry).
    if enable_formula_retry:
        markdown, fstats = revalidate_markdown(
            markdown=markdown,
            content_list=content_list,
            pdf_path=pdf_path,
            workdir=job_dir / "_tmp",
            max_workers=config.formula_validator.max_workers,
            max_retries=config.formula_validator.max_retries_per_doc,
        )
    else:
        from app.services.k12_batch.formula_validator import FormulaRetryStats
        fstats = FormulaRetryStats()

    # Resolve every referenced image, copy it next to the markdown so the
    # /media mount can serve it, and rewrite the markdown to point at the
    # new path. Original MinerU layout often nests images several dirs deep.
    images_dir = job_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    new_md, assets = _normalize_image_paths(markdown, work_dir, images_dir, job_id, content_list, ocr_result.assets)

    # Persist the final markdown so /media/.../raw.md is accessible too.
    (job_dir / "raw.md").write_text(new_md, encoding="utf-8")

    # Tidy up the engine's scratch dir to keep media_uploads compact.
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(job_dir / "_tmp", ignore_errors=True)

    return K12OcrResult(
        job_id=job_id,
        filename=original_filename,
        subject=subject,
        grade=grade,
        markdown=new_md,
        assets=assets,
        formula_stats=FormulaStatsOut(
            scanned=fstats.scanned,
            invalid=fstats.invalid,
            retried=fstats.retried,
            recovered=fstats.recovered,
            no_bbox=fstats.no_bbox,
            crop_failed=fstats.crop_failed,
            paddle_empty=fstats.paddle_empty,
        ),
        ocr_engine=engine_name,
        ocr_latency_ms=ocr_result.latency_ms,
        content_block_count=len(content_list) or len(ocr_result.assets),
        total_elapsed_seconds=0.0,  # set by caller after persistence
    )


_RE_IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _normalize_image_paths(
    markdown: str,
    work_dir: Path,
    images_dir: Path,
    job_id: str,
    content_list: list[dict[str, Any]],
    engine_assets: list[Any] | None = None,
) -> tuple[str, list[AssetOut]]:
    """Resolve, copy and rewrite every image reference in ``markdown``.

    ``content_list`` carries MinerU bbox/page metadata when available
    (Paddle does not write it). ``engine_assets`` is the ``OcrResult.assets``
    list — used as a secondary source of bbox info, and as a discovery
    fallback when the markdown itself has no ``![](url)`` references.

    Returns the rewritten markdown plus an asset list whose ``url`` points
    at the static ``/media/k12_ocr_jobs/{job_id}/images/<name>`` path the
    frontend can fetch directly.
    """
    seen: dict[str, str] = {}  # original_ref → new filename

    # Build a bbox lookup from both metadata sources.
    bbox_by_filename: dict[str, dict[str, Any]] = {}
    for item in content_list or []:
        if str(item.get("type") or "") != "image":
            continue
        img_path = str(item.get("img_path") or "").strip()
        if not img_path:
            continue
        bbox_by_filename[Path(img_path).name] = {
            "page": item.get("page_idx"),
            "bbox": item.get("bbox"),
            "caption": " ".join(item.get("image_caption") or []) or None,
        }
    for asset in engine_assets or []:
        path = getattr(asset, "path", "") or ""
        if not path:
            continue
        name = Path(path).name
        bbox_by_filename.setdefault(name, {
            "page": getattr(asset, "page", None),
            "bbox": getattr(asset, "bbox", None),
            "caption": getattr(asset, "caption", None),
        })

    def _copy_to_images(src: Path) -> str:
        dst = images_dir / src.name
        i = 1
        while dst.exists() and dst.stat().st_size != src.stat().st_size:
            dst = images_dir / f"{src.stem}_{i}{src.suffix}"
            i += 1
        if not dst.exists():
            shutil.copy2(src, dst)
        return dst.name

    def _replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        ref = match.group(2).strip()
        if ref.startswith(("http://", "https://", "data:")):
            return match.group(0)
        if ref in seen:
            return f"![{alt}](/media/k12_ocr_jobs/{job_id}/images/{seen[ref]})"
        src = (work_dir / ref) if not Path(ref).is_absolute() else Path(ref)
        if not src.exists():
            matches = list(work_dir.rglob(Path(ref).name))
            if not matches:
                return match.group(0)
            src = matches[0]
        seen[ref] = _copy_to_images(src)
        return f"![{alt}](/media/k12_ocr_jobs/{job_id}/images/{seen[ref]})"

    new_md = _RE_IMAGE_MD.sub(_replace, markdown)

    # Engines that don't embed image refs in the markdown (Paddle is one)
    # still expose extracted figures via OcrResult.assets — copy those too
    # so the FE gallery has something to show.
    if engine_assets:
        for asset in engine_assets:
            path = getattr(asset, "path", "") or ""
            if not path:
                continue
            src = (work_dir / path) if not Path(path).is_absolute() else Path(path)
            if not src.exists():
                candidates = list(work_dir.rglob(Path(path).name))
                if not candidates:
                    continue
                src = candidates[0]
            if src.name in seen.values():
                continue
            seen[path] = _copy_to_images(src)

    assets: list[AssetOut] = []
    for original_ref, filename in seen.items():
        meta = bbox_by_filename.get(filename) or bbox_by_filename.get(Path(original_ref).name) or {}
        assets.append(AssetOut(
            filename=filename,
            url=f"/media/k12_ocr_jobs/{job_id}/images/{filename}",
            page=meta.get("page"),
            bbox=meta.get("bbox"),
            caption=meta.get("caption"),
        ))
    return new_md, assets
