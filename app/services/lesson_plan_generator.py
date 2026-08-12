"""
Pipeline sinh KHBD (Công văn 5512) — 2 bước: GROUND → GENERATE.

(1) GROUND (deterministic, KHÔNG gọi LLM sinh):
      - Lookup bài (curriculum) → YCCĐ qua join `curriculum_yccd` (neo chống bịa).
      - Retrieve câu hỏi/bài tập THẬT liên quan từ ngân hàng (anchor chống sáo rỗng).
(2) GENERATE (1 lần gọi Gemini, output = KHBD_SCHEMA JSON):
      - Prompt = khung CV5512 + YCCĐ nguyên văn + ví dụ bài tập + quy tắc chống sáo rỗng.
      - 3-tier fallback (schema / json / plain) như ai_generator.
      - Validate (cấu trúc + grounding + sáo rỗng); fail → retry 1 lần với phản hồi lỗi.

Cache Redis theo (curriculum_id, so_tiet, model) — degrade in-memory nếu không có Redis.
"""

from __future__ import annotations

import os
import json
import hashlib
import logging
import asyncio
from typing import Any, Dict, List, Optional

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.curriculum import Curriculum
from app.db.models.question import Question
from app.db.models.lesson_plan import Yccd, CurriculumYccd
from app.services.lesson_plan_schema import (
    KHBD_SCHEMA, validate_khbd,
    NANG_LUC_CHUNG, NANG_LUC_DAC_THU_BY_SUBJECT, PHAM_CHAT,
    HOAT_DONG_LABELS,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = 7 * 24 * 3600  # 7 ngày
_MAX_EXAMPLES = 8


# ── GROUND ──────────────────────────────────────────────────────────────────

async def ground_lesson(
    db: AsyncSession, curriculum_id: int, user_id: int,
) -> Dict[str, Any]:
    """Thu thập NEO cho 1 bài: thông tin bài + YCCĐ + ví dụ bài tập thật.

    Raises ValueError nếu bài không tồn tại.
    """
    lesson = (await db.execute(
        select(Curriculum).where(Curriculum.id == curriculum_id)
    )).scalars().first()
    if lesson is None:
        raise ValueError(f"Không tìm thấy bài học id={curriculum_id}")

    # YCCĐ neo qua join curriculum_yccd
    yccd_rows = list((await db.execute(
        select(Yccd)
        .join(CurriculumYccd, CurriculumYccd.yccd_id == Yccd.id)
        .where(CurriculumYccd.curriculum_id == curriculum_id)
        .order_by(Yccd.code)
    )).scalars().all())

    # Ví dụ bài tập thật từ ngân hàng (của giáo viên hoặc public), cùng chương
    examples: List[Dict[str, str]] = []
    try:
        q_rows = list((await db.execute(
            select(Question).where(
                or_(Question.user_id == user_id, Question.is_public.is_(True)),
                Question.subject_code == (lesson.subject_code or "toan"),
                Question.grade == lesson.grade,
                Question.chapter == lesson.chapter,
            ).order_by(func.random()).limit(_MAX_EXAMPLES)
        )).scalars().all())
        examples = [
            {"question": q.question_text, "answer": q.answer or ""}
            for q in q_rows
        ]
    except Exception as e:
        logger.debug(f"Ground examples skipped: {e}")

    return {
        "lesson": lesson,
        "yccd": yccd_rows,
        "examples": examples,
    }


# ── PROMPT ──────────────────────────────────────────────────────────────────

def _build_prompt(
    lesson: Curriculum, yccd: List[Yccd], examples: List[Dict[str, str]],
    so_tiet: int, subject_code: str,
) -> str:
    yccd_block = "\n".join(f"- [{y.code}] {y.requirement}" for y in yccd) or "(chưa có)"
    dac_thu = NANG_LUC_DAC_THU_BY_SUBJECT.get(subject_code, [])
    ex_block = (
        "\n".join(
            f"- {e['question']}" + (f"  → ĐA: {e['answer']}" if e["answer"] else "")
            for e in examples
        )
        if examples else "(không có — hãy tự đặt bài tập cụ thể, đúng nội dung bài)"
    )
    hoat_dong_block = "\n".join(f"  + {v} (loai=\"{k}\")" for k, v in HOAT_DONG_LABELS.items())

    return f"""Bạn là chuyên gia soạn Kế hoạch bài dạy (KHBD) theo Công văn 5512/BGDĐT-GDTrH.
Soạn KHBD cho bài học sau, ĐÚNG khung Phụ lục IV, trả về JSON theo schema.

THÔNG TIN BÀI:
- Môn: {lesson.subject_code or subject_code} — Lớp {lesson.grade} — Bộ sách Kết nối tri thức
- Chương: {lesson.chapter}
- Bài: {lesson.lesson_title}
- Thời gian: {so_tiet} tiết

YÊU CẦU CẦN ĐẠT (NEO — BẮT BUỘC bám sát, KHÔNG tự nghĩ mục tiêu ngoài danh sách này):
{yccd_block}

QUY TẮC MỤC TIÊU:
- "kien_thuc": diễn giải các YCCĐ trên thành kiến thức cụ thể HS cần đạt.
- "yccd_refs": CHỈ chứa các mã trong ngoặc vuông ở trên (vd {yccd[0].code if yccd else 'TOAN6.XXX.01'}).
- "nang_luc.chung": chọn trong {NANG_LUC_CHUNG}, mỗi mục nêu biểu hiện CỤ THỂ gắn bài.
- "nang_luc.dac_thu": chọn trong {dac_thu}, biểu hiện CỤ THỂ.
- "pham_chat": chọn trong {PHAM_CHAT}, biểu hiện gắn nội dung bài.

TIẾN TRÌNH DẠY HỌC — gồm 4 hoạt động, mỗi hoạt động đủ 4 phần
(muc_tieu / noi_dung / san_pham / to_chuc_thuc_hien với 4 bước):
{hoat_dong_block}

NGÂN HÀNG BÀI TẬP THẬT (dùng/biến tấu làm "noi_dung" và "san_pham", KHÔNG bịa lý thuyết suông):
{ex_block}

CHỐNG SÁO RỖNG (BẮT BUỘC):
- Mỗi hoạt động phải có ≥1 câu hỏi/bài tập CỤ THỂ trong "cau_hoi_nhiem_vu"
  (kèm số liệu/biểu thức), và "ket_qua_mong_doi" là đáp án/kết quả CỤ THỂ.
- TRÁNH câu chung chung: "HS nắm được kiến thức", "HS hiểu bài", "rèn kỹ năng tư duy".
- Công thức toán viết bằng LaTeX trong $...$.

Trả về DUY NHẤT một object JSON đúng schema (muc_tieu, thiet_bi_day_hoc, tien_trinh)."""


# ── GENERATE (Gemini) ───────────────────────────────────────────────────────

class LessonPlanGenerator:
    MAX_CONCURRENT = 3

    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY", "")
        # KHBD dùng model RẺ (flash) theo quyết định; cho phép override
        self.model = os.getenv("KHBD_MODEL", "gemini-2.5-flash")
        self.max_tokens = 16000
        self._client = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._init_client()

    def _init_client(self):
        if not self.api_key:
            logger.warning("No GOOGLE_API_KEY for KHBD generator")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        except Exception as e:
            logger.error(f"KHBD Gemini init error: {e}")

    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        return self._semaphore

    async def _call(self, prompt: str) -> str:
        """3-tier fallback (schema / json / plain), retry rate-limit."""
        from google.genai import types

        async with self._sem():
            async def _with_retry(config, label) -> Optional[str]:
                for attempt in range(3):
                    try:
                        resp = await self._client.aio.models.generate_content(
                            model=self.model, contents=prompt, config=config,
                        )
                        txt = getattr(resp, "text", "") or ""
                        if txt:
                            return txt
                        return None
                    except Exception as e:
                        es = str(e)
                        if "429" in es or "RESOURCE_EXHAUSTED" in es:
                            wait = (attempt + 1) * 10
                            logger.warning(f"KHBD {label} rate limited, wait {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(f"KHBD {label} failed: {e}")
                        return None
                return None

            for mime, schema, label in [
                ("application/json", KHBD_SCHEMA, "Schema"),
                ("application/json", None,        "JSON"),
                (None,               None,         "Plain"),
            ]:
                cfg: Dict[str, Any] = dict(temperature=0.4, max_output_tokens=self.max_tokens)
                if mime:
                    cfg["response_mime_type"] = mime
                if schema:
                    cfg["response_schema"] = schema
                txt = await _with_retry(types.GenerateContentConfig(**cfg), label)
                if txt:
                    return txt
            raise RuntimeError("Gemini KHBD: tất cả mode thất bại, thử lại sau.")

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        text = text.strip()
        try:
            r = json.loads(text)
            return r if isinstance(r, dict) else None
        except json.JSONDecodeError:
            pass
        # strip fences / locate object
        if "```" in text:
            text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                r = json.loads(text[start:end + 1])
                return r if isinstance(r, dict) else None
            except json.JSONDecodeError:
                return None
        return None


lesson_plan_generator = LessonPlanGenerator()


# ── Cache helpers ───────────────────────────────────────────────────────────

def _cache_key(curriculum_id: int, so_tiet: int, yccd_codes: List[str], model: str) -> str:
    raw = f"{curriculum_id}|{so_tiet}|{','.join(sorted(yccd_codes))}|{model}"
    return "khbd:" + hashlib.md5(raw.encode("utf-8")).hexdigest()


# ── Orchestration ───────────────────────────────────────────────────────────

async def generate_khbd(
    db: AsyncSession,
    curriculum_id: int,
    user_id: int,
    so_tiet: Optional[int] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Sinh KHBD cho 1 bài. Trả dict gồm meta + KHBD + _warnings + _yccd_refs.

    Raises ValueError (bài không tồn tại / chưa map YCCĐ), RuntimeError (LLM lỗi).
    """
    ctx = await ground_lesson(db, curriculum_id, user_id)
    lesson: Curriculum = ctx["lesson"]
    yccd: List[Yccd] = ctx["yccd"]
    if not yccd:
        raise ValueError(
            f"Bài '{lesson.lesson_title}' chưa được ánh xạ yêu cầu cần đạt. "
            f"Hãy chạy scripts/map_yccd_lessons.py trước."
        )

    subject_code = lesson.subject_code or "toan"
    eff_so_tiet = so_tiet or 1
    valid_codes = [y.code for y in yccd]

    meta = {
        "ten_bai": lesson.lesson_title,
        "mon": "Toán" if subject_code == "toan" else subject_code,
        "lop": lesson.grade,
        "bo_sach": "Kết nối tri thức",
        "so_tiet": eff_so_tiet,
        "chuong": lesson.chapter,
    }

    # Cache hit?
    ckey = _cache_key(curriculum_id, eff_so_tiet, valid_codes, lesson_plan_generator.model)
    if use_cache:
        cached = await _cache_get(ckey)
        if cached is not None:
            cached["meta"] = meta
            cached["_cached"] = True
            return cached

    if lesson_plan_generator._client is None:
        raise RuntimeError("GOOGLE_API_KEY chưa cấu hình — không sinh được KHBD.")

    base_prompt = _build_prompt(lesson, yccd, ctx["examples"], eff_so_tiet, subject_code)

    khbd: Optional[Dict[str, Any]] = None
    validation = {"ok": False, "errors": ["chưa sinh"], "warnings": []}
    for attempt in range(2):
        prompt = base_prompt
        if attempt == 1 and validation["errors"]:
            prompt = base_prompt + (
                "\n\nLẦN TRƯỚC BỊ LỖI, hãy SỬA: " + "; ".join(validation["errors"])
            )
        raw = await lesson_plan_generator._call(prompt)
        parsed = lesson_plan_generator._extract_json(raw)
        if parsed is None:
            validation = {"ok": False, "errors": ["JSON không hợp lệ"], "warnings": []}
            continue
        validation = validate_khbd(parsed, valid_codes, subject_code)
        khbd = parsed
        if validation["ok"]:
            break

    if khbd is None:
        raise RuntimeError("Không sinh được KHBD hợp lệ sau 2 lần thử.")

    result = {
        "meta": meta,
        "muc_tieu": khbd.get("muc_tieu", {}),
        "thiet_bi_day_hoc": khbd.get("thiet_bi_day_hoc", []),
        "tien_trinh": khbd.get("tien_trinh", []),
        "_warnings": validation["warnings"],
        "_errors": validation["errors"],
        "_valid": validation["ok"],
        "_yccd_refs": valid_codes,
        "_subject_code": subject_code,
        "_model": lesson_plan_generator.model,
        "_cached": False,
    }

    # Chỉ cache bản hợp lệ
    if use_cache and validation["ok"]:
        await _cache_set(ckey, result)

    return result


async def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    from app.core.redis_client import get_redis
    r = get_redis()
    if r is None:
        return None
    try:
        val = await r.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None


async def _cache_set(key: str, value: Dict[str, Any]) -> None:
    from app.core.redis_client import get_redis
    r = get_redis()
    if r is None:
        return
    try:
        await r.setex(key, _CACHE_TTL, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass
