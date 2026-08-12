# CLAUDE.md — math-parser-mvp

Backend FastAPI cho nền tảng dạy học K12 tiếng Việt, **phục vụ giáo viên**.
Đầu vào là đề thi (PDF/ảnh, tiếng Việt + công thức toán); đầu ra là câu hỏi có
cấu trúc trong ngân hàng, dùng để ráp đề theo ma trận đặc tả, sinh đề mới, xuất
Word/PDF, và soạn Kế hoạch bài dạy theo CV5512.

> File này mô tả **HIỆN TRẠNG**, không phải kiến trúc mong muốn. Nếu bạn sửa
> code mà nó không còn đúng nữa, sửa file này cùng lúc. Phiên bản trước của
> file này mô tả một kiến trúc đích chưa từng được xây, và đã khiến người đọc
> đi sai suốt nhiều tháng.

---

## Chạy

```bash
# Test (không cần GPU / ML stack)
pip install -r requirements-test.txt
pytest tests/ -q                      # 352 passed, 7 skipped

# Chạy app
pip install -r requirements.txt
uvicorn app.main:app --reload
```

`requirements-test.txt` là tập tối thiểu, **cố ý** không kéo torch/paddleocr/
mineru/docling. Test phụ thuộc OCR đều mock engine hoặc skip.

---

## Triết lý (áp cho mọi quyết định)

Xếp tầng theo độ đắt, rẻ trước:

1. **Deterministic** ở đâu làm được thì làm — bóc text layer, lọc SQL, regex.
2. **Model chuyên dụng** cho task hẹp — PaddleOCR-VL cho OCR.
3. **LLM (Gemini)** ở nơi thật sự cần — parse cấu trúc câu hỏi, sinh câu mới,
   chấm writing.

Khẩu quyết: *thử rẻ trước, chỉ leo thang khi output không đạt ngưỡng.*

---

## Pipeline upload → ngân hàng câu hỏi

```
PDF/ảnh  →  [1] OCR  →  [2] Parse  →  [3] Người duyệt  →  [4] Ngân hàng  →  [5] Truy xuất
```

**[1] OCR** — `services/local_ocr_service.py`, hàm vào: `extract_local_ocr_artifact`

```
pdf_detector.analyze_pdf_for_ocr → có text layer thật?
  ├─ CÓ  → native_pdf.extract_native_pdf_markdown (PyMuPDF, $0, KHÔNG OCR)
  │         └─ assess_native_math_text đạt ngưỡng → DỪNG
  └─ KHÔNG / kém → _run_ocr_cascade
        ├─ primary  = PaddleOCR-VL 1.6   (env OCR_PRIMARY_ENGINE, mặc định paddle-vl)
        │             local GPU hoặc hosted API (env PADDLE_VL_MODE=api)
        └─ fallback = MinerU — CHỈ chạy khi primary rỗng/dưới ngưỡng
                      _fallback_beats_primary() quyết định lấy kết quả nào
```

- **Gemini Vision đã gỡ hoàn toàn.** Tham số `use_vision` còn lại là no-op giữ
  tương thích ngược.
- **Marker đã gỡ khỏi pipeline upload** (rủi ro giấy phép GPL-3.0). `marker_ocr.py`
  còn tồn tại và VẪN NẰM TRÊN ĐƯỜNG PRODUCTION ở vai trò tiện ích
  (`_estimate_page_count`) + chunk-fallback — đừng xóa file này vì tưởng Marker
  đã đi hẳn.
- Kết quả OCR cache theo `file_hash`; đổi logic cascade thì **bump `CACHE_VERSION`**
  trong `local_ocr_service.py` (hiện v16).

**[2] Parse cấu trúc** — hai đường:
- Block-aware (khi `DOCUMENT_SEGMENTATION_ENABLED=1`):
  `document_blocks` → `block_classifier` → `document_structure` → `question_assembler`
- `ai_parser.py` — Gemini parse chunk markdown thành JSON câu hỏi.
  Chunk cực lớn (300K ký tự) là **cố ý**: đề + hướng dẫn chấm phải vào cùng một
  lần gọi thì model mới map được đáp án về đúng `cau_num`.

**[3] Người duyệt** — hai chốt trước khi ghi vào ngân hàng:
- `status=ocr_review` → FE `/upload/ocr/[id]` sửa markdown → reparse
- `QuestionDraft` → FE `/upload/review/[id]` sửa/tách/gộp → commit

**[4] Ngân hàng** — bảng `question` (xem `app/db/models/question.py`).

**[5] Truy xuất** — ráp đề theo ma trận là **SQL thuần** (`WHERE` + `ORDER BY
random()`), không đụng embedding. Vector chỉ dùng cho "tìm câu tương tự" và RAG.

---

## QUY TẮC BẮT BUỘC

- **KHÔNG OCR file born-digital.** Có text layer → bóc trực tiếp bằng PyMuPDF.
- **Một câu hỏi = một record.** KHÔNG chunk cắt ngang câu hay công thức `$...$`.
- **MASK công thức trước mọi regex cấu trúc**, rồi unmask. Tránh `A.`/`Câu`
  nằm trong công thức làm vỡ bộ tách.
- **Truy xuất: lọc metadata TRƯỚC, vector SAU.** Chỗ nào `WHERE` đủ thì đừng RAG.
- **Chuẩn hóa NFC** cho mọi text tiếng Việt khi so sánh/băm.
- **Đổi engine/model OCR phải đo bằng `scripts/ocr_benchmark.py`** trước khi
  merge, so theo tầng (`doc_type`), không chỉ nhìn số trung bình.
- **KHÔNG thêm cột định danh cá nhân học sinh** vào `quizattempt` / `submission`
  (ngày sinh, SĐT, email, địa chỉ, ảnh). Có test tự động chặn:
  `tests/test_student_data_minimal.py`.

---

## Bố trí mã

| Đường dẫn | Vai trò |
|---|---|
| `app/api/` | 26 router. `parser.py` (2561 dòng) là file lớn nhất và rủi ro nhất |
| `app/services/` | Logic nghiệp vụ. Không import ngược lên `app/api/` |
| `app/db/models/` | 26 bảng SQLAlchemy |
| `app/benchmark/` | 9 engine OCR để **đo đạc**. Không phải đường production… |
| `app/services/k12_batch/` | CLI xử lý theo lô |
| `alembic/versions/` | 2 migration: baseline `404cab2e5e0c` + compliance `b1f7c2a94e30` |
| `scripts/ocr_benchmark.py` | Eval harness OCR |

---

## Bẫy đã biết (đọc trước khi sửa)

**Schema có ba nguồn, không đồng bộ.** Models + Alembic + raw SQL lúc boot.
4 bảng runtime KHÔNG nằm trong Alembic vì tạo bằng raw SQL: `question_embedding`,
`question_similarity`, `document_chunk`, `question_fts`. Nên `alembic upgrade head`
trên DB trắng **không dựng đủ schema** — phải boot app một lần.

**`main.py` còn ~30 `ALTER TABLE` legacy** bọc `try/except: pass`, chạy mỗi lần
boot. Đừng thêm mục mới vào đó — thêm cột = sửa model + `alembic revision`.

**`main.py:110` ép mọi role về `teacher` ở mỗi lần boot.** Không tạo được tài
khoản học sinh. Đây là chủ ý (teacher-only pivot), nhưng nó có nghĩa
`classmember` / `submission` / `answerdetail` **không có nguồn dữ liệu mới**.

**`api/ielts_parser.py` import `_publish_progress` + `_background_tasks`
(private, biến trạng thái toàn cục) từ `api/parser.py`.** Coupling chặt nhất
repo, và `ielts_parser` KHÔNG có test. Sửa cơ chế SSE/background task trong
`parser.py` sẽ làm vỡ nó âm thầm.

**Vòng import `pipeline` ↔ `docling_chunker`** — cả hai chiều đều defer trong
hàm nên Python không nổ, nhưng vẫn là vòng thật, đi qua tên private.

**`local_ocr_service` phụ thuộc ngược lên `app/benchmark/`** (3 chỗ) và
`k12_batch.pipeline._read_content_list`. Sửa engine trong `benchmark/` có thể
làm vỡ pipeline upload thật.

**Bài test end-to-end cho `process_file` đang bị skip** (7 test trong
`tests/test_parser_pipeline.py`, lý do: "stale, asserts legacy deferred-bank-save
flow"). Nghĩa là luồng upload trọn vẹn hiện KHÔNG có test nào phủ.

**`latex_to_omml.py` (619 dòng) KHÔNG có test.** Nó quyết định chất lượng công
thức trong file Word xuất ra. `tests/test_latex_utils.py` test module KHÁC
(`app/benchmark/latex_utils.py`).

**`.env.example` thiếu ~39 biến môi trường mà code thực sự đọc**, gồm cả
`OCR_PRIMARY_ENGINE`, `PADDLE_VL_MODE`, `PADDLE_VL_API_KEY`, toàn bộ nhóm
`ASSET_S3_*`, `GEMINI_MODEL`.

---

## Tech stack

FastAPI · SQLAlchemy 2.x async · Python 3.12 · PostgreSQL (Neon) + extension
pgvector · SQLite cho dev local · Redis (tùy chọn, degrade về in-memory) ·
PaddleOCR-VL 1.6 (OCR chính) · MinerU (fallback) · Gemini `gemini-2.5-flash`
(parse + sinh đề + chấm writing) · `text-embedding-004` 768 chiều.

---

## Ngữ cảnh repo

`e:/Edu_Smart_App/` **không phải** git repo. Ba repo độc lập bên trong:
`math-parser-mvp/` (repo này), `mathplay-frontend/`, và trước đây có
`mathplay-mobile/` (app học sinh, đã gỡ 2026-08-12 — còn trên GitHub).

`.github/workflows/` nằm ở thư mục gốc **không được repo nào track**, viết theo
layout monorepo → **CI chưa từng chạy**. Đừng tin là có CI đỡ lưng.
