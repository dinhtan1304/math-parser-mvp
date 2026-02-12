# 📚 Math Exam Parser MVP

Upload file đề toán → Gemini AI phân tích → JSON output (LaTeX)

## 🎯 Output Format

```json
[
  {
    "question": "Giải phương trình $x^{2} - 5x + 6 = 0$\nA. $x = 2, x = 3$\nB. $x = -2, x = -3$",
    "type": "TN",
    "topic": "Đại số",
    "difficulty": "TH",
    "solution_steps": [
      "Tính $\\Delta = b^{2} - 4ac = 25 - 24 = 1$",
      "$x = \\frac{5 \\pm 1}{2}$",
      "$x_{1} = 2, x_{2} = 3$"
    ],
    "answer": "A"
  }
]
```

### Question Types
| Code | Mô tả |
|------|-------|
| `TN` | Trắc nghiệm |
| `TL` | Tự luận |
| `Rút gọn biểu thức` | Rút gọn |
| `So sánh` | So sánh |
| `Chứng minh` | Chứng minh |
| `Tính toán` | Tính toán |

### Difficulty Levels
| Code | Mô tả |
|------|-------|
| `NB` | Nhận biết |
| `TH` | Thông hiểu |
| `VD` | Vận dụng |
| `VDC` | Vận dụng cao |

## 🚀 Quick Start

### 1. Setup

```bash
cd math-parser-mvp

python -m venv venv
source venv/bin/activate  # Linux/Mac

pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env:
#   SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
#   GOOGLE_API_KEY=<your Gemini API key from https://aistudio.google.com/apikey>
```

### 3. Run

```bash
python run.py
# Server starts at http://localhost:8000
```

## 📡 API Endpoints

All endpoints require JWT authentication. Register → Login → use Bearer token.

### Auth

```bash
# Register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "secret", "full_name": "User"}'

# Login (returns JWT token)
curl -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=user@example.com&password=secret"

# → {"access_token": "eyJ...", "token_type": "bearer"}
```

### Parse

```bash
TOKEN="eyJ..."

# Upload & parse (async, returns job_id)
curl -X POST "http://localhost:8000/api/v1/parser/parse?speed=balanced&use_vision=false" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@de_thi.pdf"

# Check status
curl "http://localhost:8000/api/v1/parser/status/1" \
  -H "Authorization: Bearer $TOKEN"

# List history (paginated)
curl "http://localhost:8000/api/v1/parser/history?page=1&page_size=20" \
  -H "Authorization: Bearer $TOKEN"

# Delete
curl -X DELETE "http://localhost:8000/api/v1/parser/1" \
  -H "Authorization: Bearer $TOKEN"
```

### Parse Options

| Param | Values | Description |
|-------|--------|-------------|
| `speed` | `fast`, `balanced`, `quality` | Parser speed preset |
| `use_vision` | `true`, `false` | Force Vision mode (recommended for scanned PDFs) |

## 📁 Supported Files

| Format | Extensions | Method |
|--------|------------|--------|
| PDF | .pdf | PyMuPDF text + Vision API fallback |
| Word | .docx, .doc | python-docx / LibreOffice |
| Images | .png, .jpg, .jpeg | Gemini Vision API |
| Text | .txt, .md | Direct read |

## 🐳 Docker

```bash
cp .env.example .env
# Fill in GOOGLE_API_KEY and SECRET_KEY in .env

docker-compose up -d
# → http://localhost:8000
```

## 🏗️ Project Structure

```
app/
├── api/            # Endpoints (auth, parser)
├── core/           # Config, security
├── db/             # SQLAlchemy models, session
├── schemas/        # Pydantic schemas
├── services/       # AI parser, file handler
└── templates/      # Jinja2 HTML
```

## ⚙️ Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `SECRET_KEY` | JWT signing key | ✅ |
| `GOOGLE_API_KEY` | Gemini API key | ✅ |
| `DATABASE_URL` | Database connection | No (default: SQLite) |
| `ENV` | `development` or `production` | No (default: production) |
| `PORT` | Server port | No (default: 8000) |

## 📄 License

MIT