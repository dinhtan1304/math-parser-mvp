"""Vector Similarity Search for Question Bank.

Uses Google's embedding API to find semantically similar questions,
improving sample selection for AI question generation.

Architecture:
    - Embeddings stored in SQLite table (question_embedding)
    - Google text-embedding-004 model for embedding generation
    - Cosine similarity computed in Python (fast for <50K questions)

Usage:
    - Call embed_questions(db, question_ids) after inserting questions
    - Call find_similar(db, query, user_id, ...) to find matching samples
"""

import os
import json
import math
import asyncio
import logging
from typing import Optional

from sqlalchemy import text, Column, Integer, Text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)


# ========== INIT ==========

async def init_vector_table(engine: AsyncEngine):
    """Create embedding storage table. Call once on startup."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS question_embedding (
                question_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                topic TEXT,
                difficulty TEXT,
                embedding TEXT NOT NULL,
                FOREIGN KEY (question_id) REFERENCES question(id) ON DELETE CASCADE
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_qemb_user
            ON question_embedding(user_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_qemb_user_topic
            ON question_embedding(user_id, topic)
        """))
    logger.info("Vector embedding table initialized")


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


async def _generate_embedding(text_content: str) -> Optional[list[float]]:
    """Generate embedding vector for a text string."""
    client = _get_client()
    if not client:
        return None

    try:
        # Use native async API
        result = await client.aio.models.embed_content(
            model="text-embedding-004",
            contents=text_content[:2000],  # Truncate to avoid token limits
        )
        if result and result.embeddings:
            return result.embeddings[0].values
    except Exception as e:
        logger.warning(f"Embedding generation failed: {e}")

    return None


async def _generate_embeddings_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Generate embeddings for multiple texts in parallel."""
    tasks = [_generate_embedding(t) for t in texts]
    return await asyncio.gather(*tasks)


# ========== STORAGE ==========

async def embed_questions(db: AsyncSession, question_ids: list[int]):
    """Generate and store embeddings for questions. Skips already-embedded ones."""
    if not question_ids:
        return

    # Check which IDs already have embeddings
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

    # Fetch question texts
    new_placeholders = ",".join(str(int(qid)) for qid in new_ids)
    result = await db.execute(text(f"""
        SELECT id, user_id, question_text, topic, difficulty
        FROM question WHERE id IN ({new_placeholders})
    """))
    questions = result.fetchall()

    if not questions:
        return

    # Build embedding input: combine question + topic for better semantic matching
    texts = []
    for q in questions:
        combined = f"{q[3] or ''}: {q[2][:500]}"  # topic: question_text
        texts.append(combined)

    # Generate embeddings (parallel)
    logger.info(f"Generating embeddings for {len(texts)} questions...")
    embeddings = await _generate_embeddings_batch(texts)

    # Store embeddings
    stored = 0
    for q, emb in zip(questions, embeddings):
        if emb is None:
            continue
        try:
            await db.execute(text("""
                INSERT OR REPLACE INTO question_embedding
                (question_id, user_id, topic, difficulty, embedding)
                VALUES (:qid, :uid, :topic, :diff, :emb)
            """), {
                "qid": q[0],
                "uid": q[1],
                "topic": q[3] or "",
                "diff": q[4] or "",
                "emb": json.dumps(emb),
            })
            stored += 1
        except Exception as e:
            logger.warning(f"Failed to store embedding for q#{q[0]}: {e}")

    if stored:
        await db.commit()
    logger.info(f"Stored {stored}/{len(questions)} embeddings")


# ========== SIMILARITY SEARCH ==========

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def find_similar(
    db: AsyncSession,
    query_text: str,
    user_id: int,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    limit: int = 5,
    min_similarity: float = 0.3,
) -> list[dict]:
    """Find questions most similar to query_text using vector similarity.

    Returns list of {question_id, similarity} ordered by similarity desc.
    Falls back to empty list if embeddings unavailable.
    """
    # Generate query embedding
    query_emb = await _generate_embedding(
        f"{topic or ''}: {query_text[:500]}"
    )
    if query_emb is None:
        logger.debug("Could not generate query embedding, falling back")
        return []

    # Fetch candidate embeddings from DB
    conditions = ["user_id = :uid"]
    params = {"uid": user_id}

    if topic:
        conditions.append("topic = :topic")
        params["topic"] = topic
    if difficulty:
        conditions.append("difficulty = :diff")
        params["diff"] = difficulty

    where_clause = " AND ".join(conditions)

    result = await db.execute(text(f"""
        SELECT question_id, embedding
        FROM question_embedding
        WHERE {where_clause}
    """), params)
    candidates = result.fetchall()

    if not candidates:
        return []

    # Compute similarities
    scored = []
    for qid, emb_json in candidates:
        try:
            emb = json.loads(emb_json)
            sim = _cosine_similarity(query_emb, emb)
            if sim >= min_similarity:
                scored.append({"question_id": qid, "similarity": sim})
        except Exception:
            continue

    # Sort by similarity descending
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit]


async def delete_embedding(db: AsyncSession, question_id: int):
    """Remove embedding for a deleted question."""
    try:
        await db.execute(text(
            "DELETE FROM question_embedding WHERE question_id = :qid"
        ), {"qid": question_id})
    except Exception as e:
        logger.debug(f"Embedding delete note: {e}")