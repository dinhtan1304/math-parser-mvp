"""
Ánh xạ bán tự động: bài học (curriculum) → yêu cầu cần đạt (yccd).

Quy trình (theo quyết định "bán tự động: LLM gợi ý → người duyệt"):
  1. Pre-filter DETERMINISTIC: gom YCCĐ ứng viên theo chương (chapter → topic).
  2. LLM (Gemini) chọn tập con YCCĐ liên quan nhất cho TỪNG bài.
  3. Ghi vào `curriculum_yccd` với source="llm" (CHƯA duyệt).
  4. Người dùng review trên FE/DB rồi đổi source → "reviewed".

Usage:
    # Xem gợi ý, KHÔNG ghi DB:
    python scripts/map_yccd_lessons.py --grade 6 --dry-run
    # Ghi gợi ý LLM vào DB (source=llm):
    python scripts/map_yccd_lessons.py --grade 6
    # Không có API key / muốn nhanh: chỉ map theo chương (deterministic, source=reviewed):
    python scripts/map_yccd_lessons.py --grade 6 --deterministic
    # Đánh dấu toàn bộ ánh xạ của lớp là đã duyệt:
    python scripts/map_yccd_lessons.py --grade 6 --mark-reviewed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.db.models.curriculum import Curriculum  # noqa: E402
from app.db.models.lesson_plan import Yccd, CurriculumYccd  # noqa: E402


# Pre-filter deterministic: chương (chapter_no) → các topic YCCĐ ứng viên.
# Khớp `topic` trong app/db/seed_yccd.py. Toán 6 KNTT.
CHAPTER_TO_TOPICS_TOAN6 = {
    1: ["Số tự nhiên"],
    2: ["Tính chia hết"],
    3: ["Số nguyên"],
    4: ["Hình phẳng trong thực tiễn"],
    5: ["Tính đối xứng"],
    6: ["Phân số"],
    7: ["Số thập phân"],
    8: ["Hình học cơ bản"],
    9: ["Thống kê", "Xác suất thực nghiệm"],
}


def _candidate_yccd(chapter_no: int, all_yccd: list[Yccd]) -> list[Yccd]:
    topics = CHAPTER_TO_TOPICS_TOAN6.get(chapter_no)
    if not topics:
        return list(all_yccd)
    return [y for y in all_yccd if y.topic in topics]


_PICK_SCHEMA = {"type": "ARRAY", "items": {"type": "STRING"}}


async def _llm_pick(lesson: Curriculum, candidates: list[Yccd]) -> list[str]:
    """Gọi Gemini chọn tập con mã YCCĐ liên quan đến bài. Trả list code."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY", ""))
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    cand_text = "\n".join(f"- {y.code}: {y.requirement}" for y in candidates)
    prompt = (
        "Bạn là chuyên gia chương trình GDPT 2018 môn Toán. Cho một BÀI HỌC và danh "
        "sách các YÊU CẦU CẦN ĐẠT ứng viên (đã lọc theo chương). Hãy chọn các mã YCCĐ "
        "THỰC SỰ liên quan trực tiếp đến bài học này (thường 1–4 mã). Chỉ trả về mảng "
        "JSON các mã, KHÔNG giải thích, KHÔNG chọn mã ngoài danh sách.\n\n"
        f"BÀI HỌC: {lesson.chapter} — {lesson.lesson_title}\n\n"
        f"YCCĐ ỨNG VIÊN:\n{cand_text}\n"
    )

    valid_codes = {y.code for y in candidates}
    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=_PICK_SCHEMA,
        ),
    )
    raw = (getattr(resp, "text", "") or "").strip()
    try:
        codes = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        codes = []
    # Lọc về danh mục ứng viên (chống bịa)
    return [c for c in codes if c in valid_codes]


async def _upsert_mapping(session, curriculum_id: int, codes: list[str],
                          code_to_id: dict[str, int], source: str) -> int:
    """Thêm các (curriculum_id, yccd_id) chưa có. Trả số mapping mới."""
    existing = {
        row[0]
        for row in (await session.execute(
            text("SELECT yccd_id FROM curriculum_yccd WHERE curriculum_id = :cid"),
            {"cid": curriculum_id},
        )).fetchall()
    }
    added = 0
    for c in codes:
        yid = code_to_id.get(c)
        if yid is None or yid in existing:
            continue
        session.add(CurriculumYccd(curriculum_id=curriculum_id, yccd_id=yid, source=source))
        added += 1
    return added


async def run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        # --mark-reviewed: chỉ đổi source → reviewed cho cả lớp rồi thoát
        if args.mark_reviewed:
            res = await session.execute(text(
                "UPDATE curriculum_yccd SET source='reviewed' "
                "WHERE curriculum_id IN (SELECT id FROM curriculum WHERE grade=:g AND subject_code='toan')"
            ), {"g": args.grade})
            await session.commit()
            print(f"Đã đánh dấu reviewed cho lớp {args.grade}: {res.rowcount} mapping.")
            return 0

        lessons = list((await session.execute(
            select(Curriculum).where(
                Curriculum.grade == args.grade,
                Curriculum.subject_code == "toan",
            ).order_by(Curriculum.chapter_no, Curriculum.lesson_no)
        )).scalars().all())

        all_yccd = list((await session.execute(
            select(Yccd).where(Yccd.grade == args.grade, Yccd.subject_code == "toan")
        )).scalars().all())
        code_to_id = {y.code: y.id for y in all_yccd}

        if not lessons or not all_yccd:
            print(f"Thiếu dữ liệu: lessons={len(lessons)}, yccd={len(all_yccd)}. "
                  f"Hãy chạy app 1 lần để seed, hoặc kiểm tra grade.", file=sys.stderr)
            return 1

        if args.limit:
            lessons = lessons[: args.limit]

        total_added = 0
        for lesson in lessons:
            cands = _candidate_yccd(lesson.chapter_no, all_yccd)
            if args.deterministic:
                codes = [y.code for y in cands]
                source = "reviewed"
            else:
                codes = await _llm_pick(lesson, cands)
                source = "llm"

            print(f"[{lesson.chapter_no}.{lesson.lesson_no}] {lesson.lesson_title}")
            print(f"    → {codes or '(không có)'}")

            if not args.dry_run and codes:
                total_added += await _upsert_mapping(
                    session, lesson.id, codes, code_to_id, source
                )

        if not args.dry_run:
            await session.commit()
            print(f"\nĐã ghi {total_added} mapping mới (source={'reviewed' if args.deterministic else 'llm'}).")
        else:
            print("\n(dry-run: không ghi DB)")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Ánh xạ bài học → yêu cầu cần đạt (bán tự động).")
    ap.add_argument("--grade", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="Chỉ xử lý N bài đầu (0 = tất cả)")
    ap.add_argument("--dry-run", action="store_true", help="In gợi ý, không ghi DB")
    ap.add_argument("--deterministic", action="store_true",
                    help="Bỏ LLM: map mọi YCCĐ cùng chương (source=reviewed)")
    ap.add_argument("--mark-reviewed", action="store_true",
                    help="Đổi source mọi mapping của lớp thành 'reviewed' rồi thoát")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
