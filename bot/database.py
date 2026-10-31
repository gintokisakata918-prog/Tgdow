import aiosqlite

from bot.config import DATABASE_PATH


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS done (
    vid TEXT PRIMARY KEY,
    msg_id INTEGER,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


async def init_db():
    """Initialize SQLite database."""

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(CREATE_TABLE)
        await db.commit()


async def seen(vid: str) -> bool:
    """Check whether video ID has already been posted."""

    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM done WHERE vid = ? LIMIT 1",
            (vid,),
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark(vid: str, msg_id: int) -> None:
    """Save successfully posted video."""

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO done (vid, msg_id)
            VALUES (?, ?)
            """,
            (vid, msg_id),
        )
        await db.commit()


async def get_message_id(vid: str) -> int | None:
    """Get Telegram message ID for a video."""

    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT msg_id FROM done WHERE vid = ?",
            (vid,),
        ) as cursor:
            row = await cursor.fetchone()

            return row[0] if row else None


async def delete(vid: str) -> None:
    """Remove video ID from database."""

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM done WHERE vid = ?",
            (vid,),
        )
        await db.commit()
