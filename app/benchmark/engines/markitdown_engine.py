from __future__ import annotations

import subprocess
import time
from pathlib import Path

from app.benchmark.engines.base import (
    DEFAULT_TIMEOUT_SECONDS,
    OcrEngine,
    command_available,
    metadata_from_markdown,
    normalize_text_markdown,
    package_available,
    persist_result,
)
from app.benchmark.types import OcrMetadata, OcrResult


class MarkItDownEngine(OcrEngine):
    name = "markitdown"
    supported_extensions = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".txt", ".md", ".csv"}

    def is_available(self) -> bool:
        return package_available("markitdown") or command_available("markitdown") is not None

    def run(self, file_path: Path, output_dir: Path) -> OcrResult:
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        if not self.is_available():
            return self.skipped("markitdown package/command unavailable", output_dir)
        try:
            markdown = ""
            raw = {"command": None}
            if package_available("markitdown"):
                from markitdown import MarkItDown  # type: ignore

                converted = MarkItDown().convert(str(file_path))
                markdown = normalize_text_markdown(getattr(converted, "text_content", "") or str(converted))
                raw = {"api": "markitdown.MarkItDown.convert"}
            else:
                command = ["markitdown", str(file_path)]
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
                raw = {
                    "command": command,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-12000:],
                    "stderr": completed.stderr[-12000:],
                }
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr[:500])
                markdown = normalize_text_markdown(completed.stdout)
            latency_ms = int((time.perf_counter() - start) * 1000)
            result = OcrResult(
                engine=self.name,
                status="success",
                error=None,
                latency_ms=latency_ms,
                markdown=markdown,
                raw=raw,
                assets=[],
                metadata=metadata_from_markdown(markdown, page_count=1),
            )
            persist_result(result, output_dir)
            return result
        except Exception as exc:
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))
