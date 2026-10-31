import asyncio
import re
import shutil
import subprocess
import logging
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.tl.types import (
    DocumentAttributeVideo,
)

from bot.config import (
    MAX_RES,
    CLEAN,
    COMPRESS_ABOVE_MB,
    DOWNLOAD_DIR,
    TARGET_CHANNEL,
    FFMPEG_BIN,
    FFMPEG_CRF,
    FFMPEG_AUDIO_BITRATE,
    FFMPEG_MAX_WIDTH,
    CAPTION_TEMPLATE,
    CLEAN_CAPTION_TEMPLATE,
)

from bot.database import seen, mark
from bot.eporner import (
    parse_id,
    fetch_meta,
    pick_source,
    get_duration,
    get_keywords,
    get_thumbnail,
    get_title,
)

from bot.downloader import (
    download,
    download_thumbnail,
    human,
)


logger = logging.getLogger(__name__)


def clean_title(title: str) -> str:
    """Remove branding and URLs from title."""

    title = re.sub(
        r"EPORNER",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(
        r"https?://\S+|www\.\S+",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    )

    return title.strip(" -_|")


def make_slug(title: str, video_id: str) -> str:
    """Create a filesystem-safe filename."""

    slug = title.lower()

    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        slug,
    )

    slug = slug.strip("-")

    slug = slug[:80] or "video"

    return f"{slug}-{video_id}"


def make_hashtags(keywords: list[str]) -> str:
    """Generate maximum eight hashtags."""

    hashtags = []
    seen_tags = set()

    for keyword in keywords:

        tag = re.sub(
            r"[^A-Za-z0-9_]",
            "",
            keyword.replace(" ", ""),
        )

        if not tag:
            continue

        tag = "#" + tag

        if tag.lower() in seen_tags:
            continue

        seen_tags.add(tag.lower())
        hashtags.append(tag)

        if len(hashtags) >= 8:
            break

    return " ".join(hashtags)


def make_caption(
    title: str,
    keywords: list[str],
) -> str:

    hashtags = make_hashtags(keywords)

    template = (
        CLEAN_CAPTION_TEMPLATE
        if CLEAN
        else CAPTION_TEMPLATE
    )

    caption = template.format(
        title=title,
        hashtags=hashtags,
    )

    return caption[:1024]


async def compress_video(
    input_path: Path,
    output_path: Path,
) -> Path:
    """Compress video using FFmpeg."""

    command = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map_metadata", "-1",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(FFMPEG_CRF),
        "-vf",
        f"scale='min({FFMPEG_MAX_WIDTH},iw)':-2",
        "-c:a", "aac",
        "-b:a", FFMPEG_AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-sn",
        "-dn",
        str(output_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        error = stderr.decode(
            errors="replace"
        )[-2000:]

        raise RuntimeError(
            f"FFmpeg compression failed: {error}"
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg produced an empty file.")

    return output_path


async def strip_metadata(
    input_path: Path,
    output_path: Path,
) -> Path:
    """Remove metadata without re-encoding."""

    command = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),
        "-map", "0",
        "-map_metadata", "-1",
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        error = stderr.decode(
            errors="replace"
        )[-2000:]

        raise RuntimeError(
            f"Metadata cleanup failed: {error}"
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Metadata cleanup produced an empty file.")

    return output_path


async def get_video_dimensions(
    video_path: Path,
) -> tuple[int, int]:

    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(video_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    stdout, _ = await process.communicate()

    try:
        width, height = stdout.decode().strip().split("x")

        return int(width), int(height)

    except (ValueError, AttributeError):
        return 0, 0


async def process_video(
    bot_client: TelegramClient,
    user_client: TelegramClient,
    message,
    url: str,
):
    """
    Complete Eporner download and Telegram upload pipeline.

    bot_client:
        Receives commands and edits progress messages.

    user_client:
        Telethon StringSession used for MTProto uploads.
    """

    video_path = None
    thumb_path = None
    processed_path = None

    progress = await message.reply(
        "⏳ Processing video..."
    )

    try:
        # -------------------------
        # Extract video ID
        # -------------------------

        try:
            video_id = parse_id(url)

        except ValueError as exc:
            await progress.edit(
                f"❌ Invalid Eporner URL.\n{exc}"
            )
            return

        # -------------------------
        # Duplicate detection
        # -------------------------

        if await seen(video_id):
            await progress.edit(
                "⚠️ This video has already been posted."
            )
            return

        # -------------------------
        # Fetch metadata
        # -------------------------

        await progress.edit(
            "🔎 Fetching video information..."
        )

        metadata = await fetch_meta(video_id)

        title = get_title(metadata)

        if CLEAN:
            title = clean_title(title)

        duration = get_duration(metadata)

        # -------------------------
        # Select resolution
        # -------------------------

        source = pick_source(
            metadata,
            MAX_RES,
        )

        video_url = source["url"]

        resolution = source.get(
            "resolution",
            "Unknown",
        )

        # -------------------------
        # Generate filename
        # -------------------------

        filename = make_slug(
            title,
            video_id,
        )

        video_path = DOWNLOAD_DIR / f"{filename}.mp4"

        thumb_path = DOWNLOAD_DIR / f"{filename}.jpg"

        # -------------------------
        # Download progress
        # -------------------------

        await progress.edit(
            f"⬇️ Downloading video...\n"
            f"🎬 {title}\n"
            f"📺 Resolution: {resolution}"
        )

        last_step = -1

        async def progress_callback(
            downloaded: int,
            total: int,
        ):

            nonlocal last_step

            if total <= 0:
                return

            percent = min(
                100,
                int(downloaded * 100 / total),
            )

            step = percent // 10

            if step <= last_step:
                return

            last_step = step

            await progress.edit(
                f"⬇️ Downloading...\n"
                f"📊 Progress: {percent}%\n"
                f"📦 {human(downloaded)} / {human(total)}"
            )

        video_path = await download(
            video_url,
            video_path.name,
            progress_callback,
        )

        # -------------------------
        # Download thumbnail
        # -------------------------

        thumbnail_url = get_thumbnail(metadata)

        if thumbnail_url:
            try:
                thumb_path = await download_thumbnail(
                    thumbnail_url,
                    thumb_path.name,
                )

            except Exception as exc:
                logger.warning(
                    "Thumbnail download failed: %s",
                    exc,
                )

                thumb_path = None

        # -------------------------
        # Compression
        # -------------------------

        original_size = (
            video_path.stat().st_size / (1024 * 1024)
        )

        if original_size > COMPRESS_ABOVE_MB:

            await progress.edit(
                f"⚙️ Compressing video...\n"
                f"📦 Original: {original_size:.2f} MB"
            )

            processed_path = DOWNLOAD_DIR / (
                f"{filename}-compressed.mp4"
            )

            await compress_video(
                video_path,
                processed_path,
            )

        elif CLEAN:

            await progress.edit(
                "🧹 Removing video metadata..."
            )

            processed_path = DOWNLOAD_DIR / (
                f"{filename}-clean.mp4"
            )

            await strip_metadata(
                video_path,
                processed_path,
            )

        final_path = processed_path or video_path

        final_size = (
            final_path.stat().st_size / (1024 * 1024)
        )

        # -------------------------
        # Prepare caption
        # -------------------------

        keywords = get_keywords(metadata)

        caption = make_caption(
            title,
            keywords,
        )

        # -------------------------
        # Video attributes
        # -------------------------

        width, height = await get_video_dimensions(
            final_path
        )

        attributes = [
            DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True,
            )
        ]

        # -------------------------
        # Upload using MTProto
        # -------------------------

        await progress.edit(
            f"⬆️ Uploading video...\n"
            f"📦 Size: {final_size:.2f} MB"
        )

        target = TARGET_CHANNEL

        if target.lstrip("-").isdigit():
            target = int(target)

        sent_message = await user_client.send_file(
            entity=target,
            file=str(final_path),
            caption=caption,
            thumb=str(thumb_path) if thumb_path else None,
            supports_streaming=True,
            attributes=attributes,
            force_document=False,
            progress_callback=None,
        )

        # -------------------------
        # Save duplicate record
        # -------------------------

        await mark(
            video_id,
            sent_message.id,
        )

        await progress.edit(
            f"✅ Upload completed!\n\n"
            f"🎬 {title}\n"
            f"📺 {resolution}\n"
            f"📦 {final_size:.2f} MB\n"
            f"🆔 {video_id}"
        )

        logger.info(
            "Video uploaded successfully: %s",
            video_id,
        )

    except Exception as exc:

        logger.exception(
            "Video processing failed"
        )

        try:
            await progress.edit(
                f"❌ Processing failed.\n\n"
                f"`{str(exc)[:1500]}`"
            )

        except Exception:
            pass

    finally:

        # -------------------------
        # Cleanup temporary files
        # -------------------------

        for path in (
            video_path,
            thumb_path,
            processed_path,
        ):

            if path and isinstance(path, Path):

                try:
                    path.unlink(
                        missing_ok=True
                    )

                except OSError:
                    logger.warning(
                        "Could not delete temporary file: %s",
                        path,
      )
