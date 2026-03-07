"""
curriculum_matcher.py — Map câu hỏi AI vào đúng bài/chương trong bảng curriculum.

Sau khi AI parse xong, mỗi câu hỏi có:
  - grade: int (6-12), có thể None
  - chapter: str (AI tự sinh, format không chuẩn)
  - topic: str (AI tự sinh, ví dụ "TOÁN 8 — C2.Hằng đẳng thức")
  - lesson_title: str (AI tự sinh)

Bảng curriculum có dữ liệu chuẩn:
  - grade, chapter_no, chapter, lesson_no, lesson_title

Chiến lược match (theo thứ tự ưu tiên):
  1. Exact grade + chapter_no từ topic string ("C2." → chapter_no=2)
  2. Grade + fuzzy match chapter text
  3. Grade only → chapter_no = None (giữ nguyên grade, xóa chapter sai)
"""

import re
import logging
from typing import Optional
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Pre-compiled patterns
_RE_GRADE_FROM_TOPIC = re.compile(r'[Tt][Oo][Áá][Nn]\s*(\d{1,2})', re.UNICODE)
_RE_CHAPTER_NO = re.compile(r'\bC(\d{1,2})\b')
_RE_CHAPTER_ROMAN = re.compile(r'\bChương\s+([IVX]+)\b', re.IGNORECASE)

_ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5,
          'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10,
          'XI': 11, 'XII': 12}


def _roman_to_int(s: str) -> Optional[int]:
    return _ROMAN.get(s.upper())


def _extract_grade_from_topic(topic: str) -> Optional[int]:
    """Trích lớp từ topic string AI sinh ra. VD: 'TOÁN 8 — C2.Hằng' → 8"""
    if not topic:
        return None
    m = _RE_GRADE_FROM_TOPIC.search(topic)
    if m:
        g = int(m.group(1))
        if 6 <= g <= 12:
            return g
    return None


def _extract_chapter_no_from_topic(topic: str) -> Optional[int]:
    """Trích số chương từ topic. VD: 'TOÁN 8 — C2.Hằng' → 2"""
    if not topic:
        return None
    m = _RE_CHAPTER_NO.search(topic)
    if m:
        return int(m.group(1))
    return None


def _extract_chapter_no_from_chapter(chapter: str) -> Optional[int]:
    """Trích số chương từ field chapter. VD: 'Chương II. Hằng đẳng thức' → 2"""
    if not chapter:
        return None
    # Try Arabic: "Chương 2" or just "2"
    m = re.search(r'[Cc]hương\s+(\d+)', chapter)
    if m:
        return int(m.group(1))
    # Try Roman: "Chương II"
    m = _RE_CHAPTER_ROMAN.search(chapter)
    if m:
        return _roman_to_int(m.group(1))
    # Try plain number at start
    m = re.match(r'^(\d+)', chapter.strip())
    if m:
        return int(m.group(1))
    return None


def _similarity(a: str, b: str) -> float:
    """Simple string similarity 0-1."""
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class CurriculumMatcher:
    """
    Loads curriculum into memory once, then matches questions fast.
    Call load() before using match_question().
    """

    def __init__(self):
        # Dict: grade → list of curriculum rows
        self._by_grade: dict[int, list] = {}
        self._loaded = False

    async def load(self, db) -> None:
        """Load all curriculum rows into memory (called once per parse job)."""
        from sqlalchemy import select
        from app.db.models.curriculum import Curriculum

        rows = (await db.execute(
            select(Curriculum)
            .where(Curriculum.is_active == True)
            .order_by(Curriculum.grade, Curriculum.chapter_no, Curriculum.lesson_no)
        )).scalars().all()

        self._by_grade = {}
        for row in rows:
            self._by_grade.setdefault(row.grade, []).append(row)

        total = sum(len(v) for v in self._by_grade.values())
        logger.info(f"CurriculumMatcher loaded {total} lessons across grades {sorted(self._by_grade.keys())}")
        self._loaded = True

    def match_question(self, q: dict) -> dict:
        """
        Given a parsed question dict, return updated dict with
        grade, chapter, lesson_title matched to curriculum DB.

        Does NOT mutate the original dict.
        """
        if not self._loaded or not self._by_grade:
            return q

        q = dict(q)  # shallow copy

        # ── Step 1: Resolve grade ──
        grade = q.get("grade")
        topic = q.get("topic", "") or ""

        # AI sometimes puts grade in topic string instead of grade field
        if not grade:
            grade = _extract_grade_from_topic(topic)
        if isinstance(grade, str):
            try:
                grade = int(grade)
            except ValueError:
                grade = None
        if grade and grade not in self._by_grade:
            grade = None  # Unknown grade

        if not grade:
            # Can't match without grade — clear possibly-hallucinated chapter
            q["chapter"] = None
            q["lesson_title"] = None
            return q

        q["grade"] = grade
        lessons = self._by_grade[grade]

        # ── Step 2: Resolve chapter_no ──
        chapter_no = _extract_chapter_no_from_topic(topic)
        if not chapter_no:
            chapter_no = _extract_chapter_no_from_chapter(q.get("chapter", "") or "")

        # ── Step 3: Find best matching lesson ──
        if chapter_no:
            # Filter to this chapter
            chapter_lessons = [l for l in lessons if l.chapter_no == chapter_no]
            if chapter_lessons:
                best = self._best_lesson_match(chapter_lessons, q.get("lesson_title", "") or "")
                q["chapter"] = best.chapter
                q["lesson_title"] = best.lesson_title
                return q

        # ── Step 4: Fuzzy match chapter by text ──
        chapter_text = q.get("chapter", "") or ""
        if chapter_text:
            best_chapter_score = 0.0
            best_chapter_no = None
            # Get unique chapters for this grade
            seen = {}
            for l in lessons:
                if l.chapter_no not in seen:
                    seen[l.chapter_no] = l.chapter
            for cno, cname in seen.items():
                score = _similarity(chapter_text, cname)
                if score > best_chapter_score:
                    best_chapter_score = score
                    best_chapter_no = cno

            if best_chapter_score >= 0.4 and best_chapter_no:
                chapter_lessons = [l for l in lessons if l.chapter_no == best_chapter_no]
                best = self._best_lesson_match(chapter_lessons, q.get("lesson_title", "") or "")
                q["chapter"] = best.chapter
                q["lesson_title"] = best.lesson_title
                return q

        # ── Step 5: Grade only — set chapter to first chapter, clear lesson ──
        # Better to have correct grade than hallucinated chapter
        q["chapter"] = None
        q["lesson_title"] = None
        return q

    def _best_lesson_match(self, lessons: list, lesson_title_hint: str):
        """Find best lesson within a chapter by lesson_title similarity."""
        if len(lessons) == 1:
            return lessons[0]
        if not lesson_title_hint:
            return lessons[0]  # Default to first lesson in chapter

        best = lessons[0]
        best_score = 0.0
        for l in lessons:
            score = _similarity(lesson_title_hint, l.lesson_title)
            if score > best_score:
                best_score = score
                best = l
        return best


# ── Module-level singleton — reused per parse job ──
_matcher = CurriculumMatcher()


async def match_questions_to_curriculum(db, questions: list[dict]) -> list[dict]:
    """
    Public function: load curriculum once, then map all questions.
    Returns new list with updated grade/chapter/lesson_title fields.
    """
    if not questions:
        return questions

    await _matcher.load(db)
    matched = [_matcher.match_question(q) for q in questions]

    # Log stats
    with_chapter = sum(1 for q in matched if q.get("chapter"))
    with_lesson = sum(1 for q in matched if q.get("lesson_title"))
    with_grade = sum(1 for q in matched if q.get("grade"))
    logger.info(
        f"Curriculum matching: {len(matched)} questions → "
        f"grade={with_grade}, chapter={with_chapter}, lesson={with_lesson}"
    )
    return matched