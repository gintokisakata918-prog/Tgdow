import asyncio
import logging
import re
import sys

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from bot.config import (
    API_ID,
    API_HASH,
    STRING_SESSION,
    BOT_TOKEN,
    ALLOWED_USERS,
    validate_config,
)

from bot.database import init_db
from bot.leecher import process_video


# =========================
# Logging
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================
# Clients
# =========================

bot_client = TelegramClient(
    "eporner_bot",
    API_ID,
    API_HASH,
)

user_client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH,
)


# =========================
# URL Detection
# =========================

EPORNER_PATTERN = re.compile(
    r"https?://(?:www\.)?eporner\.com/"
    r"(?:video-|hd-porn/)[A-Za-z0-9]{5,12}"
    r"[^\s]*",
    re.IGNORECASE,
)


# =========================
# Authorization
# =========================

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return False

    return user_id in ALLOWED_USERS


# =========================
# Start Command
# =========================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/start(?:@\w+)?$"
    )
)
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


# =========================
# Help Command
# =========================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/help(?:@\w+)?$"
    )
)
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


# =========================
# Status Command
# =========================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/status(?:@\w+)?$"
    )
)
async def status_handler(event):

    if not is_authorized(event.sender_id):
        return

    await event.reply(
        "🟢 Bot is running.\n"
        "📡 Telegram connection active.\n"
        "⚙️ Download engine ready."
    )


# =========================
# Video Processing
# =========================

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

    video_url = match.group(0).rstrip(
        ".,!?;:)]}"
    )

    logger.info(
        "Video request received from user %s",
        event.sender_id,
    )

    await process_video(
        bot_client=bot_client,
        user_client=user_client,
        message=event.message,
        url=video_url,
    )


# =========================
# Main
# =========================

async def main():

    logger.info("Starting Eporner Leech Bot...")

    validate_config()

    await init_db()

    # Start MTProto user session
    await user_client.start()

    logger.info(
        "MTProto user session connected."
    )

    # Start Telegram bot
    await bot_client.start(
        bot_token=BOT_TOKEN
    )

    logger.info(
        "Telegram bot connected."
    )

    me = await bot_client.get_me()

    logger.info(
        "Bot started: @%s",
        me.username,
    )

    await bot_client.run_until_disconnected()


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("Bot stopped.")

    except Exception:
        logger.exception("Fatal startup error.")
        sys.exit(1)

    finally:
        logger.info("Shutdown complete.")
