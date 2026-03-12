"""Quick migration: add is_public column to question table."""
import asyncio
from sqlalchemy import text
from app.db.session import engine


async def run():
    async with engine.begin() as conn:
        # Check if column exists
        result = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='question' AND column_name='is_public'"
        ))
        row = result.fetchone()
        if row:
            print(f"Column is_public already exists: {row}")
        else:
            print("Column is_public does NOT exist. Adding...")
            await conn.execute(text(
                "ALTER TABLE question ADD COLUMN is_public BOOLEAN DEFAULT TRUE"
            ))
            # Set all existing questions to public
            result = await conn.execute(text(
                "UPDATE question SET is_public = TRUE WHERE is_public IS NULL"
            ))
            print(f"Done! Column added. Updated {result.rowcount} rows to TRUE.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
