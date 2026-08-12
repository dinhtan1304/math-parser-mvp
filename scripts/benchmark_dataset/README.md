# Benchmark Dataset

Thư mục chứa file test cho OCR engine A/B benchmark. Tích hợp với UI `/admin/ocr-benchmark` của project.

## Cấu trúc

```
benchmark_dataset/
├── MANIFEST.json        # Metadata + ground truth annotations
├── README.md            # File này
├── files/               # PDF/DOCX/PNG test files (gitignored)
│   ├── vn_scanned_<hash>.pdf
│   ├── stem_text_<hash>.pdf
│   └── ...
└── ground_truth/        # Optional: hand-annotated markdown reference (gitignored)
    └── <file_id>.expected.md
```

## Categories (6)

| Code | Label | Mục tiêu test |
|---|---|---|
| `vn_scanned` | Vietnamese scanned PDF | OCR diacritics, broken unicode |
| `stem_text_layer` | STEM text-layer PDF | LaTeX recovery from flattened math |
| `stem_scanned` | STEM scanned | Formula OCR accuracy |
| `layout_heavy` | Layout-heavy | Table + diagram + multi-column |
| `text_heavy` | Text-heavy non-STEM | Speed on simple text |
| `edge_case` | Edge cases | DOCX, photo, low quality |

Target: **2 file/category × 6 category = 12 file** (nâng lên 15 nếu có thêm edge case).

## Cách thêm file vào dataset

### Cách 1: Upload qua UI (khuyến nghị)
1. Mở `http://localhost:3000/admin/ocr-benchmark`
2. Tab **"Dataset"** (sẽ thêm trong Sprint 1.4) → kéo thả file
3. Chọn category + điền ground truth metadata
4. Click "Add to dataset" → file copy vào `files/`, MANIFEST update

### Cách 2: Manual
1. Copy file PDF/DOCX/PNG vào `files/`
2. Thêm entry vào `MANIFEST.json` theo schema `_file_template`
3. Compute SHA-256: `(Get-FileHash files/your-file.pdf -Algorithm SHA256).Hash`
4. Anonymize file nếu cần (strip author metadata, redact PII)

## Privacy & Git

- `files/*` và `ground_truth/*` **gitignored** — chứa data thực tế, không commit
- Chỉ commit `MANIFEST.json` (metadata), `README.md`, `*.expected.md` (nếu đã anonymize)
- Trước khi commit metadata: verify không có tên trường/học sinh trong filename

## Sử dụng cho benchmark

Sau Sprint 1.3 (backend extension):

```bash
# CLI batch run
python scripts/ocr_benchmark.py \
  --input scripts/benchmark_dataset/files \
  --engines marker,mineru,paddle,olmocr \
  --output benchmark_results_batch_$(date +%Y%m%d)

# Hoặc qua UI /admin/ocr-benchmark → tab "Run Dataset"
```
