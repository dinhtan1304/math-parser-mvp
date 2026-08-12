from __future__ import annotations

import time
from pathlib import Path

from app.benchmark.engines.base import (
    OcrEngine,
    command_available,
    command_template_from_env,
    invalid_template_reason,
    package_available,
    result_from_command,
    run_command_template,
)


class OlmOcrEngine(OcrEngine):
    name = "olmocr"
    supported_extensions = {".pdf", ".png", ".jpg", ".jpeg"}

    def is_available(self) -> bool:
        return bool(command_template_from_env("OLMOCR_CMD")) or package_available("olmocr") or command_available("olmocr") is not None

    def run(self, file_path: Path, output_dir: Path):
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        invalid = invalid_template_reason("OLMOCR_CMD")
        if invalid:
            return self.skipped(invalid, output_dir)
        template = command_template_from_env("OLMOCR_CMD")
        command = command_available("olmocr")
        if not template and command:
            template = f'"{command}" "{{input}}" --out "{{output}}"'
        if not template:
            return self.skipped("OLMOCR_CMD/olmocr command unavailable", output_dir)
        try:
            markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir)
            return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
        except Exception as exc:
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))
