from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.benchmark.engines.base import (
    OcrEngine,
    command_available,
    command_template_from_env,
    invalid_template_reason,
    metadata_from_markdown,
    persist_result,
    result_from_command,
    result_from_native_pdf,
    run_command_template,
)
from app.benchmark.types import OcrAsset, OcrResult
from app.services.native_pdf import extract_native_pdf_markdown
from app.services.pdf_detector import recommended_ocr_mode


class MinerUEngine(OcrEngine):
    name = "mineru"
    supported_extensions = {".pdf"}

    def is_available(self) -> bool:
        return bool(command_template_from_env("MINERU_CMD")) or command_available("magic-pdf", "mineru") is not None

    def run(self, file_path: Path, output_dir: Path) -> OcrResult:
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        invalid = invalid_template_reason("MINERU_CMD")
        if invalid:
            return self.skipped(invalid, output_dir)
        try:
            template = command_template_from_env("MINERU_CMD")
            command = command_available("magic-pdf", "mineru")
            detected_mode = recommended_ocr_mode(file_path)
            # Native PDF text shortcut bị bỏ default: PyMuPDF text layer KHÔNG có
            # LaTeX (math ra plain text). Benchmark cần Markdown có LaTeX để so sánh.
            # Opt-in lại: MINERU_BENCHMARK_ALLOW_NATIVE=1.
            if (
                not template
                and detected_mode == "text"
                and os.getenv("MINERU_BENCHMARK_ALLOW_NATIVE", "0").strip().lower() in {"1", "true", "yes", "on"}
            ):
                native = extract_native_pdf_markdown(file_path)
                if str(native.get("text") or "").strip():
                    return result_from_native_pdf(self.name, native, int((time.perf_counter() - start) * 1000), output_dir)
            if template:
                markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir)
                _raise_if_silent_failure(markdown, raw)
                markdown, raw, assets = _parse_mineru_output(output_dir, markdown, raw)
                raw["assets"] = [asset.__dict__ for asset in assets]
                return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
            if not command:
                return self.skipped("magic-pdf/mineru command unavailable", output_dir)
            # New mineru CLI (3.x) and legacy magic-pdf both accept -p/-o.
            # `--lang vi` is not in mineru 3.x lang list; map Vietnamese → `latin`.
            # MINERU_BACKEND=pipeline skips the Qwen2-VL load (avoids os error 1455
            # on Windows when pagefile is small). Default keeps hybrid VLM.
            backend = os.getenv("MINERU_BACKEND", "").strip().lower()
            method = os.getenv("MINERU_METHOD", "").strip().lower()
            # Default `ocr`: chạy full MinerU pipeline (layout + formula recognition)
            # để sinh LaTeX. `txt` mode chỉ extract text layer → KHÔNG LaTeX.
            # Có thể opt-out fast path bằng MINERU_METHOD=txt.
            if not method or method in {"detect", "auto-detect"}:
                method = "ocr"
            if method not in {"auto", "txt", "ocr"}:
                method = "ocr"
            backend_arg = f" -b {backend}" if backend else ""
            template = f'"{command}" -p "{{input}}" -o "{{output}}" -m {method} --lang latin{backend_arg}'
            markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir)
            _raise_if_silent_failure(markdown, raw)
            markdown, raw, assets = _parse_mineru_output(output_dir, markdown, raw)
            raw["assets"] = [asset.__dict__ for asset in assets]
            result = OcrResult(
                engine=self.name,
                status="success",
                error=None,
                latency_ms=latency_ms,
                markdown=markdown,
                raw=raw,
                assets=assets,
                metadata=metadata_from_markdown(markdown, assets=assets),
            )
            persist_result(result, output_dir)
            return result
        except Exception as exc:
            raw = getattr(exc, "raw", None)
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000), raw=raw)


_FAILURE_MARKERS = ("Traceback", "FileNotFoundError", "ModuleNotFoundError", "ImportError")


def _raise_if_silent_failure(markdown: str, raw: dict) -> None:
    if markdown.strip():
        return
    stderr = str(raw.get("stderr") or "")
    if any(marker in stderr for marker in _FAILURE_MARKERS):
        raise RuntimeError(f"mineru produced no output; stderr: {stderr[-500:]}")


def _parse_mineru_output(output_dir: Path, markdown: str, raw: dict) -> tuple[str, dict, list[OcrAsset]]:
    assets: list[OcrAsset] = []
    content_files = sorted(output_dir.rglob("*content_list.json"))
    if not content_files:
        return markdown, raw, assets
    payload = json.loads(content_files[0].read_text(encoding="utf-8"))
    raw["content_list"] = payload
    parts: list[str] = []
    for item in payload:
        kind = str(item.get("type") or "unknown")
        if kind == "equation":
            # MinerU equation items chứa LaTeX raw. Wrap `$$...$$` để markdown
            # render đúng block math; KHÔNG re-wrap nếu MinerU đã có delimiters.
            text = str(item.get("text") or "").strip()
            if text:
                if not (text.startswith("$") or text.startswith("\\(") or text.startswith("\\[")):
                    text = f"$${text}$$"
                parts.append(text)
        elif kind == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
        elif kind == "table":
            caption = " ".join(item.get("table_caption") or [])
            body = str(item.get("table_body") or "")
            parts.append(f"\n\n[Bảng: {caption}]\n{body}" if caption else f"\n\n[Bảng]\n{body}")
            assets.append(OcrAsset(type="table", path="", page=item.get("page_idx"), bbox=item.get("bbox"), caption=caption or None))
        elif kind == "image":
            image_path = str(item.get("img_path") or "")
            caption = " ".join(item.get("image_caption") or [])
            parts.append(f"\n\n![{caption or 'figure'}]({image_path})")
            assets.append(OcrAsset(type="figure", path=image_path, page=item.get("page_idx"), bbox=item.get("bbox"), caption=caption or None))
    return ("\n\n".join(parts) or markdown), raw, assets
