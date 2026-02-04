"""
Math Exam Parser MVP - OPTIMIZED
Upload file đề toán → AI phân tích → JSON output
"""

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uuid
import os
import asyncio
import time
from datetime import datetime

from file_handler import FileHandler
from ai_parser import AIQuestionParser, create_fast_parser, create_balanced_parser, create_quality_parser

# ==================== CONFIG ====================

UPLOAD_DIR = "/tmp/math_parser_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ==================== APP ====================

app = FastAPI(
    title="Math Exam Parser API - Optimized",
    description="Upload đề toán và phân tích thành JSON (Parallel Processing)",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize services
file_handler = FileHandler()

# Job tracking
jobs: Dict[str, Dict] = {}


# ==================== SCHEMAS ====================

class Question(BaseModel):
    question: str
    type: str
    topic: str
    difficulty: str
    solution_steps: List[str]
    answer: str


class ParseResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    result: Optional[List[Question]] = None
    error: Optional[str] = None
    filename: Optional[str] = None
    processing_time: Optional[float] = None


# ==================== ENDPOINTS ====================

@app.post("/api/parse", response_model=ParseResponse)
async def parse_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    speed: str = "fast"  # fast, balanced, quality
):
    """
    Upload file đề toán để phân tích.
    
    Speed modes:
    - fast: 5 parallel requests, large chunks (fastest)
    - balanced: 3 parallel requests, medium chunks
    - quality: 2 parallel requests, smaller chunks (most accurate)
    """
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
        "start_time": time.time(),
        "processing_time": None
    }
    
    asyncio.create_task(process_file(job_id, speed))

    return ParseResponse(
        job_id=job_id,
        status="pending",
        message=f"File '{file.filename}' queued ({speed} mode)"
    )


async def process_file(job_id: str, speed: str = "fast"):
    job = jobs.get(job_id)
    if not job:
        return
    
    # Select parser based on speed mode
    if speed == "quality":
        parser = create_quality_parser()
    elif speed == "balanced":
        parser = create_balanced_parser()
    else:
        parser = create_fast_parser()
    
    print(f"\n🚀 Processing job {job_id} with {speed} mode")

    try:
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["progress"] = 10

        # Step 1: Extract text
        jobs[job_id]["progress"] = 20
        extracted = await file_handler.extract_text(job["file_path"])

        if not extracted.get("text"):
            raise ValueError("Could not extract text from file")
        
        extracted_text = extracted["text"]
        print(f"📄 Extracted {len(extracted_text):,} chars")

        # Step 2: AI parsing with progress tracking
        jobs[job_id]["progress"] = 30
        jobs[job_id]["status"] = f"processing (AI - {speed} mode)"
        
        def update_progress(current, total):
            # Map chunk progress to 30-90%
            pct = 30 + int((current / total) * 60)
            jobs[job_id]["progress"] = pct

        questions = await parser.parse(extracted_text, progress_callback=update_progress)

        # Done
        elapsed = time.time() - job["start_time"]
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["result"] = questions
        jobs[job_id]["processing_time"] = round(elapsed, 1)
        
        print(f"✅ Job {job_id} completed: {len(questions)} questions in {elapsed:.1f}s")

        # Cleanup
        try:
            os.remove(job["file_path"])
        except:
            pass

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["progress"] = 0
        print(f"❌ Job {job_id} failed: {e}")


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
        result=job["result"],
        error=job["error"],
        filename=job.get("filename"),
        processing_time=job.get("processing_time")
    )


@app.post("/api/parse-sync")
async def parse_file_sync(
    file: UploadFile = File(...),
    speed: str = "fast"
):
    """
    Upload và parse đồng bộ (chờ kết quả).
    """
    allowed_extensions = {'.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg', '.txt', '.md'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"File type not supported"
        )
    
    # Select parser
    if speed == "quality":
        parser = create_quality_parser()
    elif speed == "balanced":
        parser = create_balanced_parser()
    else:
        parser = create_fast_parser()
    
    temp_path = os.path.join(UPLOAD_DIR, f"sync_{uuid.uuid4()}_{file.filename}")
    
    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)
        
        # Extract text
        extracted = await file_handler.extract_text(temp_path)
        
        if not extracted.get("text"):
            raise HTTPException(status_code=400, detail="Could not extract text from file")
        
        # Parse with timing
        start_time = time.time()
        questions = await parser.parse(extracted["text"])
        elapsed = time.time() - start_time
        
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
    
    if job.get("file_path"):
        try:
            os.remove(job["file_path"])
        except:
            pass
    
    return {"message": "Job deleted"}


@app.get("/api/jobs")
async def list_jobs():
    """Liệt kê tất cả jobs"""
    return {
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
    return {
        "status": "ok",
        "version": "2.0.0-optimized",
        "features": ["parallel_processing", "speed_modes"],
        "timestamp": datetime.now().isoformat()
    }


# ==================== MAIN ====================

if __name__ == "__main__":
    # import uvicorn
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    app.run(debug=True, port=os.getenv("PORT", default=5000))