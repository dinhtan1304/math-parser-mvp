"""
Media upload API — upload images/audio for quiz questions.
Includes a proxy endpoint for external media (Google Drive, Dropbox, etc.)
"""

import hashlib
import logging
import os
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_current_active_user
from app.db.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

MEDIA_DIR = "media_uploads"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | AUDIO_EXTENSIONS


class MediaUploadResponse(BaseModel):
    url: str
    type: str  # "image" | "audio"


@router.post("/upload", response_model=MediaUploadResponse)
async def upload_media(
    file: UploadFile = File(...),
    user: User = Depends(get_current_active_user),
):
    """Upload an image or audio file for quiz content."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # Read and validate size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(content) / 1024 / 1024:.1f} MB). Maximum: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB",
        )

    # Determine media type
    media_type = "image" if ext in IMAGE_EXTENSIONS else "audio"

    # Save with unique name
    unique_name = f"{uuid.uuid4().hex}{ext}"
    os.makedirs(MEDIA_DIR, exist_ok=True)
    file_path = os.path.join(MEDIA_DIR, unique_name)

    with open(file_path, "wb") as f:
        f.write(content)

    logger.info(f"Media uploaded: {unique_name} ({media_type}, {len(content)} bytes) by user {user.id}")

    return MediaUploadResponse(
        url=f"/media/{unique_name}",
        type=media_type,
    )


# ── Proxy for external media (Google Drive, Dropbox, etc.) ───────────

PROXY_ALLOWED_DOMAINS = {
    "drive.google.com",
    "drive.usercontent.google.com",
    "docs.google.com",
    "dl.dropboxusercontent.com",
    "www.dropbox.com",
    "dropbox.com",
}
PROXY_MAX_SIZE = 50 * 1024 * 1024  # 50 MB
PROXY_CACHE_DIR = os.path.join(MEDIA_DIR, "_cache")


def _resolve_drive_url(url: str) -> str:
    """Convert Google Drive share links to direct download URLs."""
    import re
    m = re.search(r"drive\.google\.com/file/d/([^/?#]+)", url)
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download"
    m = re.search(r"drive\.google\.com/open\?id=([^&#]+)", url)
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download"
    return url


def _resolve_dropbox_url(url: str) -> str:
    if "dropbox.com" in url and "dl=0" in url:
        return url.replace("dl=0", "dl=1")
    return url


@router.get("/proxy")
async def proxy_media(
    url: str = Query(..., min_length=10, max_length=2000),
):
    """Proxy external media URLs to bypass CORS/redirect issues.
    No auth required — only whitelisted domains are allowed."""
    parsed = urlparse(url)
    if parsed.hostname not in PROXY_ALLOWED_DOMAINS:
        raise HTTPException(status_code=400, detail="Domain not allowed for proxy")

    # Resolve share links to direct download
    resolved = url
    if "drive.google.com" in url:
        resolved = _resolve_drive_url(url)
    elif "dropbox.com" in url:
        resolved = _resolve_dropbox_url(url)

    # Check local cache first
    url_hash = hashlib.md5(resolved.encode()).hexdigest()
    os.makedirs(PROXY_CACHE_DIR, exist_ok=True)

    # Look for cached file (any extension)
    cached = [f for f in os.listdir(PROXY_CACHE_DIR) if f.startswith(url_hash)]
    if cached:
        cached_path = os.path.join(PROXY_CACHE_DIR, cached[0])
        ext = os.path.splitext(cached[0])[1]
        ct = _ext_to_content_type(ext)

        def _stream_cached():
            with open(cached_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return StreamingResponse(_stream_cached(), media_type=ct)

    # Fetch from external source
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(resolved)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch: {e}")

    content = resp.content
    if len(content) > PROXY_MAX_SIZE:
        raise HTTPException(status_code=413, detail="File too large")

    # Determine content type and extension
    ct = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    ext = _content_type_to_ext(ct)

    # Cache to disk
    cache_file = os.path.join(PROXY_CACHE_DIR, f"{url_hash}{ext}")
    with open(cache_file, "wb") as f:
        f.write(content)

    return StreamingResponse(
        iter([content]),
        media_type=ct,
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _content_type_to_ext(ct: str) -> str:
    mapping = {
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
        "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/ogg": ".ogg", "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a", "audio/aac": ".aac",
        "image/jpeg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/webp": ".webp",
    }
    return mapping.get(ct, ".bin")


def _ext_to_content_type(ext: str) -> str:
    mapping = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".ogg": "audio/ogg", ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(ext, "application/octet-stream")
