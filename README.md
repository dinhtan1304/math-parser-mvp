# 📚 Math Exam Parser MVP

Upload file đề toán → AI phân tích → JSON output

## 🎯 Output Format

```json
[
  {
    "question": "Giải phương trình x² - 5x + 6 = 0\nA. x = 2, x = 3\nB. x = -2, x = -3\nC. x = 2, x = -3\nD. x = -2, x = 3",
    "type": "multiple_choice",
    "topic": "Đại số",
    "difficulty": "medium",
    "solution_steps": [
      "Bước 1: Tính delta = b² - 4ac = 25 - 24 = 1",
      "Bước 2: x = (5 ± 1) / 2",
      "Bước 3: x₁ = 2, x₂ = 3"
    ],
    "answer": "A"
  }
]
```

## 🚀 Quick Start

### 1. Clone & Setup

```bash
cd math-parser-mvp

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

### 2. Install System Dependencies

**Ubuntu/Debian:**
```bash
# Tesseract OCR (for image/scanned PDF)
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-vie

# Poppler (for PDF)
sudo apt-get install poppler-utils
```

**MacOS:**
```bash
brew install tesseract tesseract-lang poppler
```

**Windows:**
- Download Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
- Add to PATH

### 3. Configure API Key

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 4. Run Server

```bash
python main.py
# or
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Server will start at: http://localhost:8000

## 📡 API Endpoints

### Upload & Parse (Async)

```bash
# Upload file - returns job_id
curl -X POST "http://localhost:8000/api/parse" \
  -F "file=@de_thi.pdf"

# Response:
# {"job_id": "abc123", "status": "pending", "message": "..."}

# Check status
curl "http://localhost:8000/api/status/abc123"

# Response when done:
# {
#   "job_id": "abc123",
#   "status": "completed",
#   "progress": 100,
#   "result": [{"question": "...", ...}]
# }
```

### Upload & Parse (Sync)

```bash
# For small files - wait for result
curl -X POST "http://localhost:8000/api/parse-sync" \
  -F "file=@de_thi.pdf"

# Response:
# {
#   "filename": "de_thi.pdf",
#   "total_questions": 25,
#   "questions": [{"question": "...", ...}]
# }
```

### Other Endpoints

```bash
# List all jobs
curl "http://localhost:8000/api/jobs"

# Delete a job
curl -X DELETE "http://localhost:8000/api/jobs/abc123"

# Health check
curl "http://localhost:8000/health"
```

## 📁 Supported File Types

| Format | Extension | Method |
|--------|-----------|--------|
| PDF | .pdf | Text extraction + OCR fallback |
| Word | .docx, .doc | python-docx |
| Images | .png, .jpg, .jpeg | Tesseract OCR |
| Text | .txt, .md | Direct read |

## 🔧 Configuration

Environment variables in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Claude API key | Required |
| `ANTHROPIC_MODEL` | Model to use | claude-sonnet-4-20250514 |

## 📊 Question Types

| Type | Description |
|------|-------------|
| `multiple_choice` | Trắc nghiệm A, B, C, D |
| `essay` | Tự luận |
| `calculation` | Tính toán |
| `fill_blank` | Điền khuyết |
| `true_false` | Đúng/Sai |

## 🎓 Topics (Auto-detected)

- Đại số
- Hình học
- Giải tích
- Lượng giác
- Xác suất thống kê
- Số học
- Tổ hợp

## ⚡ Performance Tips

1. **Batch Processing**: Upload nhiều file nhỏ tốt hơn 1 file lớn
2. **Clear Text**: File text-based PDF nhanh hơn scanned PDF
3. **Image Quality**: Ảnh rõ nét cho OCR chính xác hơn

## 🐛 Troubleshooting

### "Could not extract text from file"
- Check file không bị corrupted
- Đảm bảo file có nội dung text (không phải ảnh)
- Với scanned PDF/image: cài Tesseract OCR

### "API Error"
- Check ANTHROPIC_API_KEY trong .env
- Check API quota/billing

### OCR không chính xác
- Tăng độ phân giải ảnh
- Đảm bảo tesseract-ocr-vie đã cài

## 📝 Example Usage with Python

```python
import httpx

# Async upload
async def parse_exam(file_path: str):
    async with httpx.AsyncClient() as client:
        # Upload
        with open(file_path, 'rb') as f:
            response = await client.post(
                "http://localhost:8000/api/parse",
                files={"file": f}
            )
        job_id = response.json()["job_id"]
        
        # Poll for result
        while True:
            status = await client.get(f"http://localhost:8000/api/status/{job_id}")
            data = status.json()
            
            if data["status"] == "completed":
                return data["result"]
            elif data["status"] == "failed":
                raise Exception(data["error"])
            
            await asyncio.sleep(1)

# Usage
import asyncio
questions = asyncio.run(parse_exam("de_thi_toan_10.pdf"))
print(f"Found {len(questions)} questions")
```

## 📄 License

MIT