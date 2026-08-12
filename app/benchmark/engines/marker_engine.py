from __future__ import annotations

import sys
import time
import os
from pathlib import Path

from app.benchmark.engines.base import (
    OcrEngine,
    benchmark_command_timeout_seconds,
    command_available,
    command_template_from_env,
    invalid_template_reason,
    package_available,
    result_from_command,
    result_from_native_pdf,
    run_command_template,
)
from app.benchmark.types import OcrResult
from app.services.native_pdf import extract_native_pdf_markdown
from app.services.pdf_detector import recommended_ocr_mode


class MarkerEngine(OcrEngine):
    name = "marker"
    supported_extensions = {".pdf", ".docx", ".pptx", ".html", ".txt", ".md"}

    def is_available(self) -> bool:
        return (
            bool(command_template_from_env("MARKER_CMD"))
            or command_available("marker_single", "marker") is not None
            or package_available("marker")
        )

    def run(self, file_path: Path, output_dir: Path) -> OcrResult:
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        invalid = invalid_template_reason("MARKER_CMD")
        if invalid:
            return self.skipped(invalid, output_dir)
        template = command_template_from_env("MARKER_CMD")
        try:
            timeout = benchmark_command_timeout_seconds(self.name)
            mode = recommended_ocr_mode(file_path)
            # Native PDF text shortcut bị bỏ: nó dùng PyMuPDF text layer → markdown
            # KHÔNG có LaTeX cho math (phân số/luỹ thừa/tích phân ra plain text).
            # Admin benchmark dùng để so sánh chất lượng LaTeX → phải đi qua Marker
            # với Surya texify. Có thể opt-in lại bằng env MARKER_BENCHMARK_ALLOW_NATIVE=1.
            if (
                not template
                and mode == "text"
                and os.getenv("MARKER_BENCHMARK_ALLOW_NATIVE", "0").strip().lower() in {"1", "true", "yes", "on"}
            ):
                native = extract_native_pdf_markdown(file_path)
                if str(native.get("text") or "").strip():
                    return result_from_native_pdf(self.name, native, int((time.perf_counter() - start) * 1000), output_dir)
            if template:
                markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir, timeout=timeout)
                return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
            command = command_available("marker_single")
            if command:
                template = _marker_cli_template(command, mode)
                markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir, timeout=timeout)
                return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
            if package_available("marker"):
                template = f'"{sys.executable}" -m app.benchmark.marker_subprocess "{{input}}" "{{output}}"'
                markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir, timeout=timeout)
                return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
            return self.skipped("marker package/command unavailable", output_dir)
        except Exception as exc:
            raw = getattr(exc, "raw", None)
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000), raw=raw)


def _marker_cli_template(command: str, mode: str = "auto") -> str:
    parts = [
        f'"{command}"',
        '--output_dir "{output}"',
        "--output_format markdown",
        "--disable_tqdm",
    ]
    if os.getenv("MARKER_BENCHMARK_EXTRACT_IMAGES", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        parts.append("--disable_image_extraction")
    # `--disable_ocr` làm Marker SKIP Surya texify → markdown KHÔNG có LaTeX.
    # Default mới: KHÔNG disable OCR; thêm `--force_ocr` để Surya texify chắc
    # chắn chạy trên text-layer PDF (đề HSG Toán Word→PDF) → sinh `$...$`.
    # Opt-out: `MARKER_BENCHMARK_DISABLE_OCR=1` (chỉ admin cần test text-only).
    disable_ocr_env = os.getenv("MARKER_BENCHMARK_DISABLE_OCR")
    if disable_ocr_env is None or disable_ocr_env.strip().lower() in {"", "auto", "detect"}:
        disable_ocr = False
    else:
        disable_ocr = disable_ocr_env.strip().lower() not in {"0", "false", "no", "off"}
    if disable_ocr:
        parts.append("--disable_ocr")
    else:
        parts.append("--force_ocr")
    lowres_dpi = os.getenv("MARKER_BENCHMARK_LOWRES_DPI", "72").strip()
    highres_dpi = os.getenv("MARKER_BENCHMARK_HIGHRES_DPI", "144").strip()
    if lowres_dpi:
        parts.append(f"--lowres_image_dpi {lowres_dpi}")
    if highres_dpi:
        parts.append(f"--highres_image_dpi {highres_dpi}")
    page_range = os.getenv("MARKER_BENCHMARK_PAGE_RANGE", "").strip()
    if page_range:
        parts.append(f'--page_range "{page_range}"')
    parts.append('"{input}"')
    return " ".join(parts)
