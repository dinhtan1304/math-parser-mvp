"""
IELTS Audio binding API.

Endpoint:
    POST /parser/ielts/{quiz_id}/audio
        Upload an mp3/m4a/wav file and bind it to a Listening section
        of an IELTS quiz. The URL is recorded in:
          - Quiz.settings["audio_tracks"][section_title]
          - QuizTheorySection.media = {type, url, duration_sec}
"""

import hashlib
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.db.session import get_db
from app.db.models.quiz import Quiz, QuizTheory, QuizTheorySection
from app.db.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


AUDIO_DIR = os.path.join("media_uploads", "ielts_audio")
ALLOWED_AUDIO_EXT = {".mp3", ".m4a", ".wav"}
MAX_AUDIO_BYTES = 50 * 1024 * 1024  # 50 MB

# Magic byte signatures
_AUDIO_MAGIC = {
    ".mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    ".m4a": [b"ftyp"],          # Appears at offset 4 — handled specially
    ".wav": [b"RIFF"],          # WAVE signature at offset 8 verified separately
}


def _validate_audio_magic(content: bytes, ext: str) -> bool:
    if ext == ".mp3":
        return any(content.startswith(m) for m in _AUDIO_MAGIC[".mp3"])
    if ext == ".m4a":
        return len(content) > 12 and content[4:8] == b"ftyp"
    if ext == ".wav":
        return content.startswith(b"RIFF") and len(content) > 12 and content[8:12] == b"WAVE"
    return False


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:64] or "section"


def _probe_duration_sec(file_path: str) -> Optional[float]:
    """Best-effort duration extraction. Returns None if mutagen unavailable."""
    try:
        from mutagen import File as MutagenFile  # type: ignore
        meta = MutagenFile(file_path)
        if meta and meta.info and getattr(meta.info, "length", None):
            return round(float(meta.info.length), 2)
    except Exception:
        pass
    return None


class IeltsAudioResponse(BaseModel):
    section_title: str
    url: str
    duration_sec: Optional[float] = None
    bytes: int


@router.post("/ielts/{quiz_id}/audio", response_model=IeltsAudioResponse)
async def upload_ielts_audio(
    quiz_id: int,
    section_title: str = Form(..., min_length=1, max_length=200),
    file: UploadFile = File(...),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an audio file to a Listening section of an IELTS quiz."""
    # ── Validate quiz ownership + IELTS ──
    result = await db.execute(
        select(Quiz)
        .options(selectinload(Quiz.theories).selectinload(QuizTheory.sections))
        .where(Quiz.id == quiz_id)
    )
    quiz: Optional[Quiz] = result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz không tồn tại.")
    if quiz.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Bạn không có quyền sửa quiz này.")
    if (quiz.subject_code or "").lower() != "ielts":
        raise HTTPException(status_code=400, detail="Quiz không phải IELTS.")

    # ── Locate target theory matching section_title ──
    target_theory: Optional[QuizTheory] = next(
        (t for t in (quiz.theories or []) if (t.title or "").strip() == section_title.strip()),
        None,
    )
    if not target_theory:
        available = [t.title for t in (quiz.theories or [])]
        raise HTTPException(
            status_code=404,
            detail=f"Section '{section_title}' không tồn tại. Có sẵn: {available}",
        )

    # ── Validate file ──
    raw_name = file.filename or ""
    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Định dạng '{ext}' không hỗ trợ. Cho phép: {sorted(ALLOWED_AUDIO_EXT)}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File trống")
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn ({len(content) / (1024*1024):.1f} MB). Tối đa 50 MB.",
        )
    if not _validate_audio_magic(content, ext):
        raise HTTPException(status_code=400, detail="Nội dung file không khớp định dạng audio.")

    # ── Save to disk ──
    quiz_dir = os.path.join(AUDIO_DIR, str(quiz_id))
    os.makedirs(quiz_dir, exist_ok=True)
    slug = _slugify(section_title)
    digest = hashlib.md5(content).hexdigest()[:8]
    file_name = f"{slug}-{digest}{ext}"
    file_path = os.path.join(quiz_dir, file_name)
    with open(file_path, "wb") as f:
        f.write(content)

    # Web URL — served by app.mount("/media", ...)
    url = f"/media/ielts_audio/{quiz_id}/{file_name}"
    duration = _probe_duration_sec(file_path)

    # ── Update Quiz.settings.audio_tracks ──
    current_settings = dict(quiz.settings or {})
    tracks = dict(current_settings.get("audio_tracks") or {})
    tracks[section_title.strip()] = url
    current_settings["audio_tracks"] = tracks
    quiz.settings = current_settings

    # ── Update QuizTheorySection.media (1st section of the theory) ──
    sections = list(target_theory.sections or [])
    if sections:
        first = sections[0]
        first.media = {"type": "audio", "url": url, "duration_sec": duration}

    await db.commit()
    logger.info(
        "IELTS audio bound: quiz=%s section=%s bytes=%s url=%s",
        quiz_id, section_title, len(content), url,
    )

    return IeltsAudioResponse(
        section_title=section_title.strip(),
        url=url,
        duration_sec=duration,
        bytes=len(content),
    )


class IeltsListeningSection(BaseModel):
    section_title: str
    has_audio: bool
    audio_url: Optional[str] = None


class IeltsListeningSectionsResponse(BaseModel):
    quiz_id: int
    sections: list[IeltsListeningSection]


@router.get("/ielts/{quiz_id}/listening-sections", response_model=IeltsListeningSectionsResponse)
async def list_listening_sections(
    quiz_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List Listening sections detected in an IELTS quiz (for the audio
    attachment step on the upload page)."""
    result = await db.execute(
        select(Quiz)
        .options(selectinload(Quiz.theories))
        .where(Quiz.id == quiz_id)
    )
    quiz: Optional[Quiz] = result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz không tồn tại.")
    if quiz.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Bạn không có quyền xem quiz này.")
    if (quiz.subject_code or "").lower() != "ielts":
        raise HTTPException(status_code=400, detail="Quiz không phải IELTS.")

    tracks = ((quiz.settings or {}).get("audio_tracks") or {})
    sections: list[IeltsListeningSection] = []
    for t in quiz.theories or []:
        title = (t.title or "").strip()
        if not _is_listening_section(title):
            continue
        url = tracks.get(title)
        sections.append(IeltsListeningSection(
            section_title=title,
            has_audio=bool(url),
            audio_url=url,
        ))
    return IeltsListeningSectionsResponse(quiz_id=quiz_id, sections=sections)


def _is_listening_section(title: str) -> bool:
    """Match titles like 'Section 1', 'Listening Section 2', 'Part 3 (Listening)'."""
    if not title:
        return False
    t = title.lower().strip()
    if t.startswith("listening"):
        return True
    if re.match(r"^section\s+[1-4]\b", t):
        return True
    if "listening" in t and re.search(r"\b(part|section)\s*[1-4]\b", t):
        return True
    return False
