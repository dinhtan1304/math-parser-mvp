"""End-to-end orchestrator for the K12 batch pipeline.

Per file:

  1. Run MinerU on the PDF and pull back markdown +
     ``content_list.json`` (the bbox map per block).
  2. Validate every LaTeX block; for invalid blocks with a known
     bbox, crop the page and re-OCR with PaddleOCR-VL.
  3. Regex-segment the (now formula-fixed) markdown into question
     blocks and a separate answer section.
  4. Hand the blocks to ``AIQuestionParser`` (Gemini) for final
     structuring into :class:`GeneratedQuestion` dicts.
  5. Run ``AnswerExtractor`` on the original markdown + the answer
     section, override answers when the extractor is confident,
     validate against the pydantic schema, and write outputs.

The batch driver iterates files sequentially because both MinerU
(GPU) and Gemini (rate-limited network) are shared bottlenecks.
A single ``AIQuestionParser`` instance is reused for the whole
batch so its system-prompt cache stays warm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.benchmark.engines.mineru_engine import MinerUEngine
from app.services.k12_batch.config import PipelineConfig
from app.services.k12_batch.formula_validator import revalidate_markdown
from app.services.k12_batch.gemini_finalizer import finalize
from app.services.k12_batch.output_writer import (
    apply_answers,
    run_answer_extractor,
    validate_against_schema,
    write_outputs,
)
from app.services.k12_batch.regex_segmenter import (
    QuestionBlock,
    detect_numbering_gaps,
    segment,
)

if TYPE_CHECKING:
    from app.services.ai_parser import AIQuestionParser

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    file: str
    success: bool
    question_count: int = 0
    block_count: int = 0
    answer_coverage: float = 0.0
    formula_retries: int = 0
    formula_recovered: int = 0
    answer_extractor_confidence: float = 0.0
    elapsed_seconds: float = 0.0
    error: str | None = None
    output_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _slug(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name)
    return safe.strip("_") or "file"


def _read_content_list(out_dir: Path) -> list[dict[str, Any]]:
    """Find and parse MinerU's content_list.json output."""
    candidates = list(out_dir.rglob("*content_list.json"))
    if not candidates:
        return []
    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("read_content_list: cannot parse %s: %s", candidates[0], exc)
        return []


def _collect_referenced_images(out_dir: Path, markdown: str) -> list[Path]:
    """Resolve image paths mentioned in the markdown.

    MinerU writes images as relative paths next to its markdown; the
    paths inside the markdown may or may not have a directory prefix.
    We resolve by name against the OCR output directory.
    """
    import re

    refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)
    candidates: list[Path] = []
    for ref in refs:
        ref = ref.strip()
        if ref.startswith(("http://", "https://", "data:")):
            continue
        as_path = (out_dir / ref).resolve() if not Path(ref).is_absolute() else Path(ref)
        if as_path.exists():
            candidates.append(as_path)
            continue
        # Fall back to a recursive name lookup — MinerU sometimes
        # writes paths like "images/foo.png" but the actual asset
        # ends up under a nested layout dir.
        matches = list(out_dir.rglob(Path(ref).name))
        if matches:
            candidates.append(matches[0])
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _ensure_gpu_env(gpu_id: int | None) -> None:
    if gpu_id is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)


def _build_blocks_offline_fallback(blocks: list[QuestionBlock], subject_code: str, grade: int) -> list[dict[str, Any]]:
    """Convert regex blocks → GeneratedQuestion-ish dicts without Gemini.

    Only used for ``--skip-gemini`` / Gemini disabled. The output is
    intentionally minimal: question text is the raw block, type is the
    hint, no answer or solution_steps. Caller can still merge in
    answers via the extractor.
    """
    type_map = {
        "tn_4choice": "TN",
        "true_false_candidate": "true_false",
        "no_options": "short_answer",
        "essay_long": "TL",
        "unknown": "TN",
    }
    out: list[dict[str, Any]] = []
    for block in blocks:
        out.append({
            "question": block.raw_block.strip(),
            "type": type_map.get(block.hint_type, "TN"),
            "subject_code": subject_code,
            "grade": grade,
            "topic": "",
            "difficulty": "TH",
            "chapter": "",
            "lesson_title": "",
            "answer": "",
            "solution_steps": [],
        })
    return out


async def process_one(
    pdf_path: Path,
    out_root: Path,
    subject: str,
    grade: int,
    config: PipelineConfig,
    ai_parser: "AIQuestionParser | None",
    *,
    force: bool = False,
    debug: bool = False,
    skip_gemini: bool = False,
) -> ProcessResult:
    """Run the full pipeline on a single PDF.

    Exceptions are caught and recorded on the returned ``ProcessResult``
    — a single bad file never aborts a batch.
    """
    start = time.perf_counter()
    file_dir = out_root / _slug(pdf_path.stem)
    questions_path = file_dir / config.output.json_filename
    if questions_path.exists() and not force:
        logger.info("skip (already done): %s", pdf_path.name)
        try:
            existing = json.loads(questions_path.read_text(encoding="utf-8"))
            qcount = len(existing) if isinstance(existing, list) else 0
        except Exception:
            qcount = 0
        return ProcessResult(
            file=str(pdf_path),
            success=True,
            question_count=qcount,
            elapsed_seconds=time.perf_counter() - start,
            output_dir=str(file_dir),
        )
    file_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Step 1: MinerU OCR ───────────────────────────────
        mineru_dir = file_dir / "_mineru"
        mineru_dir.mkdir(parents=True, exist_ok=True)
        # Set env vars the existing engine reads. setdefault preserves any
        # value the operator already set in their shell.
        os.environ.setdefault("MINERU_METHOD", config.mineru.method)
        if config.mineru.backend:
            os.environ.setdefault("MINERU_BACKEND", config.mineru.backend)
        # The MinerU benchmark wrapper trims ~30s off the endpoint timeout
        # for safety, so we pass it as the "endpoint" knob; the subprocess
        # gets `timeout_seconds - 30` to actually complete.
        os.environ.setdefault("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", str(config.mineru.timeout_seconds))
        os.environ.setdefault("MINERU_BENCHMARK_TIMEOUT_SECONDS", str(config.mineru.timeout_seconds))
        engine = MinerUEngine()
        if not engine.is_available():
            raise RuntimeError("MinerU not available — install `mineru` CLI or set MINERU_CMD")
        logger.info("[%s] step 1/5: MinerU OCR", pdf_path.name)
        ocr_result = engine.run(pdf_path, mineru_dir)
        if ocr_result.status != "success":
            raise RuntimeError(f"MinerU failed: {ocr_result.error}")
        markdown = ocr_result.markdown or ""
        content_list = _read_content_list(mineru_dir)
        logger.info("  → markdown=%d chars, content_list=%d items", len(markdown), len(content_list))

        # ── Step 2: formula validation + retry ────────────────
        logger.info("[%s] step 2/5: formula validator", pdf_path.name)
        if config.paddle_vl.enable_formula_retry:
            markdown, formula_stats = revalidate_markdown(
                markdown=markdown,
                content_list=content_list,
                pdf_path=pdf_path,
                workdir=file_dir / "_debug" if debug else file_dir / "_tmp",
                max_workers=config.formula_validator.max_workers,
                max_retries=config.formula_validator.max_retries_per_doc,
            )
        else:
            from app.services.k12_batch.formula_validator import FormulaRetryStats
            formula_stats = FormulaRetryStats()

        # ── Step 3: regex segmentation ────────────────────────
        logger.info("[%s] step 3/5: regex segmenter", pdf_path.name)
        blocks, answer_section = segment(
            markdown,
            fallback_numeric=config.regex_segmenter.fallback_numeric,
            min_options_for_tn=config.regex_segmenter.min_options_for_tn_hint,
            essay_long_min_chars=config.regex_segmenter.essay_long_min_chars,
        )
        if not blocks:
            raise RuntimeError("regex_segmenter produced 0 blocks — markdown unrecognized")
        gaps = detect_numbering_gaps(blocks)

        if config.logging.per_question:
            for block in blocks:
                logger.debug("  block #%d (%s) hint=%s opts=%s images=%d",
                             block.number, block.marker_kind, block.hint_type,
                             list(block.hint_options), len(block.image_refs))

        # ── Step 4: Gemini finalize (or offline fallback) ─────
        if skip_gemini or not config.gemini_finalizer.enabled or ai_parser is None:
            logger.info("[%s] step 4/5: SKIP Gemini (offline fallback)", pdf_path.name)
            parsed = _build_blocks_offline_fallback(blocks, subject, grade)
        else:
            logger.info("[%s] step 4/5: Gemini finalize (%d blocks)", pdf_path.name, len(blocks))
            parsed = await finalize(
                blocks=blocks,
                subject_code=subject,
                grade=grade,
                ai_parser=ai_parser,
                inject_hint=config.gemini_finalizer.inject_subject_hint,
            )
            if not parsed:
                logger.warning("[%s] Gemini returned 0 questions; falling back to offline blocks", pdf_path.name)
                parsed = _build_blocks_offline_fallback(blocks, subject, grade)

        # ── Step 5: answer extractor + validate + write ───────
        logger.info("[%s] step 5/5: answer extractor + write outputs", pdf_path.name)
        answer_source_text = markdown + ("\n\n" + answer_section if answer_section else "")
        answer_map = run_answer_extractor(answer_source_text, parsed)
        threshold = (
            config.gemini_finalizer.extractor_confidence_threshold
            if config.gemini_finalizer.override_answer_from_extractor
            else 1.01  # disable override by raising threshold above max
        )
        parsed, answer_stats = apply_answers(parsed, answer_map, confidence_threshold=threshold)

        valid_questions, validation_errors = validate_against_schema(parsed)

        # Move/copy referenced images into the per-file images/ dir.
        images = _collect_referenced_images(mineru_dir, markdown)

        report = {
            "source_file": str(pdf_path),
            "subject": subject,
            "grade": grade,
            "ocr_engine": "mineru",
            "ocr_latency_ms": ocr_result.latency_ms,
            "block_count": len(blocks),
            "numbering_gaps": gaps,
            "formula_stats": formula_stats.to_dict(),
            "answer_stats": answer_stats,
            "validation_errors": validation_errors,
            "schema_valid_count": len(valid_questions),
            "schema_invalid_count": len(validation_errors),
            "skip_gemini": bool(skip_gemini or not config.gemini_finalizer.enabled or ai_parser is None),
        }

        debug_payload: dict[str, Any] | None = None
        if debug:
            debug_payload = {
                "mineru_content_list": content_list,
                "regex_blocks": [
                    {
                        "number": b.number,
                        "hint_type": b.hint_type,
                        "hint_options": b.hint_options,
                        "image_refs": b.image_refs,
                        "source_offset": b.source_offset,
                        "marker_kind": b.marker_kind,
                    }
                    for b in blocks
                ],
            }

        write_outputs(
            out_dir=file_dir,
            raw_md=markdown,
            questions=valid_questions,
            images=images,
            report=report,
            image_dirname=config.output.image_dirname,
            raw_filename=config.output.raw_filename,
            json_filename=config.output.json_filename,
            report_filename=config.output.report_filename,
            debug_payload=debug_payload,
        )

        # Clean up MinerU scratch dir unless debugging.
        if not debug:
            import shutil
            shutil.rmtree(mineru_dir, ignore_errors=True)
            shutil.rmtree(file_dir / "_tmp", ignore_errors=True)

        elapsed = time.perf_counter() - start
        result = ProcessResult(
            file=str(pdf_path),
            success=True,
            question_count=len(valid_questions),
            block_count=len(blocks),
            answer_coverage=answer_stats.get("coverage", 0.0),
            formula_retries=formula_stats.retried,
            formula_recovered=formula_stats.recovered,
            answer_extractor_confidence=answer_map.confidence,
            elapsed_seconds=round(elapsed, 2),
            output_dir=str(file_dir),
        )
        logger.info(
            "[%s] ✓ %d questions, %.0f%% answers, %.1fs",
            pdf_path.name, result.question_count, result.answer_coverage * 100, result.elapsed_seconds,
        )
        return result

    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.exception("[%s] ✗ failed: %s", pdf_path.name, exc)
        return ProcessResult(
            file=str(pdf_path),
            success=False,
            elapsed_seconds=round(elapsed, 2),
            error=f"{type(exc).__name__}: {exc}",
            output_dir=str(file_dir),
        )


def _iter_pdfs(input_path: Path, limit: int | None) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() == ".pdf" else []
    pdfs = sorted(input_path.rglob("*.pdf"))
    if limit:
        pdfs = pdfs[:limit]
    return pdfs


async def process_batch(
    input_path: Path,
    out_root: Path,
    subject: str,
    grade: int,
    config: PipelineConfig,
    *,
    gpu_id: int | None = None,
    force: bool = False,
    debug: bool = False,
    skip_gemini: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    progress_cb: "callable | None" = None,
) -> list[ProcessResult]:
    """Process every PDF under ``input_path`` (or a single file).

    The whole batch shares one ``AIQuestionParser`` so Gemini's
    system-prompt cache stays warm across files. MinerU and Gemini
    are inherently serial here (single GPU, rate-limited API); the
    plan documents pipeline-parallel as a phase-2 optimization.
    """
    _ensure_gpu_env(gpu_id)
    pdfs = _iter_pdfs(input_path, limit)
    if not pdfs:
        logger.warning("no PDFs found under %s", input_path)
        return []
    out_root.mkdir(parents=True, exist_ok=True)
    logger.info("batch: %d PDFs → %s (subject=%s, grade=%d)", len(pdfs), out_root, subject, grade)

    if dry_run:
        for pdf in pdfs:
            logger.info("  would process: %s", pdf)
        return [ProcessResult(file=str(p), success=True, error="dry-run") for p in pdfs]

    ai_parser: "AIQuestionParser | None" = None
    if config.gemini_finalizer.enabled and not skip_gemini:
        try:
            from app.services.ai_parser import AIQuestionParser
            ai_parser = AIQuestionParser(
                max_concurrency=config.gemini_finalizer.max_concurrent_chunks,
            )
        except Exception as exc:
            logger.error("AIQuestionParser init failed (%s) — falling back to offline mode", exc)
            ai_parser = None

    results: list[ProcessResult] = []
    for idx, pdf in enumerate(pdfs, start=1):
        logger.info("─── [%d/%d] %s ───", idx, len(pdfs), pdf.name)
        try:
            res = await process_one(
                pdf_path=pdf,
                out_root=out_root,
                subject=subject,
                grade=grade,
                config=config,
                ai_parser=ai_parser,
                force=force,
                debug=debug,
                skip_gemini=skip_gemini,
            )
        except Exception as exc:
            # process_one already handles its own exceptions, but be
            # defensive — we never want one bad file to stop the batch.
            tb = traceback.format_exc()
            logger.error("process_one raised unexpectedly:\n%s", tb)
            res = ProcessResult(file=str(pdf), success=False, error=f"{type(exc).__name__}: {exc}")
        results.append(res)
        if progress_cb:
            try:
                progress_cb(idx, len(pdfs), res)
            except Exception:
                logger.exception("progress_cb failed")

    # Aggregate batch report.
    batch_report = {
        "input": str(input_path),
        "output": str(out_root),
        "subject": subject,
        "grade": grade,
        "total_files": len(results),
        "succeeded": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success),
        "total_questions": sum(r.question_count for r in results),
        "avg_answer_coverage": round(
            sum(r.answer_coverage for r in results) / max(len(results), 1), 3
        ),
        "results": [r.to_dict() for r in results],
    }
    (out_root / "batch_report.json").write_text(
        json.dumps(batch_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "batch done: %d/%d succeeded, %d questions, avg coverage %.0f%%",
        batch_report["succeeded"], batch_report["total_files"],
        batch_report["total_questions"], batch_report["avg_answer_coverage"] * 100,
    )
    return results


def run_batch_sync(*args: Any, **kwargs: Any) -> list[ProcessResult]:
    """Convenience sync wrapper for the CLI."""
    return asyncio.run(process_batch(*args, **kwargs))
