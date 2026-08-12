# CLAUDE.md — math-parser-mvp

Nền tảng phân tích & sinh đề K12 tiếng Việt. Đầu vào là đề thi (PDF/ảnh, tiếng
Việt + công thức toán); đầu ra là câu hỏi có cấu trúc trong ngân hàng câu hỏi,
dùng để phân tích và ráp đề theo ma trận đặc tả (Thông tư 22).

> File này mô tả **kiến trúc đích** và các **quy tắc bắt buộc**. Đọc kỹ phần
> "Quy tắc bắt buộc" trước khi sửa bất kỳ thứ gì trong pipeline OCR / parse /
> truy xuất — chúng tồn tại để tránh các anti-pattern đã từng gây tốn kém và
> thiếu tin cậy.

---

## Triết lý cốt lõi (áp cho mọi quyết định)

Xếp tầng theo độ khó, đắt dần:

1. **Deterministic** ở đâu làm được thì làm (bóc text layer, lọc SQL, parse
   regex, verify bằng code).
2. **Model nhỏ chuyên dụng** cho task hẹp (PaddleOCR-VL cho OCR, classifier rẻ
   cho phân loại).
3. **LLM đắt (Gemini)** CHỈ ở biên: ảnh hỏng nặng, block parse lỗi, sinh câu mới.
4. **Mọi thay đổi OCR/model phải đo qua eval harness** trước khi merge.

Khẩu quyết: *thử rẻ trước, chỉ leo thang khi output không đạt ngưỡng.*

---

## Pipeline (các tầng và ranh giới)

```
PDF/ảnh
  → [1] Routing OCR        → Markdown + LaTeX
  → [2] Parse cấu trúc      → JSON câu hỏi (thiếu difficulty/topic_id)
  → [3] Classify ngữ nghĩa  → điền difficulty + topic_id
  → [4] Lưu ngân hàng       → bảng questions (Postgres + pgvector)
  → [5] Truy xuất           → ráp đề (SQL) / tìm tương tự (vector)
```

**[1] Routing OCR.** Định tuyến theo CHẤT LƯỢNG nguồn, không chỉ theo môn:
- PDF có text layer thật → bóc bằng PyMuPDF, **KHÔNG OCR** ($0, gần như đúng tuyệt đối).
- Scan sạch / ảnh méo → PaddleOCR-VL (self-host, $0). Bản 1.5+ robust với ảnh chụp.
- Handwritten / output dưới ngưỡng chất lượng → leo thang Gemini Vision (fallback, tốn phí).
Mỗi tầng tự kiểm tra chất lượng output; kém thì leo thang, không mặc định dùng tầng đắt.

**[2] Parse cấu trúc** (`exam_parser.py`). Regex/luật, KHÔNG LLM. Tách câu, tách
phương án, nhận diện form, ghép đáp án, gắn cờ `valid`. Block `valid=false` mới
đẩy sang fallback (LLM rẻ parse riêng / người duyệt).

**[3] Classify ngữ nghĩa.** Gán `difficulty` (NB/TH/VD/VDC) và `topic_id`.
Đây là phần KHÔNG suy ra được từ cấu trúc. Dùng **classifier rẻ** (embed
`searchable_text` → đầu phân loại, fine-tune từ đề đã gán nhãn) + heuristic
từ khóa cho topic. KHÔNG dùng Gemini parse cả trang cho việc này.

**[4]/[5] Ngân hàng & truy xuất.** Xem `question_bank_schema.sql`.

---

## QUY TẮC BẮT BUỘC (đừng vi phạm)

- **KHÔNG OCR file born-digital.** Có text layer → bóc trực tiếp.
- **KHÔNG dùng LLM cho parse cấu trúc.** Chỉ dùng cho block `valid=false`.
- **KHÔNG dùng Gemini cho phân loại difficulty/topic.** Đó là việc của classifier.
- **Một câu hỏi = một record.** KHÔNG chunk cắt ngang câu hay công thức `$...$`.
- **KHÔNG embed LaTeX thô.** Embed `searchable_text` (mô tả tiếng Việt sạch).
- **Truy xuất: lọc metadata TRƯỚC, vector SAU.** Chỗ nào `WHERE` đủ thì đừng RAG.
  Ráp đề theo ma trận là SQL thuần, không đụng embedding.
- **MASK công thức trước mọi regex cấu trúc**, rồi unmask. Tránh `A.`/`Câu`
  nằm trong công thức làm vỡ bộ tách.
- **Mọi thay đổi engine/model OCR phải chạy eval harness** và so theo TẦNG
  (`doc_type`/`content`), không chỉ nhìn CER trung bình.
- **Chuẩn hóa NFC** cho mọi text tiếng Việt khi so sánh/băm.
- **Verify đáp án toán bằng code** (sympy), không nhờ model lớn hơn.

---

## Data model (tóm tắt — chi tiết ở `question_bank_schema.sql`)

- `topics`: taxonomy theo chương trình GDPT 2018 / TT32. Lọc theo `topic_id`
  luôn ưu tiên hơn match text. Neo `requirement` ("yêu cầu cần đạt").
- `source_documents`: truy vết nguồn + engine OCR + độ tin cậy.
- `questions`: đơn vị nguyên tử. Tách **metadata để lọc**
  (`subject/grade/topic_id/difficulty/form`) khỏi **nội dung** (`stem/choices/
  answer/solution`, giữ LaTeX nguyên bản) và **trường tìm kiếm**
  (`searchable_text` → `embedding vector(1024)`, `ts` tsvector).
  Chống trùng: `content_hash` (tuyệt đối) + ngưỡng similarity ~0.95 (gần trùng).

Mẫu truy xuất (xem comment trong file .sql):
- **Ráp đề theo ma trận** = `WHERE subject/grade/topic_id/difficulty/form` + `ORDER BY usage_count, random()`.
- **Tìm tương tự** = lọc metadata trước, rồi `ORDER BY embedding <=> $1`.

---

## Hợp đồng của parser (`exam_parser.py`)

- Input: Markdown+LaTeX (đầu ra OCR). Output: `list[ParsedQuestion]`.
- Đặt: `number, stem, form, choices, answer, points, has_figure, valid, issues`.
- **KHÔNG đặt** `difficulty`, `topic_id` — đó là tầng [3].
- `valid=true` → vào thẳng ngân hàng. `valid=false` → fallback.
- Đề Việt Nam đa dạng format → khi gặp format mới làm vỡ parse, **thêm/chỉnh
  regex**, đừng thay bằng LLM. Dùng cờ `valid` để phát hiện format lệch sớm.

---

## Bố trí mã & công cụ

- `exam_parser.py` — parser cấu trúc (tầng 2).
- `question_bank_schema.sql` — schema ngân hàng (tầng 4/5).
- `ocr-eval/` — eval harness OCR. **Chạy trước khi đổi model.**
  `python -m ocr_eval.cli run --goldset goldset --engines ...`
- Gold set: `ocr-eval/goldset/` (đọc README ở đó cho giao thức gán nhãn).

## Tech stack

FastAPI · SQLAlchemy async · Python 3.12 · PostgreSQL + pgvector (HNSW) ·
PaddleOCR-VL (self-host, OCR chính) · Gemini Vision (CHỈ fallback) ·
embedding BGE-M3 / multilingual-e5-large (1024-d).

## Trạng thái hiện tại vs đích

- Đang chuyển OCR từ MinerU+Gemini → PaddleOCR-VL; giữ MinerU như baseline so sánh.
- Classifier (tầng 3) và adapter fallback parse là phần đang xây.
- Khi thêm OCR engine: thêm adapter trong `ocr-eval/ocr_eval/adapters.py`,
  KHÔNG sửa `runner.py`.
