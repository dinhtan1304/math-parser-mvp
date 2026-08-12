from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmark.engines import ENGINE_CLASSES
from app.benchmark.file_classifier import classify_file
from app.benchmark.scoring import score_result
from app.benchmark.types import BenchmarkMetricRow, FileClassification, OcrResult


GPU_ENV_PROFILE: dict[str, str] = {
    "TORCH_DEVICE": "cuda",
    "MINERU_DEVICE_MODE": "cuda",
    "CUDA_VISIBLE_DEVICES": "0",
    "MARKER_MAX_WORKERS": "1",
    "MARKER_EQUATION_BATCH": "2",
    "DETECTOR_BATCH_SIZE": "2",
    "LAYOUT_BATCH_SIZE": "2",
    "RECOGNITION_BATCH_SIZE": "8",
    "TABLE_REC_BATCH_SIZE": "1",
    "OCR_ERROR_BATCH_SIZE": "2",
    "MARKER_BENCHMARK_DISABLE_OCR": "0",
    "MINERU_METHOD": "ocr",
    "MINERU_BACKEND": "pipeline",
}

DEFAULT_OUTPUT_DIR = ROOT / "benchmark_results_gpu"
DEFAULT_ENGINES = "marker,mineru"
DEFAULT_VARIANTS = "1"


def main() -> int:
    args = parse_args()
    apply_gpu_profile(override=not args.no_override_env)
    if args.timeout is not None:
        configure_timeouts(args.timeout)

    gpu_status = collect_gpu_status()
    if args.require_cuda and not bool(gpu_status.get("torch_cuda_available")):
        print(json.dumps(gpu_status, ensure_ascii=False, indent=2), file=sys.stderr)
        print("CUDA is not available in this Python environment. Run scripts/setup_gpu_venv.ps1 first.", file=sys.stderr)
        return 2

    files = discover_inputs(args.input)
    if not files:
        print(f"No supported input files found: {args.input}", file=sys.stderr)
        return 2

    engines = build_engines(args.engines)
    if not engines:
        print("No valid engines selected.", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "gpu_status.json").write_text(
        json.dumps(gpu_status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    warmup_results: list[dict[str, Any]] = []
    if not args.no_warmup:
        warmup_results = run_warmup(files[0], engines, args.output)
        (args.output / "warmup_results.json").write_text(
            json.dumps(warmup_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    rows: list[BenchmarkMetricRow] = []
    classifications: dict[str, FileClassification] = {}
    for file_path in files:
        rows.extend(run_measured_file(file_path, engines, args.output, classifications, args.variants))

    write_results(args.output / "results.csv", args.output / "results.json", rows, classifications, gpu_status)
    (args.output / "summary_gpu.md").write_text(
        build_summary_markdown(rows, gpu_status=gpu_status, warmup_results=warmup_results),
        encoding="utf-8",
    )
    print(f"Wrote GPU benchmark results to {args.output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Marker vs MinerU with a small-batch CUDA profile.")
    parser.add_argument("--input", type=Path, required=True, help="PDF file or directory containing PDFs.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--engines", type=str, default=DEFAULT_ENGINES)
    parser.add_argument("--timeout", type=int, default=600, help="Per-engine timeout seconds. 0 means unlimited.")
    parser.add_argument(
        "--variants",
        type=str,
        default=DEFAULT_VARIANTS,
        help="Comma-separated page variants: 1,3,full. Default is 1-page smoke benchmark.",
    )
    parser.add_argument("--no-warmup", action="store_true", help="Skip one-page warmup runs.")
    parser.add_argument("--no-require-cuda", dest="require_cuda", action="store_false")
    parser.add_argument("--no-override-env", action="store_true", help="Keep existing env values instead of forcing GPU profile.")
    parser.set_defaults(require_cuda=True)
    return parser.parse_args()


def apply_gpu_profile(*, override: bool) -> None:
    for key, value in GPU_ENV_PROFILE.items():
        if override or not os.getenv(key):
            os.environ[key] = value


def configure_timeouts(timeout_seconds: int) -> None:
    value = "unlimited" if timeout_seconds <= 0 else str(timeout_seconds)
    for key in [
        "OCR_BENCHMARK_PER_ENGINE_TIMEOUT",
        "MARKER_BENCHMARK_PER_ENGINE_TIMEOUT",
        "MINERU_BENCHMARK_PER_ENGINE_TIMEOUT",
        "OCR_BENCHMARK_TIMEOUT_SECONDS",
        "MARKER_BENCHMARK_TIMEOUT_SECONDS",
        "MINERU_BENCHMARK_TIMEOUT_SECONDS",
    ]:
        os.environ[key] = value


def discover_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() == ".pdf" else []
    if not input_path.exists():
        return []
    return sorted(path for path in input_path.rglob("*.pdf") if path.is_file())


def build_engines(value: str):
    engines = []
    for name in [part.strip().lower() for part in value.split(",") if part.strip()]:
        cls = ENGINE_CLASSES.get(name)
        if cls is not None:
            engines.append(cls())
    return engines


def run_warmup(file_path: Path, engines, output_dir: Path) -> list[dict[str, Any]]:
    warmup_dir = output_dir / "warmup"
    warmup_input, warmup_pages = materialize_variant(file_path, "warmup-p1", 1, warmup_dir / "slices")
    results = []
    for engine in engines:
        result = engine.run(warmup_input, warmup_dir / engine.name)
        if result.metadata.page_count == 0:
            result.metadata.page_count = warmup_pages
        results.append(
            {
                "engine": engine.name,
                "status": result.status,
                "latency_ms": result.latency_ms,
                "page_count": warmup_pages,
                "latency_per_page_ms": latency_per_page(result.latency_ms, warmup_pages),
                "error": result.error,
            }
        )
    return results


def run_measured_file(
    file_path: Path,
    engines,
    output_dir: Path,
    classifications: dict[str, FileClassification],
    variants_spec: str,
) -> list[BenchmarkMetricRow]:
    base_classification = classify_file(file_path)
    variants = page_variants(file_path, base_classification.page_count, variants_spec)
    rows: list[BenchmarkMetricRow] = []

    for variant_name, variant_pages in variants:
        variant_input, actual_pages = materialize_variant(
            file_path,
            variant_name,
            variant_pages,
            output_dir / "slices" / base_classification.file_id,
        )
        classification = classify_file(variant_input)
        file_id = f"{base_classification.file_id}-{variant_name}"
        classification.file_id = file_id
        classification.path = str(variant_input)
        classification.page_count = actual_pages
        classifications[file_id] = classification

        for engine in engines:
            engine_dir = output_dir / "by_file" / file_id / engine.name
            result = engine.run(variant_input, engine_dir)
            if result.metadata.page_count == 0:
                result.metadata.page_count = actual_pages
            row = score_result(
                result,
                file_id=file_id,
                file_path=str(variant_input),
                classification=classification,
            )
            rows.append(row)
    return rows


def page_variants(file_path: Path, page_count: int, variants_spec: str = DEFAULT_VARIANTS) -> list[tuple[str, int]]:
    if file_path.suffix.lower() != ".pdf":
        return [("full", max(page_count, 1))]
    pages = max(page_count, 1)
    requested = {part.strip().lower() for part in variants_spec.split(",") if part.strip()}
    variants: list[tuple[str, int]] = []
    if "1" in requested or "p1" in requested:
        variants.append(("p1", 1))
    if ("3" in requested or "p3" in requested) and pages >= 3:
        variants.append(("p3", 3))
    if "full" in requested and pages > 1:
        variants.append(("full", pages))
    return variants or [("p1", 1)]


def materialize_variant(file_path: Path, variant_name: str, page_count: int, slice_dir: Path) -> tuple[Path, int]:
    if file_path.suffix.lower() != ".pdf" or variant_name == "full":
        return file_path, max(page_count, 1)
    slice_dir.mkdir(parents=True, exist_ok=True)
    output_path = slice_dir / f"{file_path.stem}-{variant_name}.pdf"
    try:
        import fitz  # PyMuPDF

        source = fitz.open(str(file_path))
        actual_pages = min(max(page_count, 1), source.page_count)
        target = fitz.open()
        target.insert_pdf(source, from_page=0, to_page=actual_pages - 1)
        target.save(str(output_path))
        target.close()
        source.close()
        return output_path, actual_pages
    except Exception:
        return file_path, max(page_count, 1)


def collect_gpu_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "env_profile": dict(GPU_ENV_PROFILE),
        "python": sys.executable,
    }
    try:
        import torch

        status["torch_version"] = getattr(torch, "__version__", None)
        status["torch_cuda_runtime"] = getattr(torch.version, "cuda", None)
        status["torch_cuda_available"] = bool(torch.cuda.is_available())
        status["torch_device_count"] = int(torch.cuda.device_count())
        if torch.cuda.is_available():
            status["torch_device_name"] = torch.cuda.get_device_name(0)
    except Exception as exc:
        status["torch_error"] = str(exc)
        status["torch_cuda_available"] = False

    try:
        from surya.settings import settings as surya_settings

        status["surya_torch_device_model"] = getattr(surya_settings, "TORCH_DEVICE_MODEL", None)
    except Exception as exc:
        status["surya_error"] = str(exc)

    try:
        from mineru.utils.config_reader import get_device

        status["mineru_device"] = get_device()
    except Exception as exc:
        status["mineru_error"] = str(exc)

    status["nvidia_smi"] = query_nvidia_smi()
    if isinstance(status["nvidia_smi"], dict):
        status.update({f"gpu_{key}": value for key, value in status["nvidia_smi"].items()})
    return status


def query_nvidia_smi() -> dict[str, str] | dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,memory.used,compute_cap",
        "--format=csv,noheader",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except Exception as exc:
        return {"error": str(exc)}
    if completed.returncode != 0:
        return {"error": (completed.stderr or completed.stdout).strip()}
    return parse_nvidia_smi_query(completed.stdout)


def parse_nvidia_smi_query(stdout: str) -> dict[str, str]:
    first_line = next((line for line in stdout.splitlines() if line.strip()), "")
    parts = [part.strip() for part in first_line.split(",")]
    keys = ["name", "driver_version", "memory_total", "memory_used", "compute_cap"]
    return {key: parts[index] for index, key in enumerate(keys) if index < len(parts)}


def latency_per_page(latency_ms: int, page_count: int) -> float | None:
    if page_count <= 0:
        return None
    return round(latency_ms / page_count, 1)


def build_summary_markdown(
    rows: list[BenchmarkMetricRow],
    *,
    gpu_status: dict[str, Any],
    warmup_results: list[dict[str, Any]] | None = None,
) -> str:
    lines = [
        "# GPU OCR Benchmark Summary",
        "",
        f"- GPU: `{gpu_status.get('name') or gpu_status.get('gpu_name') or 'unknown'}`",
        f"- Torch: `{gpu_status.get('torch_version') or 'unknown'}`",
        f"- CUDA available: `{gpu_status.get('torch_cuda_available')}`",
        f"- Surya device: `{gpu_status.get('surya_torch_device_model') or 'unknown'}`",
        f"- MinerU device: `{gpu_status.get('mineru_device') or 'unknown'}`",
        "",
    ]
    if warmup_results:
        lines.extend(["## Warmup", "", "| Engine | Status | Latency ms | Latency/page ms | Error |", "| --- | --- | ---: | ---: | --- |"])
        for result in warmup_results:
            lines.append(
                f"| {result.get('engine')} | {result.get('status')} | {result.get('latency_ms')} | "
                f"{result.get('latency_per_page_ms')} | {result.get('error') or ''} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Measured Runs",
            "",
            "| Engine | Status | Score | Latency ms | Latency/page ms | LaTeX formulas | LaTeX valid | Questions | File variant |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in sorted(rows, key=lambda item: (item.file_id, item.engine)):
        lines.append(
            f"| {row.engine} | {row.status} | {row.final_quality_score:.2f} | {row.latency_ms} | "
            f"{latency_per_page(row.latency_ms, row.page_count)} | {row.formula_count} | "
            f"{row.latex_valid_ratio:.2f} | {row.question_count} | {row.file_id} |"
        )

    lines.extend(
        [
            "",
            "## Decision Rule",
            "",
            "- Marker becomes primary for small STEM scans only if GPU warm latency is at least 2x faster than CPU and quality is clearly higher than MinerU.",
            "- MinerU remains fallback if it is much faster but needs downstream LaTeX/Vietnamese normalization.",
            "- Large text-layer STEM PDFs should stay on native extraction plus Gemini LaTeX normalization unless Marker GPU reaches product latency.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_results(
    csv_path: Path,
    json_path: Path,
    rows: list[BenchmarkMetricRow],
    classifications: dict[str, FileClassification],
    gpu_status: dict[str, Any],
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(BenchmarkMetricRow)]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())

    payload = {
        "gpu_status": gpu_status,
        "rows": [row.to_dict() for row in rows],
        "files": {file_id: classification.to_dict() for file_id, classification in classifications.items()},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
