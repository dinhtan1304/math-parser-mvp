"""
similarity_detector.py — Phát hiện câu hỏi tương tự trong ngân hàng sau khi upload.

v3: Uses pgvector <=> operator on PostgreSQL for fast similarity matrix.
    Falls back to numpy on SQLite.
"""

import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.82
MAX_SIMILAR_PER_QUESTION = 3


async def ensure_similarity_table(engine: AsyncEngine) -> None:
    """Tạo bảng lưu kết quả similarity — gọi lúc startup."""
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
    """Tìm câu tương tự cho tất cả câu trong exam_id.

    v3: Uses pgvector <=> operator on PostgreSQL — no need to load all
    embeddings into memory. On SQLite, falls back to numpy.
    """
    from app.core.config import settings as _settings
    _is_pg = "postgresql" in _settings.DATABASE_URL or "postgres" in _settings.DATABASE_URL

    if _is_pg:
        return await _detect_pgvector(db, exam_id, user_id)
    else:
        return await _detect_numpy(db, exam_id, user_id)


async def _detect_pgvector(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
) -> int:
    """pgvector: cross-join with <=> cosine distance, done entirely in SQL."""
    # Find all similar pairs in one query using pgvector
    result = await db.execute(text("""
        SELECT
            new_q.question_id AS new_id,
            bank_q.question_id AS bank_id,
            1 - (new_q.embedding <=> bank_q.embedding) AS score
        FROM question_embedding new_q
        JOIN question q_new ON q_new.id = new_q.question_id
        CROSS JOIN LATERAL (
            SELECT
                bank.question_id,
                bank.embedding
            FROM question_embedding bank
            JOIN question q_bank ON q_bank.id = bank.question_id
            WHERE bank.user_id = :uid
              AND q_bank.exam_id != :eid
              AND q_bank.exam_id IS NOT NULL
              AND (new_q.embedding <=> bank.embedding) <= :max_dist
            ORDER BY new_q.embedding <=> bank.embedding
            LIMIT :max_per
        ) bank_q
        WHERE q_new.exam_id = :eid
    """), {
        "uid": user_id,
        "eid": exam_id,
        "max_dist": 1.0 - SIMILARITY_THRESHOLD,
        "max_per": MAX_SIMILAR_PER_QUESTION,
    })
    rows = result.fetchall()

    if not rows:
        logger.info(f"Exam {exam_id}: no similar questions (pgvector, threshold={SIMILARITY_THRESHOLD})")
        return 0

    # Upsert pairs
    inserted = 0
    for new_id, bank_id, score in rows:
        try:
            await db.execute(text("""
                INSERT INTO question_similarity (question_id, similar_id, score)
                VALUES (:qid, :sid, :score)
                ON CONFLICT (question_id, similar_id) DO UPDATE SET score = EXCLUDED.score
            """), {"qid": new_id, "sid": bank_id, "score": round(float(score), 4)})
            inserted += 1
        except Exception as e:
            logger.debug(f"Similarity insert skipped: {e}")

    try:
        await db.commit()
    except Exception as e:
        logger.warning(f"Similarity commit failed: {e}")
        await db.rollback()

    logger.info(f"Exam {exam_id}: {inserted} similar pairs found (pgvector)")
    return inserted


async def _detect_numpy(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
) -> int:
    """SQLite fallback: load embeddings + numpy matrix multiplication."""
    import numpy as np

    # Lấy embeddings của câu mới (trong exam này)
    new_rows = (await db.execute(text("""
        SELECT qe.question_id, qe.embedding
        FROM question_embedding qe
        JOIN question q ON q.id = qe.question_id
        WHERE q.exam_id = :eid
    """), {"eid": exam_id})).fetchall()

    if not new_rows:
        return 0

    new_ids = []
    new_embs = []
    for qid, emb_json in new_rows:
        try:
            new_embs.append(json.loads(emb_json))
            new_ids.append(qid)
        except Exception:
            continue

    if not new_embs:
        return 0

    # Lấy embeddings của bank (trừ exam này)
    bank_rows = (await db.execute(text("""
        SELECT qe.question_id, qe.embedding
        FROM question_embedding qe
        JOIN question q ON q.id = qe.question_id
        WHERE qe.user_id = :uid
          AND q.exam_id != :eid
          AND q.exam_id IS NOT NULL
    """), {"uid": user_id, "eid": exam_id})).fetchall()

    if not bank_rows:
        return 0

    bank_ids = []
    bank_embs = []
    for row in bank_rows:
        try:
            bank_embs.append(json.loads(row[1]))
            bank_ids.append(row[0])
        except Exception:
            continue

    if not bank_embs:
        return 0

    # Vectorized cosine similarity
    new_matrix = np.array(new_embs, dtype=np.float32)
    bank_matrix = np.array(bank_embs, dtype=np.float32)

    new_norms = np.linalg.norm(new_matrix, axis=1, keepdims=True)
    bank_norms = np.linalg.norm(bank_matrix, axis=1, keepdims=True)
    new_norms = np.where(new_norms == 0, 1e-10, new_norms)
    bank_norms = np.where(bank_norms == 0, 1e-10, bank_norms)

    sim_matrix = (new_matrix / new_norms) @ (bank_matrix / bank_norms).T

    # Collect pairs above threshold
    pairs = []
    for i, new_qid in enumerate(new_ids):
        row_scores = sim_matrix[i]
        above = np.where(row_scores >= SIMILARITY_THRESHOLD)[0]
        if len(above) == 0:
            continue
        top_k = above[np.argsort(row_scores[above])[::-1]][:MAX_SIMILAR_PER_QUESTION]
        for j in top_k:
            pairs.append((new_qid, bank_ids[j], float(row_scores[j])))

    if not pairs:
        logger.info(f"Exam {exam_id}: no similar questions (numpy, threshold={SIMILARITY_THRESHOLD})")
        return 0

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

    logger.info(f"Exam {exam_id}: {inserted} similar pairs (numpy)")
    return inserted


async def find_user_duplicates(
    db: AsyncSession,
    user_id: int,
    threshold: float = 0.85,
    max_per_question: int = 10,
) -> list[tuple[int, int, float]]:
    """Find all duplicate pairs across ALL of a user's questions using embeddings.

    Returns list of (question_id, similar_id, score) tuples.
    This computes on-the-fly — no dependency on question_similarity table.
    """
    from app.core.config import settings as _settings
    _is_pg = "postgresql" in _settings.DATABASE_URL or "postgres" in _settings.DATABASE_URL

    if _is_pg:
        return await _find_duplicates_pgvector(db, user_id, threshold, max_per_question)
    else:
        return await _find_duplicates_numpy(db, user_id, threshold, max_per_question)


async def _find_duplicates_pgvector(
    db: AsyncSession,
    user_id: int,
    threshold: float,
    max_per: int,
) -> list[tuple[int, int, float]]:
    """pgvector: self-join on all user embeddings to find duplicate pairs."""
    max_dist = 1.0 - threshold
    result = await db.execute(text("""
        SELECT
            a.question_id AS q1_id,
            b_q.question_id AS q2_id,
            1 - (a.embedding <=> b_q.embedding) AS score
        FROM question_embedding a
        CROSS JOIN LATERAL (
            SELECT b.question_id, b.embedding
            FROM question_embedding b
            WHERE b.user_id = :uid
              AND b.question_id > a.question_id
              AND (a.embedding <=> b.embedding) <= :max_dist
            ORDER BY a.embedding <=> b.embedding
            LIMIT :max_per
        ) b_q
        WHERE a.user_id = :uid
        ORDER BY score DESC
    """), {"uid": user_id, "max_dist": max_dist, "max_per": max_per})
    return [(int(r[0]), int(r[1]), float(r[2])) for r in result.fetchall()]


async def _find_duplicates_numpy(
    db: AsyncSession,
    user_id: int,
    threshold: float,
    max_per: int,
) -> list[tuple[int, int, float]]:
    """SQLite fallback: load all user embeddings and compute cosine similarity."""
    import numpy as np

    rows = (await db.execute(text("""
        SELECT question_id, embedding
        FROM question_embedding
        WHERE user_id = :uid
    """), {"uid": user_id})).fetchall()

    if len(rows) < 2:
        return []

    ids = []
    embs = []
    for qid, emb_json in rows:
        try:
            embs.append(json.loads(emb_json))
            ids.append(qid)
        except Exception:
            continue

    if len(embs) < 2:
        return []

    matrix = np.array(embs, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1e-10, norms)
    normed = matrix / norms
    sim_matrix = normed @ normed.T

    # Zero out diagonal and lower triangle (avoid self-matches and duplicates)
    np.fill_diagonal(sim_matrix, 0)
    sim_matrix = np.triu(sim_matrix)

    pairs = []
    for i in range(len(ids)):
        row = sim_matrix[i]
        above = np.where(row >= threshold)[0]
        if len(above) == 0:
            continue
        top_k = above[np.argsort(row[above])[::-1]][:max_per]
        for j in top_k:
            pairs.append((ids[i], ids[j], float(row[j])))

    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


async def find_all_duplicates(
    db: AsyncSession,
    threshold: float = 0.85,
    max_per_question: int = 10,
) -> list[tuple[int, int, float]]:
    """Find duplicate pairs across ALL users (admin). Same logic, no user_id filter."""
    from app.core.config import settings as _settings
    _is_pg = "postgresql" in _settings.DATABASE_URL or "postgres" in _settings.DATABASE_URL

    if _is_pg:
        max_dist = 1.0 - threshold
        result = await db.execute(text("""
            SELECT
                a.question_id AS q1_id,
                b_q.question_id AS q2_id,
                1 - (a.embedding <=> b_q.embedding) AS score
            FROM question_embedding a
            CROSS JOIN LATERAL (
                SELECT b.question_id, b.embedding
                FROM question_embedding b
                WHERE b.question_id > a.question_id
                  AND (a.embedding <=> b.embedding) <= :max_dist
                ORDER BY a.embedding <=> b.embedding
                LIMIT :max_per
            ) b_q
            ORDER BY score DESC
        """), {"max_dist": max_dist, "max_per": max_per_question})
        return [(int(r[0]), int(r[1]), float(r[2])) for r in result.fetchall()]
    else:
        # SQLite fallback: load all embeddings
        import numpy as np
        rows = (await db.execute(text(
            "SELECT question_id, embedding FROM question_embedding"
        ))).fetchall()
        if len(rows) < 2:
            return []
        ids, embs = [], []
        for qid, emb_json in rows:
            try:
                embs.append(json.loads(emb_json))
                ids.append(qid)
            except Exception:
                continue
        if len(embs) < 2:
            return []
        matrix = np.array(embs, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1e-10, norms)
        normed = matrix / norms
        sim_matrix = normed @ normed.T
        np.fill_diagonal(sim_matrix, 0)
        sim_matrix = np.triu(sim_matrix)
        pairs = []
        for i in range(len(ids)):
            row = sim_matrix[i]
            above = np.where(row >= threshold)[0]
            if len(above) == 0:
                continue
            top_k = above[np.argsort(row[above])[::-1]][:max_per_question]
            for j in top_k:
                pairs.append((ids[i], ids[j], float(row[j])))
        pairs.sort(key=lambda x: x[2], reverse=True)
        return pairs


async def get_exam_similarities(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
) -> list[dict]:
    """Trả về danh sách gợi ý câu tương tự cho một đề."""
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