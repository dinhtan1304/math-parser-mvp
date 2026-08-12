from __future__ import annotations

import importlib.util
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.benchmark.latex_utils import analyze_latex
from app.benchmark.types import OcrAsset, OcrMetadata, OcrResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = int(os.getenv("OCR_BENCHMARK_TIMEOUT_SECONDS", "0") or "0")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".txt", ".md"}
_TIMEOUT_NOT_SET = object()


class OcrEngine:
    name: str = "base"
    supported_extensions: set[str] = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS

    def is_available(self) -> bool:
        return False

    def supports(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.supported_extensions

    def run(self, file_path: Path, output_dir: Path) -> OcrResult:
        raise NotImplementedError

    def skipped(self, reason: str, output_dir: Path, latency_ms: int = 0) -> OcrResult:
        result = OcrResult(
            engine=self.name,
            status="skipped",
            error=reason,
            latency_ms=latency_ms,
            markdown="",
            raw={"reason": reason},
            assets=[],
            metadata=OcrMetadata(),
        )
        persist_result(result, output_dir)
        return result

    def failed(self, error: Exception | str, output_dir: Path, latency_ms: int = 0, raw: dict[str, Any] | None = None) -> OcrResult:
        result = OcrResult(
            engine=self.name,
            status="failed",
            error=str(error),
            latency_ms=latency_ms,
            markdown="",
            raw=raw or {},
            assets=[],
            metadata=OcrMetadata(),
        )
        persist_result(result, output_dir)
        return result


def package_available(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def command_available(*commands: str) -> str | None:
    script_dir = Path(sys.executable).parent
    suffixes = ["", ".exe", ".cmd", ".bat"] if os.name == "nt" else [""]
    for command in commands:
        found = shutil.which(command)
        if found:
            return found
        for suffix in suffixes:
            candidate = script_dir / f"{command}{suffix}"
            if candidate.exists():
                return str(candidate)
    return None


def command_template_from_env(env_name: str) -> str | None:
    template = os.getenv(env_name, "").strip()
    if not template:
        return None
    if "{input}" not in template or "{output}" not in template:
        return None
    return template


def invalid_template_reason(env_name: str) -> str | None:
    value = os.getenv(env_name, "").strip()
    if value and ("{input}" not in value or "{output}" not in value):
        return f"{env_name} must include {{input}} and {{output}} placeholders"
    return None


def _read_timeout_env(*names: str) -> tuple[bool, int | None]:
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        if value.lower() in {"none", "unlimited", "off"}:
            return True, None
        try:
            parsed = int(value)
        except ValueError:
            continue
        if parsed <= 0:
            return True, None
        return True, parsed
    return False, None


def benchmark_endpoint_timeout_seconds(engine_name: str | None = None) -> int | None:
    if engine_name:
        found, timeout = _read_timeout_env(f"{engine_name.upper()}_BENCHMARK_PER_ENGINE_TIMEOUT")
        if found:
            return timeout
        if engine_name.lower() == "marker":
            return None
    names = ["OCR_BENCHMARK_PER_ENGINE_TIMEOUT"]
    found, timeout = _read_timeout_env(*names)
    return timeout if found else 600


def benchmark_command_timeout_seconds(engine_name: str | None = None) -> int | None:
    """Timeout for subprocess OCR commands.

    Keep this below the FastAPI endpoint per-engine timeout so subprocess.run
    can terminate the child process before the request-level guard gives up.
    """
    endpoint_timeout = benchmark_endpoint_timeout_seconds(engine_name)
    names = []
    if engine_name:
        names.append(f"{engine_name.upper()}_BENCHMARK_TIMEOUT_SECONDS")
    names.append("OCR_BENCHMARK_TIMEOUT_SECONDS")
    configured_found, configured = _read_timeout_env(*names)
    if endpoint_timeout is None:
        return configured if configured_found else None
    default_timeout = max(1, endpoint_timeout - min(30, max(1, endpoint_timeout // 4)))
    if not configured_found or configured is None:
        return default_timeout
    return min(configured, default_timeout)


def run_command_template(
    *,
    engine: str,
    template: str,
    file_path: Path,
    output_dir: Path,
    timeout: int | None | object = _TIMEOUT_NOT_SET,
) -> tuple[str, dict[str, Any], int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = template.format(input=str(file_path), output=str(output_dir))
    effective_timeout = (
        benchmark_command_timeout_seconds()
        if timeout is _TIMEOUT_NOT_SET
        else timeout
    )
    start = time.perf_counter()
    completed = _run_command_process_tree(
        command,
        timeout=effective_timeout,
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    raw = {
        "engine": engine,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
        "timeout_seconds": effective_timeout,
    }
    if completed.returncode != 0:
        try:
            (output_dir / "stderr.log").write_text(completed.stderr or "", encoding="utf-8")
            (output_dir / "stdout.log").write_text(completed.stdout or "", encoding="utf-8")
        except Exception:
            pass
        stderr_tail = (completed.stderr or "")[-4000:]
        exc = RuntimeError(
            f"{engine} exit={completed.returncode}; full stderr at {output_dir / 'stderr.log'}\n"
            f"--- stderr (last 4000 chars) ---\n{stderr_tail}"
        )
        exc.raw = raw  # type: ignore[attr-defined]
        raise exc
    markdown, discovered_raw, assets = discover_outputs(output_dir)
    if not markdown and completed.stdout.strip():
        markdown = completed.stdout
    raw["discovered"] = discovered_raw
    persist_markdown(markdown, output_dir)
    return markdown, raw, latency_ms


def _run_command_process_tree(command: str, *, timeout: int | None) -> subprocess.CompletedProcess:
    creationflags = 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        timeout_exc = RuntimeError(f"command timed out after {timeout}s")
        timeout_exc.raw = {  # type: ignore[attr-defined]
            "command": command,
            "returncode": process.returncode,
            "stdout": (stdout or "")[-12000:],
            "stderr": (stderr or "")[-12000:],
            "timeout_seconds": timeout,
            "timeout": True,
        }
        raise timeout_exc from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
            )
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


def discover_outputs(output_dir: Path) -> tuple[str, dict[str, Any], list[OcrAsset]]:
    markdown = ""
    md_files = sorted(output_dir.rglob("*.md"))
    if md_files:
        markdown = md_files[0].read_text(encoding="utf-8", errors="replace")
    json_payload: dict[str, Any] = {}
    json_files = [p for p in sorted(output_dir.rglob("*.json")) if p.name != "raw.json"]
    for json_path in json_files[:5]:
        try:
            json_payload[str(json_path.relative_to(output_dir))] = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            json_payload[str(json_path.relative_to(output_dir))] = json_path.read_text(encoding="utf-8", errors="replace")[:4000]
    assets = []
    for asset_path in sorted(output_dir.rglob("*")):
        if asset_path.is_file() and asset_path.suffix.lower() in IMAGE_EXTENSIONS:
            assets.append(
                OcrAsset(
                    type="figure",
                    path=str(asset_path.relative_to(output_dir)),
                    page=None,
                    bbox=None,
                    caption=None,
                )
            )
    return markdown, json_payload, assets


def metadata_from_markdown(markdown: str, page_count: int = 0, assets: list[OcrAsset] | None = None) -> OcrMetadata:
    latex = analyze_latex(markdown)
    return OcrMetadata(
        page_count=page_count,
        detected_language="vi" if any(ch in markdown for ch in "ăâđêôơưĂÂĐÊÔƠƯ") else None,
        has_math=latex.formula_count > 0,
        has_tables="| ---" in markdown or "|---" in markdown,
        has_figures=bool(assets),
    )


def persist_markdown(markdown: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "output.md").write_text(markdown or "", encoding="utf-8")


def persist_result(result: OcrResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    persist_markdown(result.markdown, output_dir)
    (output_dir / "raw.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def result_from_command(engine: str, markdown: str, raw: dict[str, Any], latency_ms: int, output_dir: Path) -> OcrResult:
    assets = [OcrAsset(**asset) if isinstance(asset, dict) else asset for asset in raw.get("assets", [])]
    if not assets:
        _, _, assets = discover_outputs(output_dir)
    result = OcrResult(
        engine=engine,
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


def result_from_native_pdf(
    engine: str,
    native: dict[str, Any],
    latency_ms: int,
    output_dir: Path,
) -> OcrResult:
    markdown = str(native.get("text") or "")
    raw = {
        "engine": engine,
        "mode": "text",
        "method": native.get("method") or "native-pdf-text",
        "page_count": native.get("page_count") or 0,
        "source": "native_pdf",
    }
    result = OcrResult(
        engine=engine,
        status="success",
        error=None,
        latency_ms=latency_ms,
        markdown=markdown,
        raw=raw,
        assets=[],
        metadata=metadata_from_markdown(markdown, page_count=int(native.get("page_count") or 0), assets=[]),
    )
    persist_result(result, output_dir)
    return result


def normalize_text_markdown(text: str) -> str:
    raw = text or ""
    if not raw.strip():
        return ""
    return raw if any(token in raw for token in ["#", "|", "$", "- "]) else raw.strip() + "\n"
