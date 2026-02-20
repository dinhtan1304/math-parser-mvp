"""Full-Text Search (FTS5) for Question Bank.

SQLite FTS5 enables fast keyword search on question_text,
replacing slow LIKE '%keyword%' queries.

Usage:
    - Call init_fts(engine) on startup to create the FTS virtual table
    - Call sync_fts(session) after inserting questions to update the index
    - Call search_fts(session, keyword, user_id) to search
"""

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)


async def init_fts(engine: AsyncEngine):
    """Create FTS5 virtual table if not exists. Call once on startup."""
    async with engine.begin() as conn:
        # Create FTS5 virtual table
        await conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS question_fts
            USING fts5(
                question_id,
                user_id UNINDEXED,
                question_text,
                topic,
                content='question',
                content_rowid='id',
                tokenize='unicode61'
            )
        """))

        # Populate FTS from existing questions (idempotent - rebuild)
        try:
            await conn.execute(text("""
                INSERT OR REPLACE INTO question_fts(
                    rowid, question_id, user_id, question_text, topic
                )
                SELECT id, id, user_id, question_text, COALESCE(topic, '')
                FROM question
                WHERE id NOT IN (SELECT rowid FROM question_fts)
            """))
        except Exception as e:
            # FTS might already be populated, that's fine
            logger.debug(f"FTS populate note: {e}")

    logger.info("FTS5 index initialized")


async def sync_fts_questions(db: AsyncSession, question_ids: list[int]):
    """Sync specific questions into FTS index after insert/update."""
    if not question_ids:
        return

    placeholders = ",".join(str(int(qid)) for qid in question_ids)

    try:
        await db.execute(text(f"""
            INSERT OR REPLACE INTO question_fts(
                rowid, question_id, user_id, question_text, topic
            )
            SELECT id, id, user_id, question_text, COALESCE(topic, '')
            FROM question
            WHERE id IN ({placeholders})
        """))
        await db.commit()
        logger.debug(f"FTS synced {len(question_ids)} questions")
    except Exception as e:
        logger.warning(f"FTS sync failed: {e}")


async def search_fts(db: AsyncSession, keyword: str, user_id: int,
                     limit: int = 20) -> list[int]:
    """Search questions by keyword using FTS5. Returns question IDs."""
    if not keyword or not keyword.strip():
        return []

    # Escape FTS5 special chars and build query
    safe_keyword = keyword.strip().replace('"', '""')

    try:
        result = await db.execute(text("""
            SELECT question_id
            FROM question_fts
            WHERE question_fts MATCH :query
              AND user_id = :uid
            ORDER BY rank
            LIMIT :lim
        """), {"query": f'"{safe_keyword}"', "uid": str(user_id), "lim": limit})

        return [row[0] for row in result.fetchall()]
    except Exception as e:
        logger.warning(f"FTS search failed: {e}")
        return []


async def delete_fts_question(db: AsyncSession, question_id: int):
    """Remove a question from FTS index."""
    try:
        await db.execute(text("""
            DELETE FROM question_fts WHERE rowid = :qid
        """), {"qid": question_id})
    except Exception as e:
        logger.debug(f"FTS delete note: {e}")