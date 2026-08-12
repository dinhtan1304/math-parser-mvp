from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.benchmark.engines.base import (
    IMAGE_EXTENSIONS,
    OcrEngine,
    command_available,
    command_template_from_env,
    invalid_template_reason,
    metadata_from_markdown,
    package_available,
    persist_result,
    result_from_command,
    run_command_template,
)
from app.benchmark.types import OcrAsset, OcrResult

logger = logging.getLogger(__name__)

# Bật PaddleOCR-VL (0.9B VLM, Apache 2.0, 109 langs incl. VN, dedicated formula head)
# Nếu env PADDLE_USE_VL=0 → quay về PaddleOCR cũ (image-only, không LaTeX).
_USE_VL = os.getenv("PADDLE_USE_VL", "1").strip().lower() not in {"0", "false", "no", "off"}
_VL_DEVICE = os.getenv("PADDLE_VL_DEVICE", "auto").strip().lower()  # auto | cpu | gpu
# Checkpoint/pipeline của PaddleOCR-VL. Mặc định v1.6 (SOTA OmniDocBench v1.6,
# ra mắt 28/05/2026, kiến trúc tương thích v1.5). Nếu bản paddleocr đang cài
# chưa có version này → tự lùi về default của package (tránh crash
# "Invalid pipeline version"). Cần paddleocr>=3.6.0 để có v1.6.
_VL_PIPELINE_VERSION = os.getenv("PADDLE_VL_PIPELINE_VERSION", "v1.6").strip().lower()

# Cache pipeline instance để tránh load model nhiều lần
_VL_PIPELINE: Any = None
_LEGACY_OCR: Any = None
_DLL_PATCHED = False


def _ensure_nvidia_dlls() -> None:
    """Make the nvidia-* CUDA wheel DLLs discoverable on Windows.

    paddlepaddle-gpu loads cuDNN from ``site-packages/nvidia/cudnn/bin/`` and
    that DLL has transitive dependencies (cuBLAS, nvrtc, …) in sibling
    ``site-packages/nvidia/*/bin/`` dirs. On Windows ``os.add_dll_directory``
    alone does NOT cover paddle's transitive load order — the loader fails the
    cudnn_cnn64_9.dll dependency chain with ``WinError 127`` (procedure not
    found). Prepending those dirs to ``PATH`` as well fixes the chain. Must run
    BEFORE ``import paddle``. Idempotent; no-op on non-Windows or when no nvidia
    wheels are present.
    """
    global _DLL_PATCHED
    if _DLL_PATCHED or not hasattr(os, "add_dll_directory"):
        return
    try:
        import glob
        import site

        bin_dirs: list[str] = []
        for site_dir in site.getsitepackages() + [site.getusersitepackages()]:
            for bin_dir in glob.glob(os.path.join(site_dir, "nvidia", "*", "bin")):
                if bin_dir not in bin_dirs:
                    bin_dirs.append(bin_dir)
                try:
                    os.add_dll_directory(bin_dir)
                except (FileNotFoundError, OSError):
                    pass
        # add_dll_directory is insufficient for paddle's transitive cuDNN→cuBLAS/
        # nvrtc resolution; PATH is what actually unblocks the chain.
        if bin_dirs:
            os.environ["PATH"] = os.pathsep.join(bin_dirs) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        # Best-effort — failure here just means paddle GPU import may
        # still fail; we log nothing because paddle's own error is more useful.
        pass
    _DLL_PATCHED = True


# Thứ tự version tăng dần. Ladder dùng để tự lùi khi version mới hơn (vd v1.6)
# CHƯA chạy được trên bản paddlex đang cài — paddleocr 3.6.0 *biết tên* v1.6 nhưng
# paddlex chỉ *đăng ký pipeline class* tới v1.5 → init v1.6 raise ngay ở bước tra
# registry (TRƯỚC khi tải model, nên thử-lùi rất rẻ). Khi paddlex về sau đăng ký
# v1.6, code tự dùng v1.6 mà không cần sửa.
_VERSION_ORDER = ["v1", "v1.5", "v1.6"]


def _candidate_versions(requested: str) -> list[str]:
    """Ladder giảm dần từ ``requested`` về v1 (vd 'v1.6' → ['v1.6','v1.5','v1'])."""
    req = (requested or "").strip().lower()
    if req in _VERSION_ORDER:
        idx = _VERSION_ORDER.index(req)
        return list(reversed(_VERSION_ORDER[: idx + 1]))
    return list(reversed(_VERSION_ORDER))


def _isolate_from_torch_cudnn() -> None:
    """Stop paddlex's hard ``import modelscope`` from pulling in torch.

    ``paddlex/inference/utils/official_models.py`` does an unconditional
    top-level ``import modelscope`` (a model-download hub). modelscope imports
    torch, whose bundled cuDNN (``cudnn_cnn64_9.dll``) is a DIFFERENT build than
    paddle's — the moment both are loaded in one process the second one fails
    with ``WinError 127``. We never need modelscope at inference: models are
    cached locally and ``PADDLE_PDX_MODEL_SOURCE`` defaults to ``huggingface``
    (downloads go through huggingface_hub, not modelscope). So install a benign
    stub — but ONLY if torch/modelscope haven't loaded yet. If torch is already
    present (e.g. an admin torch OCR engine ran earlier in this process) we leave
    things alone; paddle + torch simply can't share one process on GPU.
    """
    import sys
    import types

    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "huggingface")
    if "torch" in sys.modules or "modelscope" in sys.modules:
        return
    stub = types.ModuleType("modelscope")
    stub.__spec__ = None
    sys.modules["modelscope"] = stub
    logger.info("paddle_engine: stubbed modelscope to keep torch out of the paddle process")


def _ensure_vl_pipeline() -> Any:
    global _VL_PIPELINE
    if _VL_PIPELINE is not None:
        return _VL_PIPELINE
    _ensure_nvidia_dlls()
    _isolate_from_torch_cudnn()
    from paddleocr import PaddleOCRVL  # type: ignore

    base_kwargs: dict[str, Any] = {}
    if _VL_DEVICE == "cpu":
        base_kwargs["device"] = "cpu"
    elif _VL_DEVICE == "gpu":
        base_kwargs["device"] = "gpu"
    # device=auto → để PaddleOCRVL tự detect

    candidates = _candidate_versions(_VL_PIPELINE_VERSION)
    last_exc: Exception | None = None
    for ver in candidates:
        try:
            logger.info(
                "paddle_engine: initializing PaddleOCRVL (device=%s, pipeline_version=%s)",
                _VL_DEVICE, ver,
            )
            _VL_PIPELINE = PaddleOCRVL(pipeline_version=ver, **base_kwargs)
            if ver != (_VL_PIPELINE_VERSION or "").strip().lower():
                logger.warning(
                    "paddle_engine: PaddleOCR-VL %s chưa chạy được trên paddlex hiện tại "
                    "→ dùng %s (bản runnable mới nhất). Nâng paddlex để bật version mới.",
                    _VL_PIPELINE_VERSION, ver,
                )
            return _VL_PIPELINE
        except Exception as exc:  # registry/config miss → thử version thấp hơn
            last_exc = exc
            logger.warning(
                "paddle_engine: PaddleOCR-VL %s không khởi tạo được (%s); thử version thấp hơn",
                ver, exc,
            )
    raise RuntimeError(
        f"Không có PaddleOCR-VL pipeline version nào chạy được (đã thử {candidates}): {last_exc}"
    )


# Pure layout/bbox helpers live in app.services.paddle_vl_common (shared with the
# remote API client). Alias kept for back-compat (tests import _normalize_bbox_to_unit).
from app.services.paddle_vl_common import (  # noqa: E402
    layout_blocks_from_res_data as _layout_blocks_from_res_data,
    normalize_bbox_to_unit as _normalize_bbox_to_unit,
)


def _extract_layout_blocks(res: Any, page_idx: int) -> list[dict[str, Any]]:
    """Pull semantic layout blocks from a PaddleOCR-VL page result's JSON
    (``res.json["res"]``) → ``[{page_index,label,content,bbox}]``. Best-effort."""
    try:
        payload = res.json
    except Exception:
        return []
    data = payload.get("res") if isinstance(payload, dict) and "res" in payload else payload
    return _layout_blocks_from_res_data(data, page_idx)


def _ensure_legacy_ocr() -> Any:
    global _LEGACY_OCR
    if _LEGACY_OCR is not None:
        return _LEGACY_OCR
    _ensure_nvidia_dlls()
    from paddleocr import PaddleOCR  # type: ignore

    _LEGACY_OCR = PaddleOCR(use_angle_cls=True, lang="vi")
    return _LEGACY_OCR


class PaddleEngine(OcrEngine):
    name = "paddle"
    supported_extensions = IMAGE_EXTENSIONS | {".pdf"}

    def is_available(self) -> bool:
        return (
            bool(command_template_from_env("PADDLE_CMD"))
            or package_available("paddleocr")
            or command_available("paddleocr") is not None
        )

    def run(self, file_path: Path, output_dir: Path):
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        invalid = invalid_template_reason("PADDLE_CMD")
        if invalid:
            return self.skipped(invalid, output_dir)

        # CLI override (highest priority for power users)
        template = command_template_from_env("PADDLE_CMD")
        if template:
            try:
                markdown, raw, latency_ms = run_command_template(
                    engine=self.name, template=template, file_path=file_path, output_dir=output_dir,
                )
                return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
            except Exception as exc:
                return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))

        if not package_available("paddleocr"):
            return self.skipped("paddleocr package unavailable; set PADDLE_CMD to use CLI", output_dir)

        # Try PaddleOCR-VL (PDF + image support, output markdown with LaTeX)
        if _USE_VL:
            try:
                return self._run_paddle_vl(file_path, output_dir, start)
            except ImportError as exc:
                logger.warning("paddle_engine: PaddleOCRVL not available (%s) — falling back to legacy", exc)
            except Exception as exc:
                logger.exception("paddle_engine: PaddleOCRVL failed — falling back to legacy")
                # Fall through to legacy

        # Legacy PaddleOCR (image-only, no LaTeX)
        if file_path.suffix.lower() not in IMAGE_EXTENSIONS:
            return self.skipped(
                "Legacy PaddleOCR API only supports image inputs; set PADDLE_USE_VL=1 to enable PaddleOCR-VL for PDF",
                output_dir,
            )
        try:
            return self._run_paddle_legacy(file_path, output_dir, start)
        except Exception as exc:
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))

    def _run_paddle_vl(self, file_path: Path, output_dir: Path, start: float) -> OcrResult:
        pipeline = _ensure_vl_pipeline()
        output_dir.mkdir(parents=True, exist_ok=True)
        # PaddleOCRVL.predict trả về generator các page result. Save markdown qua API.
        output_results = pipeline.predict(str(file_path))
        markdown_parts: list[str] = []
        assets: list[OcrAsset] = []
        raw_pages: list[dict[str, Any]] = []
        layout_blocks: list[dict[str, Any]] = []

        # PaddleOCRVL trả về iterable; mỗi item là 1 trang result với method `markdown`
        for page_idx, res in enumerate(output_results):
            page_dir = output_dir / "pages"
            page_dir.mkdir(parents=True, exist_ok=True)
            try:
                # Save markdown to file (PaddleOCRVL standard API)
                res.save_to_markdown(save_path=str(page_dir))
                res.save_to_json(save_path=str(page_dir))
            except Exception as exc:
                logger.warning("paddle_vl: save_to_markdown failed page=%d: %s", page_idx, exc)
            # Extract markdown from result
            page_md = ""
            try:
                md_obj = res.markdown
                if isinstance(md_obj, dict):
                    page_md = str(md_obj.get("markdown_texts") or md_obj.get("text") or "")
                else:
                    page_md = str(md_obj or "")
            except Exception:
                page_md = ""
            markdown_parts.append(page_md)
            # Semantic layout blocks (label + normalized bbox) for the review overlay.
            try:
                layout_blocks.extend(_extract_layout_blocks(res, page_idx))
            except Exception as exc:
                logger.warning("paddle_vl: layout block extraction failed page=%d: %s", page_idx, exc)
            try:
                raw_pages.append({"page_index": page_idx, "json": res.json})
            except Exception:
                raw_pages.append({"page_index": page_idx})

        # Collect image assets that PaddleOCR saved
        for img_path in sorted(output_dir.rglob("*.png")) + sorted(output_dir.rglob("*.jpg")):
            assets.append(OcrAsset(
                type="figure",
                path=str(img_path.relative_to(output_dir)),
                page=None,
                bbox=None,
            ))

        markdown = "\n\n".join(p for p in markdown_parts if p).strip()
        latency_ms = int((time.perf_counter() - start) * 1000)
        raw: dict[str, Any] = {
            "engine_variant": "paddle-ocr-vl",
            "pipeline": "PaddleOCRVL",
            "pipeline_version": _VL_PIPELINE_VERSION,
            "pages": len(raw_pages),
            "device": _VL_DEVICE,
            "layout_blocks": layout_blocks,
        }
        # Persist raw pages JSON (truncated to avoid huge files)
        try:
            (output_dir / "pages_raw.json").write_text(
                json.dumps(raw_pages[:50], default=str, ensure_ascii=False)[:200000],
                encoding="utf-8",
            )
        except Exception:
            pass

        status = "success" if markdown.strip() else "failed"
        error = None if status == "success" else "empty paddle-vl output"
        result = OcrResult(
            engine=self.name,
            status=status,
            error=error,
            latency_ms=latency_ms,
            markdown=markdown,
            raw=raw,
            assets=assets,
            metadata=metadata_from_markdown(markdown, page_count=len(raw_pages), assets=assets),
        )
        persist_result(result, output_dir)
        return result

    def _run_paddle_legacy(self, file_path: Path, output_dir: Path, start: float) -> OcrResult:
        ocr = _ensure_legacy_ocr()
        rows = ocr.ocr(str(file_path), cls=True)
        lines: list[str] = []
        for page in rows or []:
            for item in page or []:
                if len(item) >= 2 and item[1]:
                    lines.append(str(item[1][0]))
        markdown = "\n".join(lines)
        latency_ms = int((time.perf_counter() - start) * 1000)
        result = OcrResult(
            engine=self.name,
            status="success" if markdown.strip() else "failed",
            error=None if markdown.strip() else "empty paddle output",
            latency_ms=latency_ms,
            markdown=markdown,
            raw={
                "engine_variant": "paddle-legacy",
                "api": "paddleocr.PaddleOCR.ocr",
                "line_count": len(lines),
            },
            assets=[],
            metadata=metadata_from_markdown(markdown, page_count=1),
        )
        persist_result(result, output_dir)
        return result
