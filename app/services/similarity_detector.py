"""
similarity_detector.py — Phát hiện câu hỏi tương tự trong ngân hàng sau khi upload.

Chạy background sau khi embed xong. Với mỗi câu mới trong đề vừa upload,
tìm các câu tương tự đã có trong bank (từ các đề khác).

Kết quả lưu vào bảng question_similarity:
  (new_question_id, similar_question_id, similarity_score)

FE gọi GET /parser/exams/{exam_id}/similar để lấy gợi ý.
"""

import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)

# Ngưỡng tương tự — trên 0.82 mới tính là "đáng chú ý"
SIMILARITY_THRESHOLD = 0.82
# Tối đa 3 câu tương tự cho mỗi câu mới
MAX_SIMILAR_PER_QUESTION = 3


async def ensure_similarity_table(engine: AsyncEngine) -> None:
    """Tạo bảng lưu kết quả similarity — gọi lúc startup.

    Tương thích cả SQLite (dev) và PostgreSQL (production):
    - SQLite: INTEGER PRIMARY KEY AUTOINCREMENT, TEXT timestamp
    - PostgreSQL: SERIAL PRIMARY KEY, TIMESTAMPTZ
    """
    from app.core.config import settings as _settings
    _is_pg = (
        "postgresql" in _settings.DATABASE_URL
        or "postgres" in _settings.DATABASE_URL
    )

    if _is_pg:
        id_col = "id SERIAL PRIMARY KEY"
        ts_col = "created_at TIMESTAMPTZ DEFAULT NOW()"
    else:
        id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        ts_col = "created_at TEXT DEFAULT (datetime('now'))"

    async with engine.begin() as conn:
        await conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS question_similarity (
                {id_col},
                question_id INTEGER NOT NULL REFERENCES question(id) ON DELETE CASCADE,
                similar_id  INTEGER NOT NULL REFERENCES question(id) ON DELETE CASCADE,
                score       REAL NOT NULL,
                {ts_col},
                UNIQUE (question_id, similar_id)
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_qsim_question
            ON question_similarity(question_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_qsim_similar
            ON question_similarity(similar_id)
        """))
    logger.info("question_similarity table ready (%s)", "postgresql" if _is_pg else "sqlite")


async def detect_similar_for_exam(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
) -> int:
    """
    Với tất cả câu hỏi trong exam_id, tìm câu tương tự trong bank.
    Lưu vào question_similarity. Trả về số cặp tìm được.
    """
    import numpy as np

    # ── Lấy embedding của các câu mới (thuộc exam này) ──
    new_rows = (await db.execute(text("""
        SELECT qe.question_id, qe.embedding
        FROM question_embedding qe
        JOIN question q ON q.id = qe.question_id
        WHERE q.exam_id = :eid
    """), {"eid": exam_id})).fetchall()

    if not new_rows:
        logger.debug(f"Exam {exam_id}: no embeddings yet, skip similarity")
        return 0

    new_ids = [r[0] for r in new_rows]
    new_embs = []
    valid_new_ids = []
    for qid, emb_json in new_rows:
        try:
            new_embs.append(json.loads(emb_json))
            valid_new_ids.append(qid)
        except Exception:
            continue

    if not new_embs:
        return 0

    # ── Lấy embedding của toàn bộ bank (trừ câu trong exam này) ──
    bank_rows = (await db.execute(text("""
        SELECT qe.question_id, qe.embedding,
               q.question_text, q.topic, q.difficulty, q.grade, q.exam_id
        FROM question_embedding qe
        JOIN question q ON q.id = qe.question_id
        WHERE qe.user_id = :uid
          AND q.exam_id != :eid
          AND q.exam_id IS NOT NULL
    """), {"uid": user_id, "eid": exam_id})).fetchall()

    if not bank_rows:
        logger.debug(f"Exam {exam_id}: bank empty, skip similarity")
        return 0

    bank_ids = []
    bank_embs = []
    bank_meta = []
    for row in bank_rows:
        try:
            bank_embs.append(json.loads(row[1]))
            bank_ids.append(row[0])
            bank_meta.append({
                "id": row[0],
                "question_text": row[2],
                "topic": row[3],
                "difficulty": row[4],
                "grade": row[5],
                "exam_id": row[6],
            })
        except Exception:
            continue

    if not bank_embs:
        return 0

    # ── Vectorized cosine similarity: new (M×D) vs bank (N×D) ──
    new_matrix  = np.array(new_embs,  dtype=np.float32)   # M × D
    bank_matrix = np.array(bank_embs, dtype=np.float32)   # N × D

    # Normalize
    new_norms  = np.linalg.norm(new_matrix,  axis=1, keepdims=True)
    bank_norms = np.linalg.norm(bank_matrix, axis=1, keepdims=True)
    new_norms  = np.where(new_norms  == 0, 1e-10, new_norms)
    bank_norms = np.where(bank_norms == 0, 1e-10, bank_norms)

    new_norm  = new_matrix  / new_norms   # M × D
    bank_norm = bank_matrix / bank_norms  # N × D

    # M × N similarity matrix
    sim_matrix = new_norm @ bank_norm.T

    # ── Collect pairs above threshold ──
    pairs = []
    for i, new_qid in enumerate(valid_new_ids):
        row_scores = sim_matrix[i]  # N scores
        above = np.where(row_scores >= SIMILARITY_THRESHOLD)[0]
        if len(above) == 0:
            continue
        # Sort by score descending, take top K
        top_k = above[np.argsort(row_scores[above])[::-1]][:MAX_SIMILAR_PER_QUESTION]
        for j in top_k:
            pairs.append((new_qid, bank_ids[j], float(row_scores[j])))

    if not pairs:
        logger.info(f"Exam {exam_id}: no similar questions found (threshold={SIMILARITY_THRESHOLD})")
        return 0

    # ── Upsert pairs vào DB ──
    inserted = 0
    for new_qid, sim_qid, score in pairs:
        try:
            await db.execute(text("""
                INSERT INTO question_similarity (question_id, similar_id, score)
                VALUES (:qid, :sid, :score)
                ON CONFLICT (question_id, similar_id) DO UPDATE SET score = EXCLUDED.score
            """), {"qid": new_qid, "sid": sim_qid, "score": round(score, 4)})
            inserted += 1
        except Exception as e:
            logger.debug(f"Similarity insert skipped: {e}")

    try:
        await db.commit()
    except Exception as e:
        logger.warning(f"Similarity commit failed: {e}")
        await db.rollback()

    logger.info(
        f"Exam {exam_id}: {inserted} similar pairs found "
        f"({len(valid_new_ids)} new vs {len(bank_ids)} bank questions)"
    )
    return inserted


async def get_exam_similarities(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
) -> list[dict]:
    """
    Trả về danh sách gợi ý câu tương tự cho một đề.
    Grouped theo câu mới — mỗi câu mới có list các câu bank tương tự.
    """
    rows = (await db.execute(text("""
        SELECT
            qs.question_id   AS new_id,
            qn.question_text AS new_text,
            qn.topic         AS new_topic,
            qn.difficulty    AS new_diff,
            qn.grade         AS new_grade,
            qs.similar_id    AS sim_id,
            qb.question_text AS sim_text,
            qb.topic         AS sim_topic,
            qb.difficulty    AS sim_diff,
            qb.grade         AS sim_grade,
            qb.exam_id       AS sim_exam_id,
            qs.score
        FROM question_similarity qs
        JOIN question qn ON qn.id = qs.question_id
        JOIN question qb ON qb.id = qs.similar_id
        WHERE qn.exam_id = :eid
          AND qn.user_id = :uid
        ORDER BY qs.question_id, qs.score DESC
    """), {"eid": exam_id, "uid": user_id})).fetchall()

    if not rows:
        return []

    # Group by new question
    grouped: dict[int, dict] = {}
    for r in rows:
        new_id = r[0]
        if new_id not in grouped:
            grouped[new_id] = {
                "question_id": new_id,
                "question_text": (r[1] or "")[:200],
                "topic": r[2] or "",
                "difficulty": r[3] or "",
                "grade": r[4],
                "similar": [],
            }
        grouped[new_id]["similar"].append({
            "id": r[5],
            "question_text": (r[6] or "")[:200],
            "topic": r[7] or "",
            "difficulty": r[8] or "",
            "grade": r[9],
            "exam_id": r[10],
            "score": r[11],
        })

    return list(grouped.values())