import asyncio
import logging
import re
import sys

from telethon import TelegramClient, events

from bot.config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ALLOWED_USERS,
    HOST,
    PORT,
    validate_config,
)
from bot.database import init_db
from bot.leecher import process_video


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

# Telegram bot client
bot_client = TelegramClient(
    "eporner_bot",
    API_ID,
    API_HASH,
)

# Supported Eporner URLs
EPORNER_PATTERN = re.compile(
    r"https?://(?:www\.)?eporner\.com/"
    r"(?:video-|hd-porn/)[A-Za-z0-9]{5,12}[^\s]*",
    re.IGNORECASE,
)


def is_authorized(user_id: int | None) -> bool:
    if not ALLOWED_USERS or user_id is None:
        return False

    return user_id in ALLOWED_USERS


async def health_handler(reader, writer):
    """Minimal HTTP health endpoint for Render."""

    try:
        request = await asyncio.wait_for(
            reader.readline(),
            timeout=5,
        )

        parts = request.decode(
            "utf-8",
            errors="ignore",
        ).strip().split()

        path = parts[1] if len(parts) >= 2 else "/"

        if path == "/health":
            status = "200 OK"
            body = "OK"
        else:
            status = "404 Not Found"
            body = "Not Found"

        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{body}"
        )

        writer.write(response.encode("utf-8"))
        await writer.drain()

    except Exception:
        logger.debug("Health check request failed.")

    finally:
        writer.close()

        try:
            await writer.wait_closed()
        except Exception:
            pass


@bot_client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
async def start_handler(event):
    if not is_authorized(event.sender_id):
        await event.reply(
            "⛔ You are not authorized to use this bot."
        )
        return

    await event.reply(
        "👋 Welcome to Eporner Leech Bot!\n\n"
        "Send an Eporner video URL to download it.\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/help - Help\n"
        "/status - Check bot status"
    )


@bot_client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
async def help_handler(event):
    if not is_authorized(event.sender_id):
        return

    await event.reply(
        "📖 Eporner Leech Bot Help\n\n"
        "1. Open Eporner.\n"
        "2. Copy a publicly accessible video URL.\n"
        "3. Send the URL here.\n"
        "4. Wait for processing and upload.\n\n"
        "Only supported Eporner video URLs are accepted."
    )


@bot_client.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
async def status_handler(event):
    if not is_authorized(event.sender_id):
        return

    await event.reply(
        "🟢 Bot is running.\n"
        "📡 Telegram connection active.\n"
        "⚙️ Download engine ready."
    )


@bot_client.on(
    events.NewMessage(
        incoming=True,
        func=lambda event: bool(event.raw_text),
    )
)
async def video_handler(event):
    if not is_authorized(event.sender_id):
        return

    text = event.raw_text.strip()

    if text.startswith("/"):
        return

    match = EPORNER_PATTERN.search(text)

    if not match:
        return

    video_url = match.group(0).rstrip(".,!?;:)]}")

    logger.info(
        "Video request received from user %s",
        event.sender_id,
    )

    try:
        await process_video(
            bot_client=bot_client,
            message=event.message,
            url=video_url,
        )

    except Exception:
        logger.exception("Video processing failed.")
        await event.reply(
            "❌ Something went wrong while processing this video."
        )


async def main():
    logger.info("Starting Eporner Leech Bot...")

    validate_config()
    await init_db()

    # Start health endpoint for Render
    health_server = await asyncio.start_server(
        health_handler,
        HOST,
        PORT,
    )

    logger.info(
        "Health server running at %s:%s",
        HOST,
        PORT,
    )

    try:
        # Start Telegram bot
        await bot_client.start(
            bot_token=BOT_TOKEN,
        )

        me = await bot_client.get_me()

        logger.info(
            "Telegram bot connected: @%s",
            me.username,
        )

        await bot_client.run_until_disconnected()

    finally:
        health_server.close()
        await health_server.wait_closed()

        await bot_client.disconnect()

        logger.info("Bot shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("Bot stopped.")

    except Exception:
        logger.exception("Fatal startup error.")
        sys.exit(1)
