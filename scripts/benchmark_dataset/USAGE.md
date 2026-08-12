# OCR Benchmark — Hướng dẫn sử dụng

Workflow A/B test các engine OCR cho Edu Smart App. Tích hợp full vào UI `/admin/ocr-benchmark`.

## TL;DR

```
1. Khởi động backend + frontend
2. Mở http://localhost:3000/admin/ocr-benchmark
3. Tab "Dataset" → upload 12-15 file test với category + ground truth
4. Tab "Batch Run" → chọn engines + variants → Start
5. Tab "History" → poll progress, xem matrix kết quả
```

## Yêu cầu

### Engines available (out-of-the-box)
- ✅ `markitdown`, `marker`, `mineru`, `paddle` (PaddleOCR-VL), `olmocr`, `granite-docling`, `dots`
- ⚠️ `mathpix` — cần `MATHPIX_APP_ID` + `MATHPIX_APP_KEY`
- ⚠️ `chandra` — cần `CHANDRA_CMD` env

### Hardware
- **GPU optional**: GTX 1650 4GB đủ cho Marker/Paddle-VL/Granite-Docling. Cần cu128 venv cho CUDA support.
- **CPU only**: Tất cả engine chạy được CPU nhưng chậm hơn 5-20x.
- **VRAM > 8GB**: Cần thiết cho `dots` và `olmocr` GPU mode.

### Setup GPU venv (optional)
```powershell
cd e:\Edu_Smart_App\math-parser-mvp
.\scripts\setup_gpu_venv.ps1
# Sau đó: dùng .\venv-gpu\Scripts\python.exe để chạy backend
```

## Engine matrix

| Engine | License | VRAM | VN | LaTeX | Image | Notes |
|---|---|---|---|---|---|---|
| `marker` | OpenRAIL-M (<$2M rev) | 3-5GB | Limited (Surya) | Excellent (Texify) | Yes + bbox | Current production primary |
| `mineru` | Apache code, AGPL model | 4-8GB (VLM) | 109 langs | Auto LaTeX | Yes | Set `MINERU_BACKEND=vlm` cho VLM mode |
| `paddle` | Apache 2.0 (model+code) | 3-6GB | **Yes (109 langs explicit)** | Dedicated head | Yes via `save_to_markdown` | PaddleOCR-VL 0.9B — recommended for VN |
| `granite-docling` | Apache 2.0 | 1.5GB | Limited (EN primary) | F1 0.968 | Yes via Docling | Compact, fit 4GB GPU |
| `dots` | MIT | 8-12GB | **Yes (in examples)** | Yes | Limited | Multilingual SOTA, MIT license clean |
| `olmocr` | Apache 2.0 | 12GB+ | **No** (non-EN filtered) | Yes | Limited | Skip cho VN-first project |
| `markitdown` | MIT | CPU only | Yes via deps | Limited | No | Digital files, fastest |
| `mathpix` | Commercial | 0 (cloud) | Yes | Industry-best | Yes | API key required |
| `chandra` | Commercial | varies | Yes | Yes | Yes | External subprocess |

## Workflow chi tiết

### 1. Curate dataset (target: 12-15 file)

Mở UI tab **Dataset**. Upload mỗi file với:
- **Category** (6 loại):
  - `vn_scanned` — đề thi scan có dấu tiếng Việt (2 file)
  - `stem_text_layer` — đề Toán/Lý xuất từ Word có text-layer (2 file)
  - `stem_scanned` — đề Toán/Lý scan, math-heavy (2 file)
  - `layout_heavy` — sách Sinh/Địa, bảng + diagram (2 file)
  - `text_heavy` — đề Văn/Sử, nhiều text (2 file)
  - `edge_case` — DOCX, ảnh chụp, multi-column (2-5 file)
- **Subject** + **Grade**
- **Ground truth** (optional nhưng khuyến nghị):
  - Expected questions, expected LaTeX inline min, expected tables, expected figures
  - Has VN diacritics, Is scanned, Notes

File sẽ được copy vào `scripts/benchmark_dataset/files/<file_id>.pdf` (gitignored).

### 2. Run batch

Tab **Batch Run**:
- **Chọn engines**: tick những engine muốn so sánh (khuyến nghị: marker, mineru, paddle, granite-docling, dots — bỏ olmocr nếu là VN-first)
- **Chọn variants**: 3 (median) là sweet spot
- **Chọn files**: tick toàn bộ dataset hoặc subset
- **Start** → batch chạy background

Backend persist state mỗi cell vào `benchmark_results_batch/<batch_id>/state.json`. FE poll mỗi 3s.

### 3. Xem kết quả

Tab **History**:
- List past batches với progress + status
- Click 1 batch → xem 3 view:
  - **Matrix**: grid (file × engine), score / latency mỗi cell
  - **Summary**: aggregate ranking (best per category, best per doc_type)
  - **Cell**: click 1 cell trong matrix để xem markdown output cụ thể

## Env vars

### Engine-specific
```bash
# Marker
MARKER_BENCHMARK_DISABLE_OCR=0      # 1 = không texify, mất LaTeX
MARKER_BENCHMARK_EXTRACT_IMAGES=0   # 1 = save figure files (chậm hơn)
MARKER_BENCHMARK_HIGHRES_DPI=144
MARKER_BENCHMARK_LOWRES_DPI=72

# MinerU
MINERU_BACKEND=vlm                  # vlm | pipeline (default: hybrid)
MINERU_METHOD=ocr                   # ocr | txt | auto (default: ocr)
MINERU_BENCHMARK_ALLOW_NATIVE=0     # 1 = PyMuPDF fast path (mất LaTeX)

# PaddleOCR-VL
PADDLE_USE_VL=1                     # 0 = legacy PaddleOCR (image only)
PADDLE_VL_DEVICE=auto               # auto | cpu | gpu

# Granite-Docling
GRANITE_DOCLING_DEVICE=auto         # auto | cpu | cuda
GRANITE_DOCLING_MODEL=ibm-granite/granite-docling-258M

# dots.ocr
DOTS_OCR_DEVICE=auto                # auto | cpu | cuda
DOTS_OCR_MODEL=rednote-hilab/dots.ocr
DOTS_OCR_MAX_PAGES=10               # cap để tránh OOM
DOTS_OCR_DPI=150                    # PDF → image rasterize DPI

# Common timeouts
OCR_BENCHMARK_PER_ENGINE_TIMEOUT=600     # giây (mỗi engine x mỗi file)
OCR_BENCHMARK_TIMEOUT_SECONDS=540        # subprocess timeout

# Per-engine timeout override (vd MARKER chạy chậm)
MARKER_BENCHMARK_PER_ENGINE_TIMEOUT=1800
```

### Image output mode (per yêu cầu user — Sprint 1)
Hiện tại các engine output theo mặc định của chúng:
- Marker → file refs `![](_page_X.jpeg)`
- PaddleOCR-VL → file refs trong page_dir
- Granite-Docling → file refs hoặc base64 tùy config
- dots.ocr → tùy prompt (đang request file refs)

Để base64 inline: set per-engine env riêng (Marker không support, MinerU/Paddle có thể qua post-process).

## Scoring metrics (Sprint 3)

`final_quality_score` weighted:
- 20% Vietnamese quality (`vietnamese_char_ratio` × 0.7 + `vn_diacritic_accuracy` × 0.3, trừ broken_unicode)
- 25% LaTeX quality (75% valid_ratio + 25% complexity_score)
- 20% max(question_quality, structure_score)
- 15% markdown_cleanliness
- 10% table_asset_quality (incl. image_quality_score)
- 10% readability

5 metric mới (Sprint 3):
- `image_quality_score` (0-100) — so với GT.expected_figure_count, hoặc heuristic nếu không có GT
- `markdown_image_link_count` — số `![](file)` (không base64)
- `markdown_image_base64_count` — số `data:image/...;base64,`
- `latex_complexity_score` (0-100) — ratio LaTeX commands phức tạp (`\frac`, `\int`, `\begin{matrix}`) / total LaTeX
- `vn_diacritic_accuracy` (0-1) — proxy diacritic + ascii letter accuracy, trừ broken unicode

## Troubleshooting

### Engine "skipped" → install missing
```
markitdown: pip install markitdown
marker: pip install marker-pdf
mineru: pip install mineru[vlm]
olmocr: pip install olmocr
paddle: pip install 'paddleocr[doc-parser]' paddlepaddle
granite-docling: pip install 'docling[vlm]'  # đã có sẵn
dots: pip install transformers torch          # đã có sẵn
mathpix: export MATHPIX_APP_ID=... MATHPIX_APP_KEY=...
chandra: export CHANDRA_CMD='chandra-ocr --input {input} --output {output}'
```

### GPU OOM
- Giảm `MARKER_EQUATION_BATCH=2` (default 8) cho Marker
- Set `DOTS_OCR_DEVICE=cpu` cho dots (cần 8-12GB GPU)
- Set `MINERU_BACKEND=pipeline` cho MinerU CPU-pipeline mode
- Bỏ olmocr khỏi selection

### Batch lâu quá
- Giảm `variants` từ 3 → 1 (mất tính ổn định nhưng nhanh hơn 3x)
- Bỏ engine chậm (olmocr, dots CPU mode)
- Giới hạn `DOTS_OCR_MAX_PAGES=5`
- Chia dataset thành nhiều batch nhỏ

## Output files

```
benchmark_results_batch/<batch_id>/
├── state.json              # progress state (poll target)
├── runs/<file_id>/<engine>/variant_<N>/
│   ├── output.md
│   ├── raw.json
│   └── ... (image assets nếu engine save)
├── results.csv             # final aggregated rows (median per cell)
├── results.json
├── summary.md
└── matrix.md               # markdown grid
```

Có thể commit `summary.md` + `matrix.md` vào docs/ làm reference.
