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


class ChandraEngine(OcrEngine):
    name = "chandra"

    def is_available(self) -> bool:
        return bool(command_template_from_env("CHANDRA_CMD")) or package_available("chandra") or command_available("chandra", "chandra-ocr") is not None

    def run(self, file_path: Path, output_dir: Path):
        start = time.perf_counter()
        invalid = invalid_template_reason("CHANDRA_CMD")
        if invalid:
            return self.skipped(invalid, output_dir)
        template = command_template_from_env("CHANDRA_CMD")
        command = command_available("chandra", "chandra-ocr")
        if not template and command:
            template = f'"{command}" "{{input}}" "{{output}}"'
        if not template:
            return self.skipped("CHANDRA_CMD/chandra command unavailable", output_dir)
        try:
            markdown, raw, latency_ms = run_command_template(engine=self.name, template=template, file_path=file_path, output_dir=output_dir)
            return result_from_command(self.name, markdown, raw, latency_ms, output_dir)
        except Exception as exc:
            return self.failed(exc, output_dir, int((time.perf_counter() - start) * 1000))
