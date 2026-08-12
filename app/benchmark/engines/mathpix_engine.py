from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.benchmark.engines.base import DEFAULT_TIMEOUT_SECONDS, OcrEngine, metadata_from_markdown, persist_result
from app.benchmark.types import OcrResult


class MathpixEngine(OcrEngine):
    name = "mathpix"
    supported_extensions = {".pdf", ".png", ".jpg", ".jpeg"}

    def is_available(self) -> bool:
        return bool(os.getenv("MATHPIX_APP_ID") and os.getenv("MATHPIX_APP_KEY"))

    def run(self, file_path: Path, output_dir: Path):
        start = time.perf_counter()
        if not self.supports(file_path):
            return self.skipped(f"unsupported extension: {file_path.suffix}", output_dir)
        app_id = os.getenv("MATHPIX_APP_ID", "").strip()
        app_key = os.getenv("MATHPIX_APP_KEY", "").strip()
        if not app_id or not app_key:
            return self.skipped("MATHPIX_APP_ID/MATHPIX_APP_KEY unavailable", output_dir)
        try:
            import base64

            data_uri = "data:application/pdf;base64," if file_path.suffix.lower() == ".pdf" else "data:image/jpeg;base64,"
            payload = {
                "src": data_uri + base64.b64encode(file_path.read_bytes()).decode("ascii"),
                "formats": ["md", "text"],
                "data_options": {"include_asciimath": True, "include_latex": True},
            }
            request = Request(
                "https://api.mathpix.com/v3/text",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "app_id": app_id,
                    "app_key": app_key,
                },
                method="POST",
            )
            with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = json.loads(response.read().decode("utf-8"))
            markdown = str(raw.get("md") or raw.get("text") or "")
            latency_ms = int((time.perf_counter() - start) * 1000)
            result = OcrResult(
                engine=self.name,
                status="success" if markdown.strip() else "failed",
                error=None if markdown.strip() else "empty mathpix output",
                latency_ms=latency_ms,
                markdown=markdown,
                raw=raw,
                assets=[],
                metadata=metadata_from_markdown(markdown, page_count=1),
            )
            persist_result(result, output_dir)
            return result
        except (HTTPError, URLError, TimeoutError, Exception) as exc:
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))
