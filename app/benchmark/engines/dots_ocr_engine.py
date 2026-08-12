"""dots.ocr adapter — rednote-hilab/dots.ocr 1.7B VLM.

License: MIT (clean commercial). Multilingual layout SOTA, VN tường minh trong
examples (cùng Tibetan, Russian, Kannada). Output: JSON layout + Markdown +
visualized bboxes — perfect cho overlay feature.

VRAM: 8-12GB BF16 → KHÔNG fit GTX 1650 4GB. Trên local sẽ chạy CPU mode (rất
chậm). Khuyến nghị test trên cloud GPU.

Install: pip install transformers>=4.45 (đã có sẵn). Model download tự động
qua HF (~3.4GB).
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

_DOTS_MODEL = os.getenv("DOTS_OCR_MODEL", "rednote-hilab/dots.ocr").strip()
_DOTS_DEVICE = os.getenv("DOTS_OCR_DEVICE", "auto").strip().lower()  # auto | cpu | cuda
_DOTS_MAX_PAGES = int(os.getenv("DOTS_OCR_MAX_PAGES", "10").split("#", 1)[0].strip() or "10")
_DOTS_DPI = int(os.getenv("DOTS_OCR_DPI", "150").split("#", 1)[0].strip() or "150")

_MODEL: Any = None
_PROCESSOR: Any = None
_RESOLVED_DEVICE: str | None = None


def _resolve_device() -> str:
    global _RESOLVED_DEVICE
    if _RESOLVED_DEVICE is not None:
        return _RESOLVED_DEVICE
    if _DOTS_DEVICE == "cpu":
        _RESOLVED_DEVICE = "cpu"
        return _RESOLVED_DEVICE
    if _DOTS_DEVICE == "cuda":
        _RESOLVED_DEVICE = "cuda"
        return _RESOLVED_DEVICE
    # auto
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            vram = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            if vram >= 8.0:
                _RESOLVED_DEVICE = "cuda"
                return _RESOLVED_DEVICE
            logger.warning("dots_ocr: VRAM %.1fGB < 8GB required, falling back to CPU", vram)
    except Exception:
        pass
    _RESOLVED_DEVICE = "cpu"
    return _RESOLVED_DEVICE


def _ensure_model() -> tuple[Any, Any, str]:
    global _MODEL, _PROCESSOR
    if _MODEL is not None and _PROCESSOR is not None:
        return _MODEL, _PROCESSOR, _resolve_device()

    from transformers import AutoModelForCausalLM, AutoProcessor  # type: ignore
    import torch  # type: ignore

    device = _resolve_device()
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    logger.info("dots_ocr: loading model=%s device=%s dtype=%s", _DOTS_MODEL, device, dtype)
    _PROCESSOR = AutoProcessor.from_pretrained(_DOTS_MODEL, trust_remote_code=True)
    _MODEL = AutoModelForCausalLM.from_pretrained(
        _DOTS_MODEL,
        torch_dtype=dtype,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )
    _MODEL = _MODEL.to(device)
    _MODEL.eval()
    return _MODEL, _PROCESSOR, device


def _pdf_to_images(pdf_path: Path, max_pages: int, dpi: int) -> list[Any]:
    """Convert PDF → list of PIL images (1 per page, capped at max_pages)."""
    try:
        import fitz  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"PyMuPDF + PIL required for PDF → image: {exc}") from exc

    doc = fitz.open(str(pdf_path))
    try:
        images: list[Any] = []
        n_pages = min(len(doc), max_pages)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i in range(n_pages):
            pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            images.append(img)
        return images
    finally:
        doc.close()


def _run_dots_on_image(model: Any, processor: Any, device: str, image: Any) -> str:
    """Process 1 image through dots.ocr, return markdown text."""
    # dots.ocr expects an image + a prompt asking for markdown output
    prompt = "Extract all text from this document image. Output in Markdown format with proper LaTeX for any mathematical formulas. Preserve table structure with Markdown tables. Include image placeholders as ![figure](page_X_fig_Y.png)."
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        },
    ]
    inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, return_tensors="pt")
    if hasattr(inputs, "to"):
        inputs = inputs.to(device)
    elif isinstance(inputs, dict):
        inputs = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in inputs.items()}

    import torch  # type: ignore

    with torch.no_grad():
        if isinstance(inputs, dict):
            outputs = model.generate(**inputs, max_new_tokens=4096, do_sample=False)
        else:
            outputs = model.generate(inputs, max_new_tokens=4096, do_sample=False)
    decoded = processor.batch_decode(outputs, skip_special_tokens=True)
    text = decoded[0] if decoded else ""
    # Strip the prompt echo if present (dots.ocr returns full conversation)
    if prompt in text:
        text = text.split(prompt, 1)[-1].strip()
    return text


class DotsOcrEngine(OcrEngine):
    name = "dots"
    supported_extensions = {".pdf"} | IMAGE_EXTENSIONS

    def is_available(self) -> bool:
        return package_available("transformers") and package_available("torch")

    def run(self, file_path: Path, output_dir: Path) -> OcrResult:
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        if not self.is_available():
            return self.skipped(
                "transformers + torch required for dots.ocr; pip install transformers torch",
                output_dir,
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            model, processor, device = _ensure_model()
        except Exception as exc:
            logger.exception("dots_ocr: model load failed")
            return self.failed(
                f"model load failed (try DOTS_OCR_DEVICE=cpu, may need 8GB+ VRAM for GPU): {exc}",
                output_dir,
                int((time.perf_counter() - start) * 1000),
            )

        # Prepare images
        try:
            if file_path.suffix.lower() == ".pdf":
                images = _pdf_to_images(file_path, _DOTS_MAX_PAGES, _DOTS_DPI)
            else:
                from PIL import Image  # type: ignore

                images = [Image.open(str(file_path)).convert("RGB")]
        except Exception as exc:
            return self.failed(
                f"image prep failed: {exc}",
                output_dir,
                int((time.perf_counter() - start) * 1000),
            )

        if not images:
            return self.failed("no pages extracted", output_dir, int((time.perf_counter() - start) * 1000))

        markdown_parts: list[str] = []
        page_errors: list[str] = []
        for idx, img in enumerate(images):
            try:
                page_text = _run_dots_on_image(model, processor, device, img)
                markdown_parts.append(page_text)
            except Exception as exc:
                logger.warning("dots_ocr: page %d failed: %s", idx, exc)
                page_errors.append(f"page {idx}: {exc}")
                markdown_parts.append("")

        markdown = "\n\n".join(p for p in markdown_parts if p).strip()
        latency_ms = int((time.perf_counter() - start) * 1000)
        raw: dict[str, Any] = {
            "engine_variant": "dots.ocr",
            "model": _DOTS_MODEL,
            "device": device,
            "page_count": len(images),
            "page_errors": page_errors[:10],
            "truncated_pages": len(images) >= _DOTS_MAX_PAGES,
        }
        # Save markdown
        try:
            (output_dir / "output.md").write_text(markdown, encoding="utf-8")
        except Exception:
            pass

        status = "success" if markdown.strip() else "failed"
        error = None if status == "success" else f"empty dots.ocr output ({len(page_errors)} page errors)"
        assets: list[OcrAsset] = []
        result = OcrResult(
            engine=self.name,
            status=status,
            error=error,
            latency_ms=latency_ms,
            markdown=markdown,
            raw=raw,
            assets=assets,
            metadata=metadata_from_markdown(markdown, page_count=len(images), assets=assets),
        )
        persist_result(result, output_dir)
        return result
