import json
import re
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.api.deps import get_current_user, get_db
from app.db.models.teacher_page import TeacherPage
from app.db.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_TEMPLATE_IDS = {
    "horizon", "galaxy", "sakura", "forest", "neon",
    "blueprint", "sunrise", "ocean", "chalk", "prism",
}

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{2,98}$')


class PageConfigIn(BaseModel):
    template_id: str
    slug: str
    title: str
    config: dict

    @field_validator("template_id")
    @classmethod
    def validate_template(cls, v: str) -> str:
        if v not in VALID_TEMPLATE_IDS:
            raise ValueError(f"Invalid template_id: {v}")
        return v

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.lower().strip()
        if not SLUG_RE.match(v):
            raise ValueError("Slug chỉ gồm chữ thường, số, dấu gạch ngang (3-100 ký tự)")
        return v

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 200:
            raise ValueError("Title phải từ 1-200 ký tự")
        return v


class PageConfigPatch(BaseModel):
    template_id: Optional[str] = None
    slug: Optional[str] = None
    title: Optional[str] = None
    config: Optional[dict] = None
    is_published: Optional[bool] = None

    @field_validator("template_id")
    @classmethod
    def validate_template(cls, v):
        if v is not None and v not in VALID_TEMPLATE_IDS:
            raise ValueError(f"Invalid template_id: {v}")
        return v

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v):
        if v is not None:
            v = v.lower().strip()
            if not SLUG_RE.match(v):
                raise ValueError("Slug chỉ gồm chữ thường, số, dấu gạch ngang (3-100 ký tự)")
        return v


def _serialize(page: TeacherPage) -> dict:
    return {
        "id": page.id,
        "template_id": page.template_id,
        "slug": page.slug,
        "title": page.title,
        "config": json.loads(page.config) if isinstance(page.config, str) else page.config,
        "is_published": page.is_published,
        "view_count": page.view_count,
        "created_at": page.created_at.isoformat() if page.created_at else None,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }


@router.get("/check-slug/{slug}")
async def check_slug(slug: str, db: AsyncSession = Depends(get_db)):
    """Public — check if a slug is available."""
    slug = slug.lower().strip()
    if not SLUG_RE.match(slug):
        return {"available": False, "reason": "invalid_format"}
    result = await db.execute(select(TeacherPage).where(TeacherPage.slug == slug))
    existing = result.scalars().first()
    return {"available": existing is None}


@router.get("/public/{slug}")
async def get_public_page(slug: str, db: AsyncSession = Depends(get_db)):
    """Public — fetch a published teacher page by slug."""
    result = await db.execute(
        select(TeacherPage).where(TeacherPage.slug == slug, TeacherPage.is_published == True)
    )
    page = result.scalars().first()
    if not page:
        raise HTTPException(status_code=404, detail="Trang không tồn tại hoặc chưa được publish")

    # Increment view count without blocking
    try:
        page.view_count = (page.view_count or 0) + 1
        await db.commit()
    except Exception:
        await db.rollback()

    return _serialize(page)


@router.get("/my")
async def list_my_pages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all pages owned by the current user."""
    result = await db.execute(
        select(TeacherPage)
        .where(TeacherPage.user_id == current_user.id)
        .order_by(TeacherPage.created_at.desc())
    )
    pages = result.scalars().all()
    return [_serialize(p) for p in pages]


@router.post("", status_code=201)
async def create_page(
    body: PageConfigIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new teacher page."""
    # Check slug uniqueness
    result = await db.execute(select(TeacherPage).where(TeacherPage.slug == body.slug))
    if result.scalars().first():
        raise HTTPException(status_code=409, detail="Slug đã được sử dụng, vui lòng chọn slug khác")

    page = TeacherPage(
        user_id=current_user.id,
        template_id=body.template_id,
        slug=body.slug,
        title=body.title,
        config=json.dumps(body.config, ensure_ascii=False),
        is_published=True,
    )
    db.add(page)
    await db.commit()
    await db.refresh(page)
    return _serialize(page)


@router.patch("/{page_id}")
async def update_page(
    page_id: int,
    body: PageConfigPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a page (owner only)."""
    result = await db.execute(select(TeacherPage).where(TeacherPage.id == page_id))
    page = result.scalars().first()
    if not page:
        raise HTTPException(status_code=404, detail="Trang không tồn tại")
    if page.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Không có quyền chỉnh sửa trang này")

    if body.slug is not None and body.slug != page.slug:
        dup = await db.execute(select(TeacherPage).where(TeacherPage.slug == body.slug))
        if dup.scalars().first():
            raise HTTPException(status_code=409, detail="Slug đã được sử dụng")
        page.slug = body.slug

    if body.template_id is not None:
        page.template_id = body.template_id
    if body.title is not None:
        page.title = body.title.strip()
    if body.config is not None:
        page.config = json.dumps(body.config, ensure_ascii=False)
    if body.is_published is not None:
        page.is_published = body.is_published

    page.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(page)
    return _serialize(page)


@router.delete("/{page_id}", status_code=204)
async def delete_page(
    page_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a page (owner only)."""
    result = await db.execute(select(TeacherPage).where(TeacherPage.id == page_id))
    page = result.scalars().first()
    if not page:
        raise HTTPException(status_code=404, detail="Trang không tồn tại")
    if page.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Không có quyền xóa trang này")

    await db.delete(page)
    await db.commit()
