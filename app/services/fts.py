"""Full-Text Search (FTS5) for Question Bank.

SQLite FTS5 enables fast keyword search on question_text,
replacing slow LIKE '%keyword%' queries.

BUG FIXES (v2):
    1. Removed `content='question', content_rowid='id'` — external content FTS5 caused
       rebuild failures because `question_id` column doesn't exist in `question` table.
    2. Fixed `search_fts` passing `str(user_id)` instead of int.
    3. Replaced `INSERT OR REPLACE` with DELETE + INSERT (FTS5 doesn't deduplicate on OR REPLACE).
    4. Fixed `init_fts` populate — no longer uses fragile `NOT IN (SELECT rowid FROM fts)`.
"""

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)


async def init_fts(engine: AsyncEngine):
    """Create FTS5 virtual table if not exists. Call once on startup."""
    async with engine.begin() as conn:
        # BUG FIX: Removed content='question' and content_rowid='id'.
        # External content FTS5 requires FTS columns to exactly match the content table.
        # Our FTS table had `question_id` column which doesn't exist in `question` table,
        # causing FTS5 rebuild operations to fail.
        await conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS question_fts
            USING fts5(
                question_id UNINDEXED,
                user_id UNINDEXED,
                question_text,
                topic,
                tokenize='unicode61'
            )
        """))

        # Populate FTS from existing questions
        # BUG FIX: Old code used `WHERE id NOT IN (SELECT rowid FROM question_fts)`
        # which is unreliable for FTS5. Now we check counts and rebuild if needed.
        try:
            fts_count = (await conn.execute(
                text("SELECT COUNT(*) FROM question_fts")
            )).scalar() or 0

            q_count = (await conn.execute(
                text("SELECT COUNT(*) FROM question")
            )).scalar() or 0

            if fts_count < q_count:
                await conn.execute(text("DELETE FROM question_fts"))
                await conn.execute(text("""
                    INSERT INTO question_fts(question_id, user_id, question_text, topic)
                    SELECT id, user_id, question_text, COALESCE(topic, '')
                    FROM question
                """))
                logger.info(f"FTS5 populated with {q_count} questions from question table")
        except Exception as e:
            logger.debug(f"FTS populate note: {e}")

    logger.info("FTS5 index initialized")


async def sync_fts_questions(db: AsyncSession, question_ids: list[int]):
    """Sync specific questions into FTS index after insert/update.

    BUG FIX: FTS5 INSERT OR REPLACE does NOT deduplicate — each INSERT adds a new
    row because FTS5 virtual tables don't enforce unique constraints on user-inserted rows.
    Fix: DELETE existing rows first, then INSERT fresh data.
    """
    if not question_ids:
        return

    placeholders = ",".join(str(int(qid)) for qid in question_ids)

    try:
        # Delete existing FTS entries for these IDs first
        await db.execute(text(f"""
            DELETE FROM question_fts WHERE question_id IN ({placeholders})
        """))

        # Re-insert fresh from question table
        await db.execute(text(f"""
            INSERT INTO question_fts(question_id, user_id, question_text, topic)
            SELECT id, user_id, question_text, COALESCE(topic, '')
            FROM question
            WHERE id IN ({placeholders})
        """))
        await db.commit()
        logger.debug(f"FTS synced {len(question_ids)} questions")
    except Exception as e:
        logger.warning(f"FTS sync failed: {e}")


async def search_fts(db: AsyncSession, keyword: str, user_id: int,
                     limit: int = 20) -> list[int]:
    """Search questions by keyword using FTS5. Returns question IDs.

    BUG FIX: Previously passed str(user_id) to SQL — stored as integer.
    Now passes int directly to avoid type mismatch in comparison.
    """
    if not keyword or not keyword.strip():
        return []

    safe_keyword = keyword.strip().replace('"', '""')

    try:
        result = await db.execute(text("""
            SELECT question_id
            FROM question_fts
            WHERE question_fts MATCH :query
              AND user_id = :uid
            ORDER BY rank
            LIMIT :lim
        """), {
            "query": f'"{safe_keyword}"',
            "uid": user_id,   # BUG FIX: was str(user_id)
            "lim": limit,
        })
        return [row[0] for row in result.fetchall()]
    except Exception as e:
        logger.warning(f"FTS search failed: {e}")
        return []


async def delete_fts_question(db: AsyncSession, question_id: int):
    """Remove a question from FTS index."""
    try:
        await db.execute(text("""
            DELETE FROM question_fts WHERE question_id = :qid
        """), {"qid": question_id})
    except Exception as e:
        logger.debug(f"FTS delete note: {e}")