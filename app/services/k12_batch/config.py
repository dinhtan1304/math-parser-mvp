"""Configuration model for the K12 batch CLI.

The CLI reads a YAML file (default: ``config/ocr_k12_batch.yaml``)
into a pydantic ``PipelineConfig``. CLI flags can override individual
fields at runtime; everything else falls back to YAML defaults.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MinerUConfig(BaseModel):
    method: str = "ocr"           # ocr | txt | auto
    backend: str = "pipeline"     # pipeline | hybrid
    lang: str = "latin"
    timeout_seconds: int = 600


class PaddleVLConfig(BaseModel):
    device: str = "auto"          # auto | cpu | gpu
    enable_formula_retry: bool = True


class FormulaValidatorConfig(BaseModel):
    backend: str = "latex2mathml"
    max_retries_per_doc: int = 50
    max_workers: int = 4


class RegexSegmenterConfig(BaseModel):
    fallback_numeric: bool = True
    min_options_for_tn_hint: int = 3
    essay_long_min_chars: int = 300


class GeminiFinalizerConfig(BaseModel):
    enabled: bool = True
    max_concurrent_chunks: int = 2
    chunk_target_tokens: int = 4000
    inject_subject_hint: bool = True
    override_answer_from_extractor: bool = True
    extractor_confidence_threshold: float = 0.85


class OutputConfig(BaseModel):
    image_dirname: str = "images"
    raw_filename: str = "raw.md"
    json_filename: str = "questions.json"
    report_filename: str = "report.json"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    per_question: bool = True


class PipelineConfig(BaseModel):
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    paddle_vl: PaddleVLConfig = Field(default_factory=PaddleVLConfig)
    formula_validator: FormulaValidatorConfig = Field(default_factory=FormulaValidatorConfig)
    regex_segmenter: RegexSegmenterConfig = Field(default_factory=RegexSegmenterConfig)
    gemini_finalizer: GeminiFinalizerConfig = Field(default_factory=GeminiFinalizerConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(yaml_path: Path | str | None) -> PipelineConfig:
    """Load a ``PipelineConfig`` from YAML; fall back to defaults if absent.

    Missing files are not an error — the defaults baked into the
    pydantic model are usable as-is. We log a warning so the operator
    notices the file wasn't found.
    """
    if not yaml_path:
        return PipelineConfig()
    path = Path(yaml_path)
    if not path.exists():
        logger.warning("config: %s not found, using defaults", path)
        return PipelineConfig()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML at {path}: {exc}") from exc
    if raw is None:
        return PipelineConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a mapping, got {type(raw).__name__}")
    return PipelineConfig.model_validate(raw)
