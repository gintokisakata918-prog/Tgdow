import asyncio
import logging
import re
import time
import json

from pathlib import Path

from PIL import Image

from telethon.tl.types import (
    DocumentAttributeVideo,
    DocumentAttributeFilename,
)

from bot.config import (
    DOWNLOAD_DIR,
    CLEAN,
    CLEAN_CAPTION_TEMPLATE,
    CAPTION_TEMPLATE,
)

from bot.database import seen, mark

from bot.eporner import (
    parse_id,
    fetch_meta,
    pick_source,
    get_title,
    get_duration,
    get_keywords,
    get_thumbnail,
)

from bot.downloader import (
    download,
    download_thumbnail,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def human_mb(value):
    return f"{value / (1024 * 1024):.2f} MB"


def clean_text(text):
    text = re.sub(r"https?://\S+", "", str(text))
    text = re.sub(r"eporner", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_hashtags(keywords):
    tags = []

    for keyword in keywords or []:
        keyword = re.sub(
            r"[^a-zA-Z0-9_]",
            "",
            str(keyword).replace(" ", "_"),
        )

        if keyword:
            tags.append("#" + keyword)

        if len(tags) >= 8:
            break

    return " ".join(tags)


def make_caption(title, keywords):

    if CLEAN:
        title = clean_text(title)
        template = CLEAN_CAPTION_TEMPLATE
    else:
        template = CAPTION_TEMPLATE

    hashtags = make_hashtags(keywords)

    caption = template.format(
        title=title,
        hashtags=hashtags,
    )

    return caption[:1024]


def make_thumbnail(source, output):

    try:
        with Image.open(source) as image:

            image = image.convert("RGB")
            image.thumbnail((320, 320))

            quality = 85

            while quality >= 30:
                image.save(
                    output,
                    "JPEG",
                    quality=quality,
                    optimize=True,
                )

                if Path(output).stat().st_size <= 190 * 1024:
                    break

                quality -= 10

        return Path(output)

    except Exception:
        logger.exception("Thumbnail processing failed")
        return None


async def get_video_info(path):

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _ = await process.communicate()

    if process.returncode != 0:
        return 0, 1280, 720

    data = json.loads(stdout)

    duration = int(
        float(data.get("format", {}).get("duration", 0))
    )

    width = 1280
    height = 720

    for stream in data.get("streams", []):

        if stream.get("codec_type") == "video":
            width = int(stream.get("width", 1280))
            height = int(stream.get("height", 720))
            break

    return duration, width, height


# --------------------------------------------------
# PROGRESS DISPLAY
# --------------------------------------------------

async def show_progress(
    message,
    heading,
    current,
    total,
    start_time,
    last_update,
    force=False,
):

    now = time.monotonic()

    if not force and now - last_update < 3:
        return now

    elapsed = max(now - start_time, 0.1)

    speed = current / elapsed
    speed_mb = speed / (1024 * 1024)

    current_mb = human_mb(current)

    if total > 0:
        percentage = current / total * 100
        total_mb = human_mb(total)

        text = (
            f"{heading}\n\n"
            f"📊 Progress: {percentage:.1f}%\n"
            f"📦 Transferred: {current_mb} / {total_mb}\n"
            f"⚡ Speed: {speed_mb:.2f} MB/s"
        )

    else:
        text = (
            f"{heading}\n\n"
            f"📦 Transferred: {current_mb}\n"
            f"⚡ Speed: {speed_mb:.2f} MB/s\n"
            f"ℹ️ Total size: Unknown"
        )

    try:
        await message.edit(text)

    except Exception:
        pass

    return now


# --------------------------------------------------
# MAIN VIDEO PROCESS
# --------------------------------------------------

async def process_video(
    bot_client,
    message,
    url: str,
    selected_resolution: int | None = None,
):

    video_id = parse_id(url)

    if not video_id:
        await message.edit("❌ Invalid video URL.")
        return

    if await seen(video_id):
        await message.edit(
            "⚠️ This video has already been downloaded."
        )
        return

    video_path = None
    thumbnail_path = None

    try:

        # Fetch metadata

        await message.edit(
            "🔎 Fetching video information..."
        )

        metadata = await fetch_meta(video_id)

        title = get_title(metadata)
        duration = get_duration(metadata)
        keywords = get_keywords(metadata)

        # Select quality

        source = pick_source(
            metadata,
            max_res=selected_resolution or 1080,
        )

        if not source:
            raise RuntimeError("No video stream available.")

        if isinstance(source, dict):

            stream_url = (
                source.get("url")
                or source.get("src")
            )

            resolution = (
                source.get("resolution")
                or source.get("quality")
            )

        elif isinstance(source, (tuple, list)):

            stream_url = source[0]
            resolution = source[1] if len(source) > 1 else None

        else:

            stream_url = source
            resolution = None

        if not stream_url:
            raise RuntimeError("Stream URL not found.")

        if selected_resolution and resolution:

            resolution_match = re.search(
                r"(\d{3,4})",
                str(resolution),
            )

            if (
                resolution_match
                and int(resolution_match.group(1))
                != selected_resolution
            ):
                raise RuntimeError(
                    "Selected quality does not match stream."
                )

        # Download video

        filename = f"{video_id}.mp4"

        await message.edit(
            f"📥 Downloading {selected_resolution or 1080}P\n\n"
            "Preparing download..."
        )

        download_start = time.monotonic()
        download_last_update = 0

        async def download_progress(current, total):

            nonlocal download_last_update

            download_last_update = await show_progress(
                message,
                f"📥 Downloading {selected_resolution or 1080}P",
                current,
                total,
                download_start,
                download_last_update,
            )

        video_path = await download(
            stream_url,
            filename,
            progress_callback=download_progress,
        )

        await show_progress(
            message,
            f"📥 Downloading {selected_resolution or 1080}P",
            video_path.stat().st_size,
            video_path.stat().st_size,
            download_start,
            download_last_update,
            force=True,
        )

        # Original video - no compression
        # Original video - no metadata reprocessing

        file_size = video_path.stat().st_size

        logger.info(
            "Original video preserved: %s",
            human_mb(file_size),
        )

        # Thumbnail

        thumbnail_url = get_thumbnail(metadata)

        if thumbnail_url:

            try:
                raw_thumbnail = await download_thumbnail(
                    thumbnail_url,
                    f"{video_id}_raw.jpg",
                )

                thumbnail_path = DOWNLOAD_DIR / (
                    f"{video_id}_thumb.jpg"
                )

                make_thumbnail(
                    raw_thumbnail,
                    thumbnail_path,
                )

                raw_thumbnail.unlink(missing_ok=True)

            except Exception:
                logger.exception(
                    "Thumbnail download failed"
                )

                thumbnail_path = None

        # Video attributes

        actual_duration, width, height = await get_video_info(
            video_path
        )

        if not actual_duration:
            actual_duration = int(duration or 0)

        attributes = [
            DocumentAttributeVideo(
                duration=actual_duration,
                w=width,
                h=height,
                supports_streaming=True,
            ),
            DocumentAttributeFilename(
                f"{video_id}.mp4"
            ),
        ]

        caption = make_caption(
            title,
            keywords,
        )

        # Telegram upload

        await message.edit(
            f"📤 Uploading {selected_resolution or 1080}P video...\n"
            "Preparing upload..."
        )

        upload_start = time.monotonic()
        upload_last_update = 0

        async def upload_progress(current, total):

            nonlocal upload_last_update

            upload_last_update = await show_progress(
                message,
                f"📤 Uploading {selected_resolution or 1080}P video",
                current,
                total,
                upload_start,
                upload_last_update,
            )

        # Upload file using Telethon 1.42.0

        uploaded_file = await bot_client.upload_file(
            str(video_path),
            part_size_kb=512,
            file_name=f"{video_id}.mp4",
            progress_callback=upload_progress,
        )

        # Send already uploaded file

        sent_message = await bot_client.send_file(
            entity=message.chat_id,
            file=uploaded_file,
            caption=caption,
            thumb=(
                str(thumbnail_path)
                if thumbnail_path and thumbnail_path.exists()
                else None
            ),
            attributes=attributes,
            supports_streaming=True,
            reply_to=message.id,
        )

        await show_progress(
            message,
            f"📤 Uploading {selected_resolution or 1080}P video",
            file_size,
            file_size,
            upload_start,
            upload_last_update,
            force=True,
        )

        # Database

        await mark(
            video_id,
            sent_message.id,
        )

        await message.edit(
            "✅ Video uploaded successfully!"
        )

    except Exception as error:

        logger.exception(
            "Video processing failed"
        )

        try:
            await message.edit(
                f"❌ Failed:\n{str(error)[:700]}"
            )

        except Exception:
            pass

    finally:

        # Cleanup

        for path in (
            video_path,
            thumbnail_path,
        ):

            if path:

                try:
                    Path(path).unlink(missing_ok=True)

                except Exception:
                    logger.exception(
                        "File cleanup failed"
        )
