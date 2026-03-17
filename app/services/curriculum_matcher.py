"""
curriculum_matcher.py — Map câu hỏi AI vào đúng bài/chương trong bảng curriculum.

Sau khi AI parse xong, mỗi câu hỏi có:
  - grade: int (6-12), có thể None
  - chapter: str (AI tự sinh, format không chuẩn, ví dụ "C6.Hàm bậc hai")
  - topic: str (AI tự sinh, ví dụ "TOÁN 10 — C6.Hàm bậc hai")
  - lesson_title: str (AI tự sinh)

Bảng curriculum có dữ liệu chuẩn:
  - grade, chapter_no, chapter, lesson_no, lesson_title

Chiến lược match (theo thứ tự ưu tiên):
  1. Grade + chapter_no từ topic/chapter (kết hợp kiểm tra text similarity)
     → Nếu chapter_no khớp NHƯNG text không khớp (sách khác) → bỏ qua, sang bước 2
  2. Grade + fuzzy text match (so sánh phần text đã strip prefix)
     → Ngưỡng 0.5 để tránh gán sai
  3. Grade only → xóa chapter/lesson để không gán sai
"""

import re
import logging
from typing import Optional
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Pre-compiled patterns
_RE_GRADE_FROM_TOPIC = re.compile(r'[Tt][Oo][Áá][Nn]\s*(\d{1,2})', re.UNICODE)
_RE_CHAPTER_NO_FROM_TOPIC = re.compile(r'—\s*C(\d{1,2})[.\s]')   # "— C6.Name" or "— C6 Name"
_RE_CHAPTER_NO_ALT = re.compile(r'\bC(\d{1,2})\.')                 # "C6.Name" anywhere
_RE_CHAPTER_TEXT_FROM_TOPIC = re.compile(r'C\d{1,2}\.(.+)')        # "C6.Hàm bậc hai" → "Hàm bậc hai"
_RE_CHAPTER_ROMAN = re.compile(r'\bChương\s+([IVX]+)[.\s]', re.IGNORECASE)
_RE_STRIP_CHAPTER_PREFIX = re.compile(
    r'^Chương\s+(?:[IVX]+|\d+)[.\s]+', re.IGNORECASE | re.UNICODE
)

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


def _extract_chapter_no(topic: str, chapter: str) -> Optional[int]:
    """
    Trích số chương từ topic hoặc chapter.
    Ưu tiên "— C6." format trong topic (chắc chắn hơn).
    """
    # Try "— C6." in topic first (most reliable)
    if topic:
        m = _RE_CHAPTER_NO_FROM_TOPIC.search(topic)
        if m:
            return int(m.group(1))
        # Try "C6." anywhere in topic
        m = _RE_CHAPTER_NO_ALT.search(topic)
        if m:
            return int(m.group(1))

    if chapter:
        # Try "Chương 6" or "Chương VI"
        m = re.search(r'[Cc]hương\s+(\d+)', chapter)
        if m:
            return int(m.group(1))
        m = _RE_CHAPTER_ROMAN.search(chapter)
        if m:
            return _roman_to_int(m.group(1))
        # Try "C6." prefix
        m = _RE_CHAPTER_NO_ALT.match(chapter.strip())
        if m:
            return int(m.group(1))
        # Try plain number at start
        m = re.match(r'^(\d+)', chapter.strip())
        if m:
            n = int(m.group(1))
            if 1 <= n <= 15:
                return n

    return None


def _extract_chapter_text(topic: str, chapter: str) -> str:
    """
    Trích phần text có nghĩa từ chapter field của AI.
    "C6.Hàm bậc hai"             → "Hàm bậc hai"
    "TOÁN 10 — C7.Tọa độ phẳng" → "Tọa độ phẳng"  (từ topic)
    "Chương VII. Biểu thức đại số" → "Biểu thức đại số"
    "Đa thức"                    → "Đa thức"
    """
    # Try topic "C6.Text" first
    if topic:
        m = _RE_CHAPTER_TEXT_FROM_TOPIC.search(topic)
        if m:
            return m.group(1).strip()

    if not chapter:
        return ""

    chapter = chapter.strip()

    # "C6.Text" format
    m = _RE_CHAPTER_NO_ALT.match(chapter)
    if m:
        return chapter[m.end():].strip()

    # "Chương X. Text" format
    stripped = _RE_STRIP_CHAPTER_PREFIX.sub('', chapter).strip()
    if stripped != chapter:
        return stripped

    return chapter


def _strip_db_chapter(name: str) -> str:
    """Bỏ prefix 'Chương X.' khỏi tên chương DB để so sánh text thuần túy."""
    if not name:
        return ""
    return _RE_STRIP_CHAPTER_PREFIX.sub('', name.strip()).strip()


def _similarity(a: str, b: str) -> float:
    """Simple string similarity 0-1."""
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _best_chapter_score(chapter_hint: str, db_chapter: str, db_chapter_stripped: str) -> float:
    """So sánh chapter hint với DB chapter theo cả dạng đầy đủ và stripped."""
    if not chapter_hint:
        return 0.0
    return max(
        _similarity(chapter_hint, db_chapter),
        _similarity(chapter_hint, db_chapter_stripped),
    )


class CurriculumMatcher:
    """
    Loads curriculum into memory once per parse job, then matches questions fast.
    Call load() before using match_question().
    """

    def __init__(self):
        self._by_grade: dict[int, list] = {}
        self._loaded = False

    async def load(self, db) -> None:
        """Load all active curriculum rows into memory."""
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
        chapter_raw = q.get("chapter", "") or ""

        if isinstance(grade, str):
            try:
                grade = int(grade)
            except ValueError:
                grade = None
        if grade and grade not in self._by_grade:
            grade = None

        if not grade:
            q["chapter"] = None
            q["lesson_title"] = None
            return q

        q["grade"] = grade
        lessons = self._by_grade[grade]

        # Pre-build unique chapters cache for this grade
        chapters_cache: dict[int, tuple] = {}  # chapter_no → (full_name, stripped_name)
        for l in lessons:
            if l.chapter_no not in chapters_cache:
                chapters_cache[l.chapter_no] = (l.chapter, _strip_db_chapter(l.chapter))

        # Trích chapter text và chapter_no từ AI output
        chapter_text = _extract_chapter_text("", chapter_raw)  # phần text có nghĩa
        chapter_no = _extract_chapter_no("", chapter_raw)

        # ── Step 2: chapter_no match + xác nhận text ──
        # Chỉ tin chapter_no nếu text của AI khớp với text DB (tránh sách khác có số chương khác)
        if chapter_no and chapter_no in chapters_cache:
            db_full, db_stripped = chapters_cache[chapter_no]
            text_score = _best_chapter_score(chapter_text, db_full, db_stripped)

            # Chấp nhận nếu:
            #  - Không có chapter text để verify (chỉ có số chương) → trust chapter_no
            #  - Hoặc text đủ giống (>= 0.35)
            if not chapter_text or text_score >= 0.35:
                chapter_lessons = [l for l in lessons if l.chapter_no == chapter_no]
                best = self._best_lesson_match(chapter_lessons, q.get("lesson_title", "") or "")
                q["chapter"] = best.chapter
                q["lesson_title"] = best.lesson_title
                logger.debug(f"Matched via chapter_no={chapter_no} (score={text_score:.2f}): {best.chapter}")
                return q
            else:
                logger.debug(
                    f"chapter_no={chapter_no} rejected: AI='{chapter_text}' DB='{db_stripped}' score={text_score:.2f}"
                )

        # ── Step 3: Fuzzy text match ──
        # Sử dụng phần text đã strip để so sánh hiệu quả hơn
        if chapter_text:
            best_score = 0.0
            best_no = None

            for cno, (cname, cname_stripped) in chapters_cache.items():
                score = _best_chapter_score(chapter_text, cname, cname_stripped)
                if score > best_score:
                    best_score = score
                    best_no = cno

            # Ngưỡng 0.5: chỉ gán khi đủ tự tin, tránh gán sai tên chương
            if best_score >= 0.5 and best_no is not None:
                chapter_lessons = [l for l in lessons if l.chapter_no == best_no]
                best = self._best_lesson_match(chapter_lessons, q.get("lesson_title", "") or "")
                q["chapter"] = best.chapter
                q["lesson_title"] = best.lesson_title
                logger.debug(f"Matched via fuzzy text (score={best_score:.2f}): {best.chapter}")
                return q
            else:
                logger.debug(
                    f"No chapter match for grade={grade} text='{chapter_text}' (best={best_score:.2f})"
                )

        # ── Step 4: Grade only — không gán sai chương ──
        q["chapter"] = None
        q["lesson_title"] = None
        return q

    def _best_lesson_match(self, lessons: list, lesson_title_hint: str):
        """
        Tìm bài học tốt nhất trong chương dựa trên lesson_title similarity.
        - Nếu chỉ có 1 bài → trả về ngay
        - Nếu similarity tốt nhất >= 0.3 → trả bài đó
        - Ngược lại → lesson_title=None (không gán sai bài)
        """
        if len(lessons) == 1:
            return lessons[0]

        if not lesson_title_hint:
            # Không có gợi ý → không gán bài cụ thể
            return _LessonPlaceholder(lessons[0].chapter)

        best = lessons[0]
        best_score = 0.0
        for l in lessons:
            score = _similarity(lesson_title_hint, l.lesson_title)
            if score > best_score:
                best_score = score
                best = l

        if best_score >= 0.3:
            return best
        else:
            # Similarity quá thấp → không gán sai bài
            return _LessonPlaceholder(lessons[0].chapter)


class _LessonPlaceholder:
    """Giữ chapter nhưng để lesson_title là None khi không match được bài."""
    def __init__(self, chapter: str):
        self.chapter = chapter
        self.lesson_title = None


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
