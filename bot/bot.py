import asyncio
import logging
import re

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
    parse_id,
    fetch_meta,
    get_resolution,
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


# --------------------------------------------------
# QUALITY DETECTION
# --------------------------------------------------

def find_qualities(metadata):
    return sorted({
        get_resolution(item.get("resolution"))
        for item in metadata.get("videos", [])
        if isinstance(item, dict)
        and get_resolution(item.get("resolution")) in (480, 720, 1080)
    })


# --------------------------------------------------
# HEALTH SERVER
# --------------------------------------------------

async def health_server():

    async def handle_client(reader, writer):

        try:

            request = await reader.read(1024)

            if b"GET /health" in request:

                body = b"OK"
                status = b"200 OK"

            else:

                body = b"Not Found"
                status = b"404 Not Found"

            response = (
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )

            writer.write(response)
            await writer.drain()

        except Exception:

            logger.exception("Health server error")

        finally:

            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        handle_client,
        HOST,
        PORT,
    )

    logger.info("Health server running on port %s", PORT)

    async with server:
        await server.serve_forever()


# --------------------------------------------------
# START COMMAND
# --------------------------------------------------

@bot_client.on(events.NewMessage(pattern=r"^/start$"))
async def start_command(event):

    await event.reply(
        "👋 Hello!\n\n"
        "Send me an Eporner video link."
    )


# --------------------------------------------------
# HELP COMMAND
# --------------------------------------------------

@bot_client.on(events.NewMessage(pattern=r"^/help$"))
async def help_command(event):

    await event.reply(
        "📖 How to use:\n\n"
        "1. Send an Eporner video link.\n"
        "2. Select video quality.\n"
        "3. Wait for download.\n"
        "4. Receive your video here."
    )


# --------------------------------------------------
# STATUS COMMAND
# --------------------------------------------------

@bot_client.on(events.NewMessage(pattern=r"^/status$"))
async def status_command(event):

    await event.reply("✅ Bot is running.")


# --------------------------------------------------
# VIDEO URL HANDLER
# --------------------------------------------------

@bot_client.on(
    events.NewMessage(
        incoming=True,
        func=lambda event: bool(
            re.search(
                r"https?://(?:www\.)?eporner\.com/",
                event.raw_text or "",
                re.IGNORECASE,
            )
        ),
    )
)
async def video_handler(event):

    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:

        await event.reply(
            "⛔ You are not authorized."
        )
        return

    match = re.search(
        r"https?://\S+",
        event.raw_text or "",
    )

    if not match:
        return

    video_url = match.group(0).rstrip(".,)")

    status = await event.reply(
        "🔎 Fetching video information..."
    )

    try:

        video_id = parse_id(video_url)

        if not video_id:

            await status.edit(
                "❌ Invalid Eporner video link."
            )
            return

        metadata = await fetch_meta(video_id)

        if not isinstance(metadata, dict):

            raise ValueError(
                "Invalid metadata response."
            )

        qualities = find_qualities(metadata)

        logger.info(
            "Video %s available qualities: %s",
            video_id,
            qualities,
        )

        logger.info(
            "Metadata keys: %s",
            list(metadata.keys()),
        )

        if not qualities:

            await status.edit(
                "❌ No supported qualities found."
            )
            return

        buttons = []

        row = []

        for quality in (480, 720, 1080):

            if quality in qualities:

                row.append(
                    Button.inline(
                        f"{quality}P",
                        data=f"dl:{video_id}:{quality}".encode(),
                    )
                )

        if row:
            buttons.append(row)

        title = metadata.get(
            "title",
            "Unknown",
        )

        await status.edit(
            f"🎬 **{title}**\n\n"
            "👇 Select video quality:",
            buttons=buttons,
        )

    except Exception:

        logger.exception(
            "Video handler failed"
        )

        await status.edit(
            "❌ Failed to fetch video information."
        )


# --------------------------------------------------
# QUALITY CALLBACK
# --------------------------------------------------

@bot_client.on(
    events.CallbackQuery(pattern=b"^dl:")
)
async def quality_callback(event):

    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:

        await event.answer(
            "You are not authorized.",
            alert=True,
        )
        return

    try:

        callback_data = event.data.decode(
            "utf-8"
        )

        _, video_id, quality_text = callback_data.split(":")

        resolution = int(quality_text)

        if resolution not in (480, 720, 1080):

            await event.answer(
                "Invalid quality.",
                alert=True,
            )
            return

        await event.answer(
            "Quality selected!"
        )

        callback_message = await event.get_message()

        await callback_message.edit(
            f"✅ Selected quality: {resolution}P\n"
            "⏳ Preparing download...",
            buttons=None,
        )

        video_url = (
            f"https://www.eporner.com/video-{video_id}/"
        )

        await process_video(
            bot_client=bot_client,
            message=callback_message,
            url=video_url,
            selected_resolution=resolution,
        )

    except Exception:

        logger.exception(
            "Quality callback failed"
        )

        try:

            await event.answer(
                "Download failed. Check bot logs.",
                alert=True,
            )

        except Exception:
            pass


# --------------------------------------------------
# MAIN
# --------------------------------------------------

async def main():

    validate_config()

    # Create SQLite database and tables.
    await init_db()

    logger.info(
        "Database initialized successfully."
    )

    await bot_client.start(
        bot_token=BOT_TOKEN
    )

    me = await bot_client.get_me()

    logger.info(
        "Bot connected: @%s",
        me.username,
    )

    health_task = asyncio.create_task(
        health_server()
    )

    logger.info(
        "Bot is running."
    )

    try:

        await bot_client.run_until_disconnected()

    finally:

        health_task.cancel()

        await bot_client.disconnect()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped."
        )
