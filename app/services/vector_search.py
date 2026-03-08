"""Vector Similarity Search for Question Bank.

v3 — pgvector native:
  - VECTOR(768) column type instead of TEXT JSON
  - Cosine distance operator <=> instead of numpy
  - HNSW index for fast approximate nearest neighbor
  - Enriched embedding text (grade + chapter + topic + difficulty)
  - Hash-based cache key (no collision from truncation)
  - Fallback to numpy for SQLite (dev)

Migration from v2:
  - Old: question_embedding.embedding = TEXT (JSON string)
  - New: question_embedding.embedding = VECTOR(768)
  - Auto-migration on startup converts existing JSON→VECTOR
"""

import os
import json
import asyncio
import hashlib
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)

# ── Constants ──
EMBEDDING_DIM = 768  # text-embedding-004 output dimension

# ── Embedding concurrency limit ──
_EMBED_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_embed_semaphore() -> asyncio.Semaphore:
    global _EMBED_SEMAPHORE
    if _EMBED_SEMAPHORE is None:
        _EMBED_SEMAPHORE = asyncio.Semaphore(5)
    return _EMBED_SEMAPHORE


def _is_postgres() -> bool:
    from app.core.config import settings as _settings
    return "postgresql" in _settings.DATABASE_URL or "postgres" in _settings.DATABASE_URL


# ========== INIT ==========

async def init_vector_table(engine: AsyncEngine):
    """Create embedding storage table with pgvector. Call once on startup."""
    is_pg = _is_postgres()

    async with engine.begin() as conn:
        if is_pg:
            # Enable pgvector extension (Neon supports this natively)
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                logger.info("pgvector extension enabled")
            except Exception as e:
                logger.warning(f"pgvector extension failed: {e}")

            # Check if old TEXT-based table exists
            old_type = await _check_embedding_column_type(conn)

            if old_type == "text_based":
                await _migrate_text_to_vector(conn)
            elif old_type == "none":
                await conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS question_embedding (
                        question_id INTEGER PRIMARY KEY
                            REFERENCES question(id) ON DELETE CASCADE,
                        user_id INTEGER NOT NULL,
                        topic TEXT,
                        difficulty TEXT,
                        grade INTEGER,
                        chapter TEXT,
                        embedding vector({EMBEDDING_DIM}) NOT NULL
                    )
                """))
            # else: vector_based — already correct

            # Add missing columns if upgrading from old schema
            for col, typedef in [("grade", "INTEGER"), ("chapter", "TEXT")]:
                try:
                    await conn.execute(text(
                        f"ALTER TABLE question_embedding ADD COLUMN {col} {typedef}"
                    ))
                    logger.info(f"Added question_embedding.{col}")
                except Exception:
                    pass  # Already exists

            # Indexes
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_qemb_user ON question_embedding(user_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_qemb_user_topic ON question_embedding(user_id, topic)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_qemb_user_grade ON question_embedding(user_id, grade)"
            ))

            # HNSW index — only if enough rows
            try:
                count = (await conn.execute(
                    text("SELECT COUNT(*) FROM question_embedding")
                )).scalar() or 0
                if count >= 100:
                    await conn.execute(text(f"""
                        CREATE INDEX IF NOT EXISTS ix_qemb_hnsw
                        ON question_embedding
                        USING hnsw (embedding vector_cosine_ops)
                        WITH (m = 16, ef_construction = 64)
                    """))
                    logger.info(f"HNSW index created/verified ({count} embeddings)")
            except Exception as e:
                logger.debug(f"HNSW index skipped: {e}")

        else:
            # SQLite fallback
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS question_embedding (
                    question_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    topic TEXT,
                    difficulty TEXT,
                    grade INTEGER,
                    chapter TEXT,
                    embedding TEXT NOT NULL,
                    FOREIGN KEY (question_id) REFERENCES question(id) ON DELETE CASCADE
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_qemb_user ON question_embedding(user_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_qemb_user_topic ON question_embedding(user_id, topic)"
            ))

    logger.info("Vector embedding table initialized")


async def _check_embedding_column_type(conn) -> str:
    """Check if question_embedding exists and what type embedding column is."""
    try:
        result = await conn.execute(text("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = 'question_embedding'
              AND column_name = 'embedding'
        """))
        row = result.fetchone()
        if row is None:
            return "none"
        dtype = (row[0] or "").lower()
        if dtype == "text":
            return "text_based"
        if "user-defined" in dtype or "vector" in dtype:
            return "vector_based"
        return "unknown"
    except Exception:
        return "none"


async def _migrate_text_to_vector(conn):
    """Migrate old TEXT JSON embeddings → VECTOR(768) column."""
    logger.info("Migrating question_embedding: TEXT → VECTOR...")
    try:
        await conn.execute(text(
            "ALTER TABLE question_embedding RENAME TO _question_embedding_old"
        ))

        await conn.execute(text(f"""
            CREATE TABLE question_embedding (
                question_id INTEGER PRIMARY KEY
                    REFERENCES question(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                topic TEXT,
                difficulty TEXT,
                grade INTEGER,
                chapter TEXT,
                embedding vector({EMBEDDING_DIM}) NOT NULL
            )
        """))

        # pgvector accepts '[1,2,3,...]' string format directly via ::vector cast
        await conn.execute(text("""
            INSERT INTO question_embedding
                (question_id, user_id, topic, difficulty, embedding)
            SELECT
                question_id, user_id, topic, difficulty,
                embedding::vector
            FROM _question_embedding_old
            WHERE embedding IS NOT NULL
              AND LENGTH(embedding) > 10
        """))

        # Backfill grade/chapter from question table
        await conn.execute(text("""
            UPDATE question_embedding qe
            SET grade = q.grade, chapter = q.chapter
            FROM question q
            WHERE qe.question_id = q.id
        """))

        await conn.execute(text("DROP TABLE _question_embedding_old"))
        logger.info("Migration complete: TEXT → VECTOR")

    except Exception as e:
        logger.error(f"Migration failed, rolling back: {e}")
        try:
            await conn.execute(text("DROP TABLE IF EXISTS question_embedding"))
            await conn.execute(text(
                "ALTER TABLE _question_embedding_old RENAME TO question_embedding"
            ))
        except Exception:
            pass
        raise


# ========== EMBEDDING GENERATION ==========

_embedding_client = None


def _get_client():
    """Lazy-init Google GenAI client for embeddings."""
    global _embedding_client
    if _embedding_client is None:
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            logger.warning("No GOOGLE_API_KEY for embeddings")
            return None
        try:
            from google import genai
            _embedding_client = genai.Client(api_key=api_key)
            logger.info("Embedding client initialized")
        except Exception as e:
            logger.error(f"Embedding client init failed: {e}")
            return None
    return _embedding_client


# ── Cache: hash-based to avoid collision ──
_embedding_cache: dict[str, list[float]] = {}
_MAX_CACHE_SIZE = 1000


def _cache_key(text_content: str) -> str:
    """Hash-based cache key — no collision from truncation."""
    return hashlib.md5(text_content.encode("utf-8")).hexdigest()


_EMBED_MODELS = [
    "gemini-embedding-001",     # Full path format
]
_working_embed_model: Optional[str] = None


async def _generate_embedding(text_content: str) -> Optional[list[float]]:
    """Generate embedding vector with hash-based cache + concurrency limit."""
    global _working_embed_model
    key = _cache_key(text_content)

    if key in _embedding_cache:
        return _embedding_cache[key]

    client = _get_client()
    if not client:
        return None

    sem = _get_embed_semaphore()
    async with sem:
        # If we already found a working model, try it first; fall back to all models
        models = [_working_embed_model] + [m for m in _EMBED_MODELS if m != _working_embed_model] \
                 if _working_embed_model else list(_EMBED_MODELS)

        for model_name in models:
            try:
                # Force 768 dims — our DB column is vector(768)
                try:
                    result = await client.aio.models.embed_content(
                        model=model_name,
                        contents=text_content[:2000],
                        config={"output_dimensionality": EMBEDDING_DIM},
                    )
                except TypeError:
                    # Older SDK version may not support config param
                    result = await client.aio.models.embed_content(
                        model=model_name,
                        contents=text_content[:2000],
                    )
                emb = None
                if result and hasattr(result, "embeddings") and result.embeddings:
                    emb = result.embeddings[0].values
                elif result and hasattr(result, "embedding") and result.embedding:
                    emb = result.embedding.values

                if emb is not None:
                    # Ensure correct dimensions for DB column
                    if len(emb) != EMBEDDING_DIM:
                        if len(emb) > EMBEDDING_DIM:
                            emb = emb[:EMBEDDING_DIM]  # Truncate
                        else:
                            emb = list(emb) + [0.0] * (EMBEDDING_DIM - len(emb))  # Pad
                    if len(_embedding_cache) < _MAX_CACHE_SIZE:
                        _embedding_cache[key] = emb
                    if _working_embed_model != model_name:
                        _working_embed_model = model_name
                        logger.info(f"Embedding model locked: {model_name}")
                    return emb
            except Exception as e:
                if "not found" in str(e).lower() or "404" in str(e):
                    logger.debug(f"Embedding model {model_name} not available, trying next...")
                    continue
                logger.warning(f"Embedding generation failed ({model_name}): {e}")
                return None

        logger.warning("All embedding models failed — check GOOGLE_API_KEY permissions")

    return None


async def _generate_embeddings_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Generate embeddings for multiple texts in parallel."""
    tasks = [_generate_embedding(t) for t in texts]
    return await asyncio.gather(*tasks)


# ========== ENRICHED TEXT FOR EMBEDDING ==========

def enrich_text_for_embedding(
    question_text: str,
    topic: str = "",
    grade: int = None,
    chapter: str = "",
    difficulty: str = "",
) -> str:
    """Build rich text for embedding — includes metadata context.

    v3 improvement: Instead of "topic: question_text", includes grade/chapter/difficulty
    so embedding captures the full educational context.

    Example: "Toán 10 | Hệ thức lượng tam giác | C3 | TH: Cho tam giác ABC..."
    """
    parts = []
    if grade:
        parts.append(f"Toán {grade}")
    if chapter:
        parts.append(chapter[:80])
    if topic and topic != chapter:
        parts.append(topic[:60])
    if difficulty:
        parts.append(difficulty)

    prefix = " | ".join(parts)
    text_body = question_text[:500]

    return f"{prefix}: {text_body}" if prefix else text_body


# ========== STORAGE ==========

async def embed_questions(db: AsyncSession, question_ids: list[int]):
    """Generate and store embeddings with enriched text + metadata."""
    if not question_ids:
        return

    placeholders = ",".join(str(int(qid)) for qid in question_ids)
    result = await db.execute(text(f"""
        SELECT question_id FROM question_embedding
        WHERE question_id IN ({placeholders})
    """))
    existing = {row[0] for row in result.fetchall()}

    new_ids = [qid for qid in question_ids if qid not in existing]
    if not new_ids:
        logger.debug("All questions already have embeddings")
        return

    new_placeholders = ",".join(str(int(qid)) for qid in new_ids)
    result = await db.execute(text(f"""
        SELECT id, user_id, question_text, topic, difficulty, grade, chapter
        FROM question WHERE id IN ({new_placeholders})
    """))
    questions = result.fetchall()

    if not questions:
        return

    # Enriched text for embedding (v3)
    texts = [
        enrich_text_for_embedding(
            question_text=q[2],
            topic=q[3] or "",
            grade=q[5],
            chapter=q[6] or "",
            difficulty=q[4] or "",
        )
        for q in questions
    ]
    logger.info(f"Generating embeddings for {len(texts)} questions...")
    embeddings = await _generate_embeddings_batch(texts)

    is_pg = _is_postgres()

    rows_to_insert = []
    for q, emb in zip(questions, embeddings):
        if emb is not None:
            rows_to_insert.append({
                "qid": q[0], "uid": q[1],
                "topic": q[3] or "", "diff": q[4] or "",
                "grade": q[5], "chapter": q[6] or "",
                "emb": str(emb) if is_pg else json.dumps(emb),
            })

    if not rows_to_insert:
        return

    stored = 0
    try:
        if is_pg:
            # Try pgvector INSERT first (requires vector column type)
            upsert_sql = text("""
                INSERT INTO question_embedding
                (question_id, user_id, topic, difficulty, grade, chapter, embedding)
                VALUES (:qid, :uid, :topic, :diff, :grade, :chapter, :emb::vector)
                ON CONFLICT (question_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    topic = EXCLUDED.topic,
                    difficulty = EXCLUDED.difficulty,
                    grade = EXCLUDED.grade,
                    chapter = EXCLUDED.chapter,
                    embedding = EXCLUDED.embedding
            """)
            # Fallback: if column is still TEXT (pgvector not enabled), store as JSON string
            upsert_sql_text_fallback = text("""
                INSERT INTO question_embedding
                (question_id, user_id, topic, difficulty, grade, chapter, embedding)
                VALUES (:qid, :uid, :topic, :diff, :grade, :chapter, :emb)
                ON CONFLICT (question_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    topic = EXCLUDED.topic,
                    difficulty = EXCLUDED.difficulty,
                    grade = EXCLUDED.grade,
                    chapter = EXCLUDED.chapter,
                    embedding = EXCLUDED.embedding
            """)
        else:
            upsert_sql = text("""
                INSERT OR REPLACE INTO question_embedding
                (question_id, user_id, topic, difficulty, grade, chapter, embedding)
                VALUES (:qid, :uid, :topic, :diff, :grade, :chapter, :emb)
            """)
            upsert_sql_text_fallback = upsert_sql

        use_fallback = False
        for row in rows_to_insert:
            try:
                if use_fallback:
                    await db.execute(upsert_sql_text_fallback, row)
                else:
                    await db.execute(upsert_sql, row)
            except Exception as cast_err:
                # ::vector cast failed → column is TEXT, fallback to JSON string
                if not use_fallback and "vector" in str(cast_err).lower():
                    logger.warning(
                        f"pgvector cast failed, falling back to TEXT storage. "
                        f"Run 'CREATE EXTENSION vector' on Neon to fix. Error: {cast_err}"
                    )
                    use_fallback = True
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    # Retry this row with text fallback
                    await db.execute(upsert_sql_text_fallback, row)
                else:
                    raise
            stored += 1

        await db.commit()
    except Exception as e:
        logger.warning(f"Batch embedding insert failed: {e}")
        try:
            await db.rollback()
        except Exception:
            pass

    logger.info(f"Stored {stored}/{len(questions)} embeddings")


# ========== SIMILARITY SEARCH ==========

async def find_similar(
    db: AsyncSession,
    query_text: str,
    user_id: int,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    grade: Optional[int] = None,
    limit: int = 5,
    min_similarity: float = 0.3,
) -> list[dict]:
    """Find similar questions — pgvector native on PG, numpy fallback on SQLite.

    v3: added grade param, enriched query embedding, <=> operator on PG.
    """
    conditions = ["user_id = :uid"]
    params: dict = {"uid": user_id}
    if topic:
        conditions.append("topic = :topic")
        params["topic"] = topic
    if difficulty:
        conditions.append("difficulty = :diff")
        params["diff"] = difficulty
    if grade:
        conditions.append("grade = :grade")
        params["grade"] = grade

    where_clause = " AND ".join(conditions)

    # Early exit
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM question_embedding WHERE {where_clause}"), params
    )
    count = count_result.scalar() or 0
    if count == 0:
        logger.debug("No embeddings found, skipping similarity search")
        return []

    # Enriched query embedding
    enriched_query = enrich_text_for_embedding(
        query_text, topic=topic or "", grade=grade, difficulty=difficulty or "",
    )
    query_emb = await _generate_embedding(enriched_query)
    if query_emb is None:
        return []

    if _is_postgres():
        return await _find_similar_pgvector(
            db, query_emb, where_clause, params, limit, min_similarity
        )
    else:
        return await _find_similar_numpy(
            db, query_emb, where_clause, params, limit, min_similarity
        )


async def _find_similar_pgvector(
    db: AsyncSession,
    query_emb: list[float],
    where_clause: str,
    params: dict,
    limit: int,
    min_similarity: float,
) -> list[dict]:
    """pgvector: ORDER BY <=> (cosine distance).

    cosine_distance = 1 - cosine_similarity
    so min_similarity=0.3 → max_distance=0.7
    """
    max_distance = 1.0 - min_similarity
    params["emb"] = str(query_emb)
    params["max_dist"] = max_distance
    params["lim"] = limit

    result = await db.execute(text(f"""
        SELECT
            question_id,
            1 - (embedding <=> :emb::vector) AS similarity
        FROM question_embedding
        WHERE {where_clause}
          AND (embedding <=> :emb::vector) <= :max_dist
        ORDER BY embedding <=> :emb::vector
        LIMIT :lim
    """), params)

    return [
        {"question_id": row[0], "similarity": float(row[1])}
        for row in result.fetchall()
    ]


async def _find_similar_numpy(
    db: AsyncSession,
    query_emb: list[float],
    where_clause: str,
    params: dict,
    limit: int,
    min_similarity: float,
) -> list[dict]:
    """SQLite fallback: load all embeddings + numpy cosine similarity."""
    import numpy as np

    result = await db.execute(text(f"""
        SELECT question_id, embedding
        FROM question_embedding
        WHERE {where_clause}
    """), params)
    candidates = result.fetchall()

    if not candidates:
        return []

    query_vec = np.array(query_emb, dtype=np.float32)
    qids = []
    emb_list = []

    for qid, emb_data in candidates:
        try:
            emb = json.loads(emb_data) if isinstance(emb_data, str) else emb_data
            emb_list.append(emb)
            qids.append(qid)
        except Exception:
            continue

    if not emb_list:
        return []

    emb_matrix = np.array(emb_list, dtype=np.float32)
    q_norm = np.linalg.norm(query_vec)
    if q_norm == 0:
        return []
    c_norms = np.linalg.norm(emb_matrix, axis=1)
    c_norms = np.where(c_norms == 0, 1e-10, c_norms)
    similarities = (emb_matrix @ query_vec) / (c_norms * q_norm)

    mask = similarities >= min_similarity
    filtered_indices = np.where(mask)[0]
    if len(filtered_indices) == 0:
        return []

    sorted_idx = filtered_indices[np.argsort(similarities[filtered_indices])[::-1]]
    top_k = sorted_idx[:limit]

    return [
        {"question_id": qids[i], "similarity": float(similarities[i])}
        for i in top_k
    ]


async def delete_embedding(db: AsyncSession, question_id: int):
    """Remove embedding for a deleted question."""
    try:
        await db.execute(text(
            "DELETE FROM question_embedding WHERE question_id = :qid"
        ), {"qid": question_id})
    except Exception as e:
        logger.debug(f"Embedding delete note: {e}")