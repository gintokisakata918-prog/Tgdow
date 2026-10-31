import asyncio
import logging
import re
import sys

from telethon import TelegramClient, events, Button

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
from bot.eporner import (
    fetch_meta,
    get_resolution,
    NoStreamsAvailableError,
)

from bot.leecher import process_video


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

bot_client = TelegramClient(
    "eporner_bot",
    API_ID,
    API_HASH,
)

EPORNER_PATTERN = re.compile(
    r"https?://(?:www\.)?eporner\.com/"
    r"(?:video-|hd-porn/)[A-Za-z0-9]{5,12}[^\s]*",
    re.IGNORECASE,
)

QUALITY_OPTIONS = [480, 720, 1080]


def is_authorized(user_id: int | None) -> bool:
    if not ALLOWED_USERS or user_id is None:
        return False

    return user_id in ALLOWED_USERS


async def health_handler(reader, writer):
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
        "1. Send an Eporner video URL.\n"
        "2. Select video quality.\n"
        "3. Wait for download and upload.\n\n"
        "Supported qualities depend on video availability."
    )


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

    status_message = await event.reply(
        "🔎 Checking available video qualities..."
    )

    try:
        video_id = re.search(
            r"(?:video-|hd-porn/)([A-Za-z0-9]{5,12})",
            video_url,
            re.IGNORECASE,
        ).group(1)

        metadata = await fetch_meta(video_id)

        available = {
            get_resolution(stream.get("resolution", ""))
            for stream in metadata.get("videos", [])
        }

        buttons = []

        for quality in QUALITY_OPTIONS:
            if quality in available:
                buttons.append(
                    Button.inline(
                        f"🎬 {quality}P",
                        data=f"dl:{video_id}:{quality}".encode(),
                    )
                )

        if not buttons:
            raise NoStreamsAvailableError(
                "480P, 720P ya 1080P mein koi stream available nahi hai."
            )

        keyboard = [buttons]

        await status_message.edit(
            "🎬 **Select Video Quality**\n\n"
            "Apni preferred quality choose karo:",
            buttons=keyboard,
        )

    except Exception as exc:
        logger.exception("Could not fetch video qualities.")

        await status_message.edit(
            f"❌ Could not load video qualities:\n{str(exc)[:500]}"
        )


@bot_client.on(events.CallbackQuery)
async def quality_callback(event):
    if not is_authorized(event.sender_id):
        await event.answer(
            "You are not authorized.",
            alert=True,
        )
        return

    try:
        data = event.data.decode("utf-8")

        if not data.startswith("dl:"):
            return

        _, video_id, resolution = data.split(":")

        resolution = int(resolution)

        if resolution not in QUALITY_OPTIONS:
            await event.answer(
                "Invalid quality selection.",
                alert=True,
            )
            return

        await event.answer(
            f"{resolution}P selected!"
        )

        await event.edit(
            f"✅ Selected quality: {resolution}P\n"
            "⏳ Preparing download..."
        )

        video_url = (
            f"https://www.eporner.com/video-{video_id}/"
        )

        await process_video(
            bot_client=bot_client,
            message=event.message,
            url=video_url,
            selected_resolution=resolution,
        )

    except Exception:
        logger.exception("Quality callback failed.")

        try:
            await event.answer(
                "Something went wrong.",
                alert=True,
            )
        except Exception:
            pass


async def main():
    logger.info("Starting Eporner Leech Bot...")

    validate_config()
    await init_db()

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
