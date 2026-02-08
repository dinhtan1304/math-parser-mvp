"""
Math Exam Parser MVP - OPTIMIZED
Upload file đề toán → AI phân tích → JSON output
"""

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uuid
import os
import asyncio
import time
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from file_handler import FileHandler
from ai_parser import create_fast_parser, create_balanced_parser, create_quality_parser

# ==================== CONFIG ====================

PORT = int(os.getenv("PORT", 5000))
API_BASE = os.getenv("API_BASE", "")  # Empty = same origin
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ==================== APP ====================

app = FastAPI(
    title="Math Exam Parser API",
    description="Upload đề toán và phân tích thành JSON",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Try to use Jinja2 templates, fallback to embedded HTML
USE_TEMPLATES = False
templates = None

try:
    from fastapi.templating import Jinja2Templates
    if os.path.exists("templates") and os.path.exists("templates/index.html"):
        templates = Jinja2Templates(directory="templates")
        USE_TEMPLATES = True
        print("✅ Using templates/index.html")
    else:
        print("⚠️ templates/index.html not found, using embedded HTML")
except ImportError:
    print("⚠️ Jinja2 not installed, using embedded HTML")

# Initialize services
file_handler = FileHandler()

# Job tracking
jobs: Dict[str, Dict] = {}


# ==================== SCHEMAS ====================

class Question(BaseModel):
    question: str
    type: str = "TL"
    topic: str = "Toán học"
    difficulty: str = "TH"
    solution_steps: List[str] = []
    answer: str = ""


class ParseResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    result: Optional[List[Dict]] = None
    error: Optional[str] = None
    filename: Optional[str] = None
    processing_time: Optional[float] = None


# ==================== EMBEDDED HTML TEMPLATE ====================

def get_html(api_base: str = "") -> str:
    """Generate HTML with API_BASE injected"""
    return f'''<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Math Exam Parser</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', Tahoma, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ color: white; text-align: center; margin-bottom: 30px; font-size: 2.5rem; text-shadow: 2px 2px 4px rgba(0,0,0,0.2); }}
        .upload-section {{ background: white; border-radius: 20px; padding: 40px; box-shadow: 0 20px 60px rgba(0,0,0,0.2); margin-bottom: 30px; }}
        .upload-area {{ border: 3px dashed #667eea; border-radius: 15px; padding: 60px; text-align: center; cursor: pointer; transition: all 0.3s; background: #f8f9ff; }}
        .upload-area:hover {{ border-color: #764ba2; background: #f0f2ff; }}
        .upload-area.dragover {{ border-color: #764ba2; background: #e8ebff; transform: scale(1.02); }}
        .upload-icon {{ font-size: 4rem; margin-bottom: 20px; }}
        .upload-text {{ color: #666; font-size: 1.2rem; }}
        .file-types {{ color: #999; font-size: 0.9rem; margin-top: 10px; }}
        #fileInput {{ display: none; }}
        .btn {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 15px 40px; font-size: 1.1rem; border-radius: 30px; cursor: pointer; transition: all 0.3s; margin-top: 20px; }}
        .btn:hover {{ transform: translateY(-2px); box-shadow: 0 10px 30px rgba(102, 126, 234, 0.4); }}
        .btn:disabled {{ opacity: 0.6; cursor: not-allowed; transform: none; }}
        .progress-section {{ display: none; background: white; border-radius: 20px; padding: 30px; margin-bottom: 30px; box-shadow: 0 20px 60px rgba(0,0,0,0.2); }}
        .progress-bar {{ height: 20px; background: #e9ecef; border-radius: 10px; overflow: hidden; margin: 20px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 10px; transition: width 0.3s; width: 0%; }}
        .progress-text {{ text-align: center; color: #666; font-size: 1.1rem; }}
        .results-section {{ display: none; background: white; border-radius: 20px; padding: 30px; box-shadow: 0 20px 60px rgba(0,0,0,0.2); }}
        .results-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding-bottom: 20px; border-bottom: 2px solid #eee; flex-wrap: wrap; gap: 10px; }}
        .results-count {{ font-size: 1.5rem; color: #333; }}
        .header-buttons {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .question-card {{ background: #f8f9ff; border-radius: 15px; padding: 25px; margin-bottom: 20px; border-left: 5px solid #667eea; }}
        .question-header {{ display: flex; justify-content: space-between; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }}
        .question-number {{ font-weight: bold; color: #667eea; font-size: 1.2rem; }}
        .question-badges {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .badge {{ padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 500; }}
        .badge-type {{ background: #e3f2fd; color: #1976d2; }}
        .badge-topic {{ background: #f3e5f5; color: #7b1fa2; }}
        .badge-difficulty {{ background: #fff3e0; color: #f57c00; }}
        .question-text {{ color: #333; line-height: 2; margin-bottom: 15px; font-size: 1.05rem; }}
        .answer-section {{ background: #e8f5e9; padding: 15px 20px; border-radius: 10px; margin-top: 15px; }}
        .answer-label {{ font-weight: bold; color: #388e3c; margin-bottom: 8px; }}
        .answer-text {{ color: #2e7d32; font-size: 1.05rem; line-height: 1.8; }}
        .solution-section {{ background: #fff8e1; padding: 15px 20px; border-radius: 10px; margin-top: 10px; }}
        .solution-label {{ font-weight: bold; color: #f57c00; margin-bottom: 10px; }}
        .solution-steps {{ list-style: none; padding: 0; }}
        .solution-steps li {{ padding: 10px 0; border-bottom: 1px dashed #ffe0b2; color: #555; line-height: 1.8; }}
        .solution-steps li:last-child {{ border-bottom: none; }}
        .json-toggle, .download-btn {{ background: #f5f5f5; border: none; padding: 10px 20px; border-radius: 20px; cursor: pointer; font-size: 0.9rem; color: #666; transition: all 0.2s; }}
        .json-toggle:hover, .download-btn:hover {{ background: #e0e0e0; }}
        .download-btn {{ background: #4caf50; color: white; }}
        .download-btn:hover {{ background: #43a047; }}
        .json-output {{ display: none; background: #1e1e1e; color: #d4d4d4; padding: 20px; border-radius: 10px; overflow-x: auto; font-family: monospace; font-size: 0.85rem; margin-top: 20px; max-height: 400px; overflow-y: auto; }}
        .filename {{ color: #999; font-size: 0.9rem; margin-top: 10px; }}
        .api-status {{ position: fixed; bottom: 20px; right: 20px; background: rgba(0,0,0,0.7); color: white; padding: 8px 15px; border-radius: 20px; font-size: 0.8rem; }}
        @media (max-width: 768px) {{ h1 {{ font-size: 1.8rem; }} .upload-area {{ padding: 30px; }} .upload-icon {{ font-size: 3rem; }} .question-card {{ padding: 15px; }} }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📚 Math Exam Parser</h1>
        <div class="upload-section">
            <div class="upload-area" id="uploadArea">
                <div class="upload-icon">📄</div>
                <div class="upload-text">Kéo thả file vào đây hoặc click để chọn</div>
                <div class="file-types">Hỗ trợ: PDF, DOCX, PNG, JPG, TXT</div>
                <input type="file" id="fileInput" accept=".pdf,.docx,.doc,.png,.jpg,.jpeg,.txt,.md">
            </div>
            <div style="text-align: center;">
                <button class="btn" id="uploadBtn" disabled>🚀 Phân tích đề</button>
            </div>
            <div class="filename" id="selectedFile"></div>
        </div>
        <div class="progress-section" id="progressSection">
            <div class="progress-text" id="progressText">Đang xử lý...</div>
            <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
        </div>
        <div class="results-section" id="resultsSection">
            <div class="results-header">
                <div class="results-count" id="resultsCount">0 câu hỏi</div>
                <div class="header-buttons">
                    <button class="download-btn" id="downloadBtn">💾 Tải JSON</button>
                    <button class="json-toggle" id="jsonToggle">📋 Xem JSON</button>
                </div>
            </div>
            <div class="json-output" id="jsonOutput"></div>
            <div id="questionsContainer"></div>
        </div>
    </div>
    <div class="api-status" id="apiStatus">🔗 Checking API...</div>
    <script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
    <script>
        // API_BASE from environment variable (injected by server)
        const API_BASE = '{api_base}';
        console.log('🔗 API_BASE:', API_BASE || '(same origin)');
        
        let currentFile = null, currentResults = null;
        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const selectedFile = document.getElementById('selectedFile');
        const progressSection = document.getElementById('progressSection');
        const progressText = document.getElementById('progressText');
        const progressFill = document.getElementById('progressFill');
        const resultsSection = document.getElementById('resultsSection');
        const resultsCount = document.getElementById('resultsCount');
        const questionsContainer = document.getElementById('questionsContainer');
        const jsonToggle = document.getElementById('jsonToggle');
        const jsonOutput = document.getElementById('jsonOutput');
        const downloadBtn = document.getElementById('downloadBtn');
        const apiStatus = document.getElementById('apiStatus');
        
        (async function() {{
            try {{
                const res = await fetch(API_BASE + '/health');
                if (res.ok) {{ const d = await res.json(); apiStatus.textContent = '✅ API v' + d.version; apiStatus.style.background = 'rgba(76,175,80,0.9)'; }}
                else throw new Error();
            }} catch(e) {{ apiStatus.textContent = '❌ API Error'; apiStatus.style.background = 'rgba(244,67,54,0.9)'; }}
            setTimeout(() => apiStatus.style.display = 'none', 5000);
        }})();
        
        uploadArea.addEventListener('dragover', e => {{ e.preventDefault(); uploadArea.classList.add('dragover'); }});
        uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
        uploadArea.addEventListener('drop', e => {{ e.preventDefault(); uploadArea.classList.remove('dragover'); if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]); }});
        uploadArea.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {{ if (fileInput.files.length) handleFile(fileInput.files[0]); }});
        
        function handleFile(file) {{ currentFile = file; selectedFile.textContent = '📎 ' + file.name + ' (' + formatSize(file.size) + ')'; uploadBtn.disabled = false; }}
        function formatSize(b) {{ if (b < 1024) return b + ' B'; if (b < 1048576) return (b/1024).toFixed(1) + ' KB'; return (b/1048576).toFixed(1) + ' MB'; }}
        
        uploadBtn.addEventListener('click', async () => {{
            if (!currentFile) return;
            uploadBtn.disabled = true;
            progressSection.style.display = 'block';
            resultsSection.style.display = 'none';
            try {{
                const formData = new FormData();
                formData.append('file', currentFile);
                progressText.textContent = 'Đang upload...';
                progressFill.style.width = '10%';
                const res = await fetch(API_BASE + '/api/parse', {{ method: 'POST', body: formData }});
                if (!res.ok) {{ const err = await res.json().catch(() => ({{}})); throw new Error(err.detail || 'Upload failed'); }}
                const {{ job_id }} = await res.json();
                await pollStatus(job_id);
            }} catch (e) {{ progressSection.style.display = 'none'; alert('Lỗi: ' + e.message); uploadBtn.disabled = false; }}
        }});
        
        async function pollStatus(jobId) {{
            while (true) {{
                const res = await fetch(API_BASE + '/api/status/' + jobId);
                const data = await res.json();
                progressFill.style.width = (data.progress || 0) + '%';
                if (data.status === 'extracting') progressText.textContent = 'Đang trích xuất... ' + (data.progress||0) + '%';
                else if (data.status === 'AI parsing') progressText.textContent = 'AI đang phân tích... ' + (data.progress||0) + '%';
                else if (data.status === 'completed') {{ progressSection.style.display = 'none'; displayResults(data.result || []); uploadBtn.disabled = false; return; }}
                else if (data.status === 'failed') throw new Error(data.error || 'Failed');
                else progressText.textContent = data.status + '... ' + (data.progress||0) + '%';
                await new Promise(r => setTimeout(r, 1500));
            }}
        }}
        
        function displayResults(questions) {{
            currentResults = questions;
            resultsSection.style.display = 'block';
            resultsCount.textContent = '📝 ' + questions.length + ' câu hỏi';
            jsonOutput.textContent = JSON.stringify(questions, null, 2);
            questionsContainer.innerHTML = questions.map((q, i) => createCard(q, i)).join('');
            if (typeof renderMathInElement === 'function') renderMathInElement(questionsContainer, {{ delimiters: [{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}}], throwOnError: false }});
        }}
        
        function createCard(q, i) {{
            return '<div class="question-card"><div class="question-header"><span class="question-number">Câu '+(i+1)+'</span><div class="question-badges"><span class="badge badge-type">'+(q.type||'TL')+'</span><span class="badge badge-topic">'+(q.topic||'Toán')+'</span><span class="badge badge-difficulty">'+(q.difficulty||'TH')+'</span></div></div><div class="question-text">'+escapeHtml(q.question||'')+'</div>'+(q.answer?'<div class="answer-section"><div class="answer-label">✅ Đáp án:</div><div class="answer-text">'+escapeHtml(q.answer)+'</div></div>':'')+(q.solution_steps&&q.solution_steps.length?'<div class="solution-section"><div class="solution-label">📖 Lời giải:</div><ul class="solution-steps">'+q.solution_steps.map(s=>'<li>'+escapeHtml(s)+'</li>').join('')+'</ul></div>':'')+'</div>';
        }}
        
        function escapeHtml(t) {{ return t ? t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>') : ''; }}
        
        jsonToggle.addEventListener('click', () => {{ const v = jsonOutput.style.display === 'block'; jsonOutput.style.display = v ? 'none' : 'block'; jsonToggle.textContent = v ? '📋 Xem JSON' : '📋 Ẩn JSON'; }});
        downloadBtn.addEventListener('click', () => {{ if (!currentResults) return; const b = new Blob([JSON.stringify(currentResults,null,2)],{{type:'application/json'}}); const u = URL.createObjectURL(b); const a = document.createElement('a'); a.href = u; a.download = 'math_questions_'+Date.now()+'.json'; a.click(); URL.revokeObjectURL(u); }});
    </script>
</body>
</html>'''


# ==================== FRONTEND ====================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Serve frontend - use template if available, otherwise embedded HTML"""
    if USE_TEMPLATES and templates:
        try:
            return templates.TemplateResponse("index.html", {"request": request, "api_base": API_BASE})
        except Exception as e:
            print(f"⚠️ Template error: {e}, using embedded HTML")
    
    return HTMLResponse(content=get_html(API_BASE))


# ==================== API ENDPOINTS ====================

@app.post("/api/parse", response_model=ParseResponse)
async def parse_file(
    file: UploadFile = File(...),
    speed: str = Query("balanced", description="fast, balanced, quality"),
    use_vision: bool = Query(False, description="Use Vision API for complex math")
):
    """Upload file đề toán để phân tích."""
    allowed_extensions = {'.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg', '.txt', '.md'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"File type not supported. Allowed: {', '.join(allowed_extensions)}"
        )
    
    job_id = str(uuid.uuid4())[:8]
    file_path = os.path.join(UPLOAD_DIR, f"{job_id}_{file.filename}")
    
    try:
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        print(f"📁 Saved file: {file_path} ({len(content):,} bytes)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "result": None,
        "error": None,
        "filename": file.filename,
        "file_path": file_path,
        "created_at": datetime.now().isoformat(),
        "speed_mode": speed,
        "use_vision": use_vision,
        "start_time": time.time(),
        "processing_time": None
    }
    
    # AUTO-VISION: Always use Vision for PDF (text extraction breaks math formulas)
    effective_vision = use_vision
    if file_ext == '.pdf' and not use_vision:
        effective_vision = True
        print(f"📌 Auto-enabling Vision mode for PDF file: {file.filename}")
    
    asyncio.create_task(process_file(job_id, speed, effective_vision))

    return ParseResponse(
        job_id=job_id,
        status="pending",
        message=f"File '{file.filename}' queued ({speed} mode)"
    )


async def process_file(job_id: str, speed: str = "balanced", use_vision: bool = False):
    """Process file with auto-fallback to Vision mode"""
    job = jobs.get(job_id)
    if not job:
        return

    # Select parser
    if speed == "quality":
        parser = create_quality_parser()
    elif speed == "fast":
        parser = create_fast_parser()
    else:
        parser = create_balanced_parser()

    try:
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["progress"] = 10

        # ============================================================
        # Step 1: Extract text (with auto-fallback to Vision)
        # ============================================================
        jobs[job_id]["progress"] = 20
        jobs[job_id]["status"] = "extracting"

        extracted = await file_handler.extract_text(job["file_path"], use_vision=use_vision)
        extracted_text = extracted.get("text", "")
        images = extracted.get("images", [])

        # AUTO-FALLBACK: Nếu text extraction thất bại → tự chuyển sang Vision
        if not use_vision and not extracted_text.strip():
            print("⚠️ Text extraction failed/empty → Auto-switching to Vision mode...")
            jobs[job_id]["status"] = "extracting (auto-vision)"
            
            try:
                extracted = await file_handler.extract_text(job["file_path"], use_vision=True)
                images = extracted.get("images", [])
                use_vision = True  # Đánh dấu đã chuyển sang Vision
                print(f"✅ Vision mode: got {len(images)} page images")
            except Exception as vision_err:
                print(f"❌ Vision fallback also failed: {vision_err}")
                raise ValueError(
                    f"Could not extract content from file. "
                    f"Text extraction returned empty, and Vision mode failed: {vision_err}"
                )

        # AUTO-FALLBACK: Nếu text quality kém (toán bị vỡ) → chuyển Vision
        if not use_vision and extracted_text.strip():
            if _is_math_text_poor_quality(extracted_text):
                print("⚠️ Text quality poor (broken math formulas) → Auto-switching to Vision...")
                jobs[job_id]["status"] = "extracting (auto-vision, poor text)"
                
                try:
                    extracted = await file_handler.extract_text(job["file_path"], use_vision=True)
                    images = extracted.get("images", [])
                    use_vision = True
                    print(f"✅ Vision mode: got {len(images)} page images")
                except Exception:
                    # Vision failed, use poor text anyway
                    print("⚠️ Vision fallback failed, using text extraction anyway")

        mode = "Vision" if use_vision else "Text"
        print(f"\n🚀 Processing job {job_id} with {mode} mode ({speed} speed)")

        # ============================================================
        # Step 2: Parse with AI
        # ============================================================
        def update_progress(current, total):
            pct = 30 + int((current / total) * 60)
            jobs[job_id]["progress"] = pct

        jobs[job_id]["progress"] = 30
        jobs[job_id]["status"] = f"AI parsing ({mode})"

        if use_vision and images:
            # Vision mode: parse from images
            print(f"🖼️ Processing {len(images)} page images with Gemini Vision")
            questions = await parser.parse_images(images, progress_callback=update_progress)
        elif use_vision and not images:
            # Vision was requested but no images generated
            print("⚠️ Vision mode active but 0 images generated!")
            print("⚠️ Falling back to text extraction...")
            # Try text extraction as last resort
            extracted_text2 = (await file_handler.extract_text(job["file_path"], use_vision=False)).get("text", "")
            if extracted_text2.strip():
                print(f"📄 Using text fallback: {len(extracted_text2):,} chars")
                questions = await parser.parse(extracted_text2, progress_callback=update_progress)
            else:
                raise ValueError("Vision mode produced no images and text extraction also failed")
        elif extracted_text.strip():
            # Text mode: parse from text
            print(f"📄 Processing {len(extracted_text):,} chars of text")
            questions = await parser.parse(extracted_text, progress_callback=update_progress)
        else:
            raise ValueError("No content could be extracted from file (both text and vision failed)")

        # ============================================================
        # Step 3: Done
        # ============================================================
        processing_time = time.time() - job.get("start_time", time.time())
        
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["result"] = questions
        jobs[job_id]["processing_time"] = round(processing_time, 1)
        jobs[job_id]["mode"] = mode

        print(f"✅ Job {job_id} done: {len(questions)} questions in {processing_time:.1f}s ({mode} mode)")

    except Exception as e:
        import traceback
        traceback.print_exc()
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["progress"] = 0
        print(f"❌ Job {job_id} failed: {e}")

    finally:
        # Cleanup uploaded file
        try:
            if os.path.exists(job.get("file_path", "")):
                os.remove(job["file_path"])
        except Exception:
            pass


def _is_math_text_poor_quality(text: str) -> bool:
    """
    Kiểm tra xem text extracted có bị lỗi công thức toán không.
    
    Dấu hiệu text bị lỗi:
    - Nhiều ký tự xuống dòng liên tiếp trong biểu thức
    - Phân số bị tách ra thành nhiều dòng
    - Quá nhiều ký tự đơn lẻ trên một dòng (OCR lỗi)
    """
    if not text:
        return True
    
    lines = text.split('\n')
    total_lines = len(lines)
    
    if total_lines < 5:
        return False  # Quá ít để đánh giá
    
    # Đếm dòng quá ngắn (1-3 ký tự) → dấu hiệu OCR lỗi
    short_lines = sum(1 for line in lines if 0 < len(line.strip()) <= 3)
    short_ratio = short_lines / total_lines
    
    # Đếm dòng chỉ có 1 ký tự (x, y, z, +, -, =, ...) → phân số bị vỡ
    single_char_lines = sum(1 for line in lines if len(line.strip()) == 1)
    single_ratio = single_char_lines / total_lines
    
    # Nếu >30% dòng quá ngắn hoặc >15% dòng 1 ký tự → text quality kém
    if short_ratio > 0.30 or single_ratio > 0.15:
        print(f"📊 Text quality check: {short_ratio:.0%} short lines, {single_ratio:.0%} single-char lines → POOR")
        return True
    
    return False


@app.get("/api/status/{job_id}", response_model=JobStatusResponse)
async def get_status(job_id: str):
    """Check trạng thái parsing job"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs[job_id]
    
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        result=job.get("result"),
        error=job.get("error"),
        filename=job.get("filename"),
        processing_time=job.get("processing_time")
    )


@app.post("/api/parse-sync")
async def parse_file_sync(
    file: UploadFile = File(...),
    speed: str = Query("balanced")
):
    """Upload và parse đồng bộ (chờ kết quả)."""
    allowed_extensions = {'.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg', '.txt', '.md'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="File type not supported")
    
    # Select parser
    if speed == "quality":
        parser = create_quality_parser()
    elif speed == "fast":
        parser = create_fast_parser()
    else:
        parser = create_balanced_parser()
    
    temp_path = os.path.join(UPLOAD_DIR, f"sync_{uuid.uuid4()}_{file.filename}")
    
    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)
        
        # Auto-vision for PDF files
        use_vision = file_ext == '.pdf'
        extracted = await file_handler.extract_text(temp_path, use_vision=use_vision)
        
        if use_vision and extracted.get("images"):
            # Vision mode
            start_time = time.time()
            questions = await parser.parse_images(extracted["images"])
            elapsed = time.time() - start_time
        elif extracted.get("text"):
            # Text mode
            start_time = time.time()
            questions = await parser.parse(extracted["text"])
            elapsed = time.time() - start_time
        else:
            raise HTTPException(status_code=400, detail="Could not extract content from file")
        
        return {
            "filename": file.filename,
            "speed_mode": speed,
            "processing_time_seconds": round(elapsed, 1),
            "total_questions": len(questions),
            "questions": questions
        }
        
    finally:
        try:
            os.remove(temp_path)
        except:
            pass


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Xóa job và cleanup"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs.pop(job_id)
    
    if job.get("file_path") and os.path.exists(job["file_path"]):
        try:
            os.remove(job["file_path"])
        except:
            pass
    
    return {"message": "Job deleted", "job_id": job_id}


@app.get("/api/jobs")
async def list_jobs():
    """Liệt kê tất cả jobs"""
    return {
        "total": len(jobs),
        "jobs": [
            {
                "job_id": jid,
                "status": j["status"],
                "filename": j.get("filename"),
                "speed_mode": j.get("speed_mode"),
                "processing_time": j.get("processing_time"),
                "created_at": j.get("created_at")
            }
            for jid, j in jobs.items()
        ]
    }


@app.get("/health")
async def health():
    """Health check for Railway"""
    return {
        "status": "ok",
        "version": "3.0.0",
        "port": PORT,
        "api_base": API_BASE or "(same origin)",
        "timestamp": datetime.now().isoformat()
    }


# ==================== MAIN ====================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*50)
    print("🚀 Math Exam Parser API v3.0")
    print("="*50)
    print(f"📍 http://localhost:{PORT}")
    print(f"🔗 API_BASE: {API_BASE or '(same origin)'}")
    print(f"📁 Upload dir: {UPLOAD_DIR}")
    print(f"📄 Templates: {'Yes' if USE_TEMPLATES else 'Embedded HTML'}")
    print("="*50 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=PORT)