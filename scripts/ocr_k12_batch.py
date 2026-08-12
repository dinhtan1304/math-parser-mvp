"""CLI entry point for the K12 batch OCR pipeline.

Usage::

    python scripts/ocr_k12_batch.py \\
        --input  data/de_thi/ \\
        --output out/ \\
        --subject toan \\
        --grade 10 \\
        --config config/ocr_k12_batch.yaml

See ``app/services/k12_batch/pipeline.py`` for orchestration details.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SUBJECTS = ("toan", "ly", "hoa", "sinh", "van", "su", "dia", "anh")
GRADES = tuple(range(6, 13))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ocr_k12_batch",
        description="Batch OCR for Vietnamese K12 exam PDFs (MinerU + PaddleOCR-VL + Gemini).",
    )
    p.add_argument("--input", type=Path, required=True, help="Directory of PDFs or a single PDF file")
    p.add_argument("--output", type=Path, required=True, help="Output root directory")
    p.add_argument("--subject", choices=SUBJECTS, required=True, help="Exam subject code")
    p.add_argument("--grade", type=int, choices=GRADES, required=True, help="School grade (6-12)")
    p.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "ocr_k12_batch.yaml",
        help="YAML config path (default: config/ocr_k12_batch.yaml)",
    )
    p.add_argument("--gpu-id", type=int, default=None, help="CUDA_VISIBLE_DEVICES override")
    p.add_argument("--force", action="store_true", help="Re-process files even if output already exists")
    p.add_argument("--limit", type=int, default=None, help="Max files to process from input directory")
    p.add_argument(
        "--page-range",
        default=None,
        help="(Reserved) page range to OCR — not yet implemented; see plan",
    )
    p.add_argument("--dry-run", action="store_true", help="List files that would be processed, then exit")
    p.add_argument(
        "--skip-gemini",
        action="store_true",
        help="Skip Gemini finalize — emit raw regex blocks as questions (debug / offline)",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Retain MinerU scratch dir + dump _debug/{content_list,regex_blocks}.json",
    )
    p.add_argument(
        "--log-level",
        default=None,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Override log level from config",
    )
    return p.parse_args(argv)


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _emit_progress(idx: int, total: int, result) -> None:
    """Single-line progress hook printed to stderr.

    Kept ASCII so it works on Windows consoles that aren't configured
    for UTF-8.
    """
    tag = "OK" if result.success else "FAIL"
    name = Path(result.file).name
    print(
        f"[{idx}/{total}] {tag} {name} — {result.question_count} q, "
        f"{result.answer_coverage:.0%} ans, {result.elapsed_seconds:.1f}s",
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args(argv)

    # Import lazily so --help works even if heavy deps are missing.
    from app.services.k12_batch.config import load_config
    from app.services.k12_batch.pipeline import run_batch_sync

    config = load_config(args.config)
    configure_logging(args.log_level or config.logging.level)
    logger = logging.getLogger("ocr_k12_batch")

    if args.page_range:
        logger.warning("--page-range is reserved but not yet implemented; processing full PDF")

    if not args.input.exists():
        logger.error("input path does not exist: %s", args.input)
        return 2

    if args.gpu_id is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_id)
        logger.info("CUDA_VISIBLE_DEVICES=%s", args.gpu_id)

    if args.skip_gemini:
        logger.info("--skip-gemini set: Gemini finalize will be bypassed")

    results = run_batch_sync(
        input_path=args.input,
        out_root=args.output,
        subject=args.subject,
        grade=args.grade,
        config=config,
        gpu_id=args.gpu_id,
        force=args.force,
        debug=args.debug,
        skip_gemini=args.skip_gemini,
        limit=args.limit,
        dry_run=args.dry_run,
        progress_cb=_emit_progress,
    )

    failed = [r for r in results if not r.success]
    if failed:
        logger.warning("%d file(s) failed:", len(failed))
        for r in failed[:10]:
            logger.warning("  %s — %s", Path(r.file).name, r.error)
        return 1
    if not results:
        logger.warning("no PDFs processed")
        return 1

    # Echo the batch report path for downstream tooling.
    report_path = args.output / "batch_report.json"
    if report_path.exists():
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            logger.info(
                "summary: %d files, %d questions, avg coverage %.0f%% — %s",
                payload.get("total_files", 0),
                payload.get("total_questions", 0),
                (payload.get("avg_answer_coverage") or 0) * 100,
                report_path,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
