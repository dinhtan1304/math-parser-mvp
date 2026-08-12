"""Granite-Docling 258M adapter — IBM Granite-Docling VLM via Docling library.

License: Apache 2.0 (model + library). Compact VLM (258M params, ~530MB), fit
GTX 1650 4GB. F1 0.968 trên equation, F1 0.988 trên code. Yếu cho VN long text
(no explicit VN training data) → primarily for math + table-heavy docs.

Install: docling đã có sẵn. Model download tự động lần chạy đầu (~530MB).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from app.benchmark.engines.base import (
    IMAGE_EXTENSIONS,
    OcrEngine,
    metadata_from_markdown,
    package_available,
    persist_result,
)
from app.benchmark.types import OcrAsset, OcrResult

logger = logging.getLogger(__name__)

_GRANITE_DEVICE = os.getenv("GRANITE_DOCLING_DEVICE", "auto").strip().lower()  # auto | cpu | cuda
_GRANITE_MODEL = os.getenv("GRANITE_DOCLING_MODEL", "ibm-granite/granite-docling-258M").strip()

_CONVERTER: Any = None


def _ensure_converter() -> Any:
    """Lazy init Docling DocumentConverter với Granite-Docling VLM pipeline."""
    global _CONVERTER
    if _CONVERTER is not None:
        return _CONVERTER

    from docling.datamodel.base_models import InputFormat  # type: ignore
    from docling.datamodel.pipeline_options import VlmPipelineOptions, AcceleratorDevice  # type: ignore
    from docling.document_converter import DocumentConverter, PdfFormatOption  # type: ignore
    from docling.pipeline.vlm_pipeline import VlmPipeline  # type: ignore

    pipeline_options = VlmPipelineOptions()
    # Best-effort device detection
    if _GRANITE_DEVICE == "cuda":
        try:
            pipeline_options.accelerator_options.device = AcceleratorDevice.CUDA
        except Exception:
            pass
    elif _GRANITE_DEVICE == "cpu":
        try:
            pipeline_options.accelerator_options.device = AcceleratorDevice.CPU
        except Exception:
            pass
    # device=auto → để Docling tự detect (default)

    # Try set granite-docling model if VlmPipelineOptions supports it
    try:
        from docling.datamodel.pipeline_options import granite_vision_vlm_conversion_options  # type: ignore  # noqa: F401
        # Use granite_docling preset if available
        try:
            from docling.datamodel.pipeline_options import granite_docling_vlm_conversion_options  # type: ignore
            pipeline_options.vlm_options = granite_docling_vlm_conversion_options
        except Exception:
            pass
    except Exception:
        pass

    logger.info("granite_docling: initializing DocumentConverter (device=%s, model=%s)", _GRANITE_DEVICE, _GRANITE_MODEL)
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline,
                pipeline_options=pipeline_options,
            ),
            InputFormat.IMAGE: PdfFormatOption(
                pipeline_cls=VlmPipeline,
                pipeline_options=pipeline_options,
            ),
        },
    )
    _CONVERTER = converter
    return converter


class GraniteDoclingEngine(OcrEngine):
    name = "granite-docling"
    supported_extensions = {".pdf"} | IMAGE_EXTENSIONS

    def is_available(self) -> bool:
        return package_available("docling")

    def run(self, file_path: Path, output_dir: Path) -> OcrResult:
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        if not package_available("docling"):
            return self.skipped(
                "docling package unavailable; pip install 'docling[vlm]' to enable Granite-Docling",
                output_dir,
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            converter = _ensure_converter()
            conv_result = converter.convert(str(file_path))
            doc = conv_result.document
            # Export markdown
            markdown = ""
            try:
                markdown = doc.export_to_markdown()
            except Exception:
                # Fallback to dict export
                try:
                    markdown = str(doc.export_to_dict())
                except Exception:
                    markdown = ""

            # Persist markdown
            try:
                (output_dir / "output.md").write_text(markdown or "", encoding="utf-8")
            except Exception:
                pass

            # Collect assets (figures, tables)
            assets: list[OcrAsset] = []
            try:
                for picture in getattr(doc, "pictures", []) or []:
                    page = None
                    bbox = None
                    try:
                        if picture.prov:
                            page = picture.prov[0].page_no
                            bbox_obj = picture.prov[0].bbox
                            bbox = [bbox_obj.l, bbox_obj.t, bbox_obj.r, bbox_obj.b] if bbox_obj else None
                    except Exception:
                        pass
                    assets.append(OcrAsset(type="figure", path="", page=page, bbox=bbox))
            except Exception as exc:
                logger.debug("granite_docling: asset extraction failed: %s", exc)

            latency_ms = int((time.perf_counter() - start) * 1000)
            raw: dict[str, Any] = {
                "engine_variant": "granite-docling",
                "device": _GRANITE_DEVICE,
                "model": _GRANITE_MODEL,
                "page_count": len(getattr(doc, "pages", []) or []) if hasattr(doc, "pages") else 0,
            }

            status = "success" if (markdown or "").strip() else "failed"
            error = None if status == "success" else "empty granite-docling output"
            result = OcrResult(
                engine=self.name,
                status=status,
                error=error,
                latency_ms=latency_ms,
                markdown=markdown or "",
                raw=raw,
                assets=assets,
                metadata=metadata_from_markdown(markdown or "", page_count=raw["page_count"], assets=assets),
            )
            persist_result(result, output_dir)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("granite_docling: convert failed for %s", file_path)
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))
