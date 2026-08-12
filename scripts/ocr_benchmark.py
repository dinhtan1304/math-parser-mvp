"""CLI so sánh OCR engines trên một thư mục tài liệu mẫu.

Bản tối giản (2026-07-10): các module scoring/report/file_classifier cũ đã bị
xoá khi chuyển benchmark sang trang admin `/admin/ocr-benchmark` — script này
từng import chúng và GÃY. Giờ chỉ chạy engine + gom kết quả thô (markdown,
latency, status) ra ``results.csv``/``results.json``; chấm điểm chất lượng dùng
OCR Benchmark Lab trong admin UI.

Ví dụ:
    python scripts/ocr_benchmark.py --input samples/ --output benchmark_out/ \
        --engines mineru,paddle
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmark.engines import ENGINE_CLASSES
from app.benchmark.types import OcrResult


DEFAULT_EXTENSIONS = "pdf,png,jpg,jpeg,docx,pptx,xlsx"
INSTALL_HINTS = {
    "markitdown": "pip install markitdown",
    "marker": "pip install marker-pdf  # CPU only; tune MARKER_SUB_RETRY_PAGES=3 + MARKER_MAX_WORKERS=4 to halve runtime",
    "mineru": "pip install mineru  # CLI: `mineru`. Optional ~/mineru.json. Models auto-download on first run (~5GB).  # or set MINERU_CMD",
    "chandra": "Set CHANDRA_CMD='chandra-ocr --input {input} --output {output}'",
    "olmocr": "Install olmOCR and/or set OLMOCR_CMD with {input}/{output}",
    "paddle": "pip install paddleocr paddlepaddle  # or set PADDLE_CMD",
    "mathpix": "Set MATHPIX_APP_ID and MATHPIX_APP_KEY",
}

RESULT_FIELDS = ["file_id", "file_path", "engine", "status", "error", "latency_ms", "markdown_chars"]


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    input_dir = args.input
    if not input_dir.exists():
        print(f"Input path does not exist: {input_dir}", file=sys.stderr)
        return 2

    output_dir = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    files = discover_files(input_dir, parse_extensions(args.include_ext), args.limit)
    engines = build_engines(args.engines)
    if not engines:
        print("No valid engines selected.", file=sys.stderr)
        return 2

    rows: list[dict] = []
    for file_path in files:
        file_id = file_path.stem
        file_dir = output_dir / "by_file" / file_id
        for engine in engines:
            engine_dir = file_dir / engine.name
            result = load_cached_result(engine_dir) if not args.force else None
            if result is None:
                result = engine.run(file_path, engine_dir)
            rows.append({
                "file_id": file_id,
                "file_path": str(file_path),
                "engine": engine.name,
                "status": result.status,
                "error": result.error or "",
                "latency_ms": result.latency_ms,
                "markdown_chars": len(result.markdown or ""),
            })

    rows.sort(key=lambda r: (r["file_id"], r["engine"]))
    write_results_csv(output_dir / "results.csv", rows)
    (output_dir / "results.json").write_text(
        json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_install_hints(output_dir, rows)
    print(f"Wrote benchmark results to {output_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OCR engines on K12 document samples (raw results only).")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engines", type=str, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-ext", type=str, default=DEFAULT_EXTENSIONS)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def parse_extensions(value: str) -> set[str]:
    return {"." + item.strip().lower().lstrip(".") for item in value.split(",") if item.strip()}


def discover_files(input_path: Path, extensions: set[str], limit: int | None) -> list[Path]:
    candidates = [input_path] if input_path.is_file() else sorted(path for path in input_path.rglob("*") if path.is_file())
    filtered = [path for path in candidates if path.suffix.lower() in extensions]
    return filtered[:limit] if limit is not None else filtered


def build_engines(value: str):
    engines = []
    for name in [part.strip().lower() for part in value.split(",") if part.strip()]:
        cls = ENGINE_CLASSES.get(name)
        if cls is None:
            logging.warning("Unknown engine %s; skipping", name)
            continue
        engines.append(cls())
    return engines


def load_cached_result(engine_dir: Path) -> OcrResult | None:
    raw_path = engine_dir / "raw.json"
    md_path = engine_dir / "output.md"
    if not raw_path.exists() or not md_path.exists():
        return None
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        if "engine" in payload and "status" in payload:
            result = OcrResult.from_dict(payload)
        else:
            result = OcrResult.from_dict(payload.get("result") or payload)
        result.markdown = md_path.read_text(encoding="utf-8", errors="replace")
        return result
    except Exception:
        return None


def write_results_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_install_hints(output_dir: Path, rows: list[dict]) -> None:
    unavailable = sorted({row["engine"] for row in rows if row["status"] == "skipped"})
    if not unavailable:
        return
    lines = ["# OCR Engine Install Hints", ""]
    for engine in unavailable:
        lines.append(f"- `{engine}`: {INSTALL_HINTS.get(engine, 'Install engine CLI/package or configure command template.')}")
    (output_dir / "install_hints.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
