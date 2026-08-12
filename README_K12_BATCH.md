# K12 Batch OCR Pipeline

Standalone CLI for batch-extracting questions from Vietnamese K12 exam PDFs.

```
input PDF
   │
   ▼ MinerU OCR (markdown + content_list.json + figures)
   ▼ formula_validator (latex2mathml → crop bbox → PaddleOCR-VL retry)
   ▼ regex_segmenter (split into Câu/Bài blocks + strip ĐÁP ÁN section)
   ▼ gemini_finalizer (AIQuestionParser → GeneratedQuestion JSON)
   ▼ AnswerExtractor (table / inline / solution tier → override answer)
   ▼ schema validation
   ▼
output/{stem}/
   ├── raw.md
   ├── questions.json   (List[GeneratedQuestion])
   ├── images/*.png
   └── report.json
output/batch_report.json
```

## Install

The pipeline reuses the existing project venv:

```powershell
cd e:\Edu_Smart_App\math-parser-mvp
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The two heavy OCR engines are intentionally not in `requirements.txt`
(they bring multi-GB models). Install them once, per the existing
benchmark script:

```powershell
pip install mineru                     # MinerU 3.x CLI
pip install paddleocr paddlepaddle     # PaddleOCR-VL (fallback for invalid LaTeX)
```

Models auto-download on first run. Verify the binaries are visible:

```powershell
mineru --help
python -c "from paddleocr import PaddleOCRVL; print('ok')"
```

The CLI also expects `GOOGLE_API_KEY` for the Gemini finalize step.
Set it in `math-parser-mvp/.env` (same one the FastAPI app reads).
For offline experiments, pass `--skip-gemini` to bypass the API call.

## Usage

```powershell
# Single file
python scripts/ocr_k12_batch.py `
  --input "data/de_thi_toan_lop_10.pdf" `
  --output "out" `
  --subject toan --grade 10

# Whole directory
python scripts/ocr_k12_batch.py `
  --input "data/de_thi/" `
  --output "out" `
  --subject ly --grade 11 `
  --gpu-id 0 --limit 50

# Offline debug (no Gemini, no PaddleOCR-VL retries — just MinerU + regex)
python scripts/ocr_k12_batch.py `
  --input "data/de_thi/sample.pdf" `
  --output "out_debug" `
  --subject toan --grade 10 `
  --skip-gemini --debug
```

### Flags

| Flag | Purpose |
|---|---|
| `--input PATH` | Directory of PDFs (recursed) or single PDF. |
| `--output PATH` | Per-file artifacts go under `output/<stem>/`. |
| `--subject {toan,ly,hoa,sinh,van,su,dia,anh}` | Subject hint for Gemini. |
| `--grade {6..12}` | Grade hint for Gemini. |
| `--config PATH` | YAML config (default `config/ocr_k12_batch.yaml`). |
| `--gpu-id N` | Sets `CUDA_VISIBLE_DEVICES=N`. |
| `--force` | Re-process files even if `questions.json` already exists. |
| `--limit N` | Process at most N PDFs from the input dir. |
| `--dry-run` | List the files that would be processed, then exit. |
| `--skip-gemini` | Skip the Gemini finalize step (offline / cost-free). |
| `--debug` | Keep MinerU scratch dir + dump `_debug/{content_list,regex_blocks}.json`. |
| `--log-level {DEBUG,INFO,WARNING,ERROR}` | Override YAML log level. |

### Output schema

`questions.json` is a list of [`GeneratedQuestion`](app/schemas/generator.py)
dicts — the same shape the FastAPI `/save-as-exam` endpoint accepts:

```json
[
  {
    "question": "Câu 1. Tính ...",
    "type": "TN",
    "subject_code": "toan",
    "grade": 10,
    "topic": "Đại số",
    "difficulty": "TH",
    "chapter": "Hằng đẳng thức",
    "lesson_title": "",
    "answer": "B",
    "solution_steps": ["Bước 1: ...", "Bước 2: ..."]
  }
]
```

`report.json` summarizes per-file quality: block count, numbering gaps,
formula retry stats, AnswerExtractor coverage and confidence,
schema-validation errors.

`batch_report.json` (at `output/`) aggregates all files in the run.

## Layout

| Path | Purpose |
|---|---|
| [scripts/ocr_k12_batch.py](scripts/ocr_k12_batch.py) | CLI entry (argparse). |
| [app/services/k12_batch/pipeline.py](app/services/k12_batch/pipeline.py) | 5-step orchestrator. |
| [app/services/k12_batch/formula_validator.py](app/services/k12_batch/formula_validator.py) | latex2mathml + structural checks + PaddleOCR-VL retry on crops. |
| [app/services/k12_batch/regex_segmenter.py](app/services/k12_batch/regex_segmenter.py) | Splits markdown into `QuestionBlock` candidates. |
| [app/services/k12_batch/gemini_finalizer.py](app/services/k12_batch/gemini_finalizer.py) | Thin wrapper around `AIQuestionParser`. |
| [app/services/k12_batch/output_writer.py](app/services/k12_batch/output_writer.py) | Merges answers, validates schema, writes artifacts. |
| [app/services/k12_batch/config.py](app/services/k12_batch/config.py) | Pydantic model for the YAML config. |
| [config/ocr_k12_batch.yaml](config/ocr_k12_batch.yaml) | Default settings (override via flags). |
| [tests/k12_batch/](tests/k12_batch/) | 57 unit tests covering segmenter, validator, finalizer. |

## Testing

```powershell
# Unit tests for the K12 batch modules
venv\Scripts\python.exe -m pytest tests/k12_batch/ -v

# Existing pipeline tests should be unaffected
venv\Scripts\python.exe -m pytest tests/ -v --ignore=tests/k12_batch
```

## Tuning

* **Formula retries are bounded** — `formula_validator.max_retries_per_doc`
  caps how many crops we'll OCR per file (default 50). Increase only if
  you're seeing a lot of `no_bbox`/`crop_failed` skips in `report.json`.
* **Gemini concurrency** — `gemini_finalizer.max_concurrent_chunks`
  defaults to 2 to stay well under Gemini's per-minute quota. Bump to 3-4
  if you're on a higher tier.
* **Answer override** — `override_answer_from_extractor: true` lets the
  regex-based `AnswerExtractor` overwrite Gemini's answer when its
  confidence is ≥ `extractor_confidence_threshold`. Set to `false` to
  trust Gemini exclusively.
* **GPU selection** — `--gpu-id 1` sets `CUDA_VISIBLE_DEVICES=1` before
  MinerU and PaddleOCR-VL initialize. On a single-GPU box this is fine to
  leave unset.

## Out of scope (deferred)

* Page-range subsetting (`--page-range` is accepted but currently logs a
  warning and processes the whole PDF).
* Two-column layout detection (relies on MinerU's reading-order).
* Auto-detecting subject/grade from filename (the CLI flags are
  required).
* Pipeline parallelism across files (sequential — MinerU + Gemini are
  the bottlenecks; see plan for the producer/consumer phase-2 design).
