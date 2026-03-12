import asyncio
from sqlalchemy import text
from app.db.session import engine

async def make_admin():
    async with engine.begin() as conn:
        res = await conn.execute(text("UPDATE \"user\" SET role = 'admin' WHERE email = 'dinhtan.yuki@gmail.com'"))
        print(f"Updated {res.rowcount} users to admin.")
    await engine.dispose()

asyncio.run(make_admin())
