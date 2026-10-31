import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from telethon.tl.types import DocumentAttributeVideo

from bot.config import (
    CLEAN,
    COMPRESS_ABOVE_MB,
    DOWNLOAD_DIR,
    FFMPEG_BIN,
    FFMPEG_VIDEO_CODEC,
    FFMPEG_AUDIO_CODEC,
    FFMPEG_CRF,
    FFMPEG_AUDIO_BITRATE,
    FFMPEG_MAX_WIDTH,
    FFMPEG_EXTRA_ARGS,
    MAX_RES,
)
from bot.database import seen, mark
from bot.downloader import download, download_thumbnail, human
from bot.eporner import (
    fetch_meta,
    parse_id,
    pick_source,
    get_duration,
    get_keywords,
    get_thumbnail,
    get_title,
)

logger = logging.getLogger(__name__)


def clean_title(title: str) -> str:
    """Remove site branding and URLs from the title."""

    title = re.sub(
        r"https?://\S+|www\.\S+",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(
        r"\beporner\b",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(r"\s+", " ", title)
    return title.strip(" -_|")


def make_slug(title: str) -> str:
    title = clean_title(title) if CLEAN else title

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower())
    slug = slug.strip("-")

    return slug[:70] or "video"


def make_caption(title: str, keywords: list[str]) -> str:
    hashtags = []

    for keyword in keywords:
        tag = re.sub(r"[^a-zA-Z0-9]", "", keyword.replace(" ", ""))

        if tag:
            hashtags.append(f"#{tag}")

        if len(hashtags) >= 8:
            break

    caption = title

    if hashtags:
        caption += "\n\n" + " ".join(hashtags)

    return caption[:1024]


async def run_ffmpeg(arguments: list[str]) -> None:
    """Run FFmpeg without blocking the event loop."""

    process = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        error = stderr.decode("utf-8", errors="ignore")[-1500:]
        raise RuntimeError(f"FFmpeg failed: {error}")


async def compress_video(input_path: Path) -> Path:
    output_path = input_path.with_name(
        f"{input_path.stem}_compressed.mp4"
    )

    arguments = [
        "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", FFMPEG_VIDEO_CODEC,
        "-preset", "medium",
        "-crf", str(FFMPEG_CRF),
        "-vf", f"scale='min({FFMPEG_MAX_WIDTH},iw)':-2",
        "-c:a", FFMPEG_AUDIO_CODEC,
        "-b:a", FFMPEG_AUDIO_BITRATE,
        *FFMPEG_EXTRA_ARGS,
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        await run_ffmpeg(arguments)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("Compressed video is empty.")

        return output_path

    except Exception:
        output_path.unlink(missing_ok=True)
        raise


async def strip_metadata(input_path: Path) -> Path:
    """Remove metadata without re-encoding the video."""

    output_path = input_path.with_name(
        f"{input_path.stem}_clean.mp4"
    )

    arguments = [
        "-y",
        "-i", str(input_path),
        "-map", "0",
        "-c", "copy",
        *FFMPEG_EXTRA_ARGS,
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        await run_ffmpeg(arguments)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("Cleaned video is empty.")

        return output_path

    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def prepare_thumbnail(input_path: Path) -> Path:
    """Convert thumbnail to a small JPEG suitable for Telegram."""

    output_path = input_path.with_suffix(".jpg")

    with Image.open(input_path) as image:
        image = image.convert("RGB")
        image.thumbnail((320, 320))

        quality = 85

        while True:
            image.save(
                output_path,
                "JPEG",
                quality=quality,
                optimize=True,
            )

            if output_path.stat().st_size <= 190 * 1024:
                break

            quality -= 10

            if quality < 35:
                output_path.unlink(missing_ok=True)
                raise RuntimeError("Could not optimize thumbnail.")

    return output_path


async def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Read video dimensions using ffprobe."""

    ffprobe = shutil.which("ffprobe")

    if not ffprobe:
        return 1280, 720

    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _ = await process.communicate()

    try:
        width, height = stdout.decode().strip().split("x")
        return int(width), int(height)
    except (ValueError, TypeError):
        return 1280, 720


async def process_video(
    bot_client,
    message,
    url: str,
) -> None:
    """Download an Eporner video and send it to the same Telegram chat."""

    video_id = parse_id(url)

    if await seen(video_id):
        await message.reply("ℹ️ This video has already been downloaded.")
        return

    progress_message = await message.reply(
        "🔎 Fetching video information..."
    )

    downloaded_path = None
    thumbnail_path = None
    prepared_thumbnail = None
    compressed_path = None
    clean_path = None

    try:
        metadata = await fetch_meta(video_id)

        title = get_title(metadata)
        display_title = clean_title(title) if CLEAN else title

        source = pick_source(metadata, MAX_RES)
        video_url = source["url"]

        duration = get_duration(metadata)
        keywords = get_keywords(metadata)

        slug = make_slug(title)
        filename = f"{slug}_{video_id}.mp4"

        await progress_message.edit(
            "📥 Download starting..."
        )

        async def update_progress(current: int, total: int):
            percent = int(current * 100 / total)

            await progress_message.edit(
                f"📥 Downloading: {percent}%\n"
                f"📦 {human(current)} / {human(total)}"
            )

        downloaded_path = await download(
            url=video_url,
            filename=filename,
            progress_callback=update_progress,
        )

        final_path = downloaded_path

        file_size_mb = downloaded_path.stat().st_size / (1024 * 1024)

        if file_size_mb > COMPRESS_ABOVE_MB:
            await progress_message.edit(
                "🗜 Compressing video...\n"
                f"Original size: {human(downloaded_path.stat().st_size)}"
            )

            compressed_path = await compress_video(downloaded_path)
            final_path = compressed_path

        elif CLEAN:
            await progress_message.edit(
                "🧹 Removing video metadata..."
            )

            clean_path = await strip_metadata(downloaded_path)
            final_path = clean_path

        thumbnail_url = get_thumbnail(metadata)

        if thumbnail_url:
            try:
                thumbnail_path = await download_thumbnail(
                    thumbnail_url,
                    f"{video_id}_thumb",
                )

                prepared_thumbnail = prepare_thumbnail(thumbnail_path)

            except Exception:
                logger.warning(
                    "Thumbnail preparation failed for %s",
                    video_id,
                    exc_info=True,
                )

        width, height = await get_video_dimensions(final_path)

        caption = make_caption(
            display_title,
            keywords,
        )

        attributes = [
            DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True,
            )
        ]

        await progress_message.edit(
            "📤 Uploading video to Telegram..."
        )

        sent_message = await bot_client.send_file(
            entity=message.chat_id,
            file=str(final_path),
            caption=caption,
            thumb=str(prepared_thumbnail) if prepared_thumbnail else None,
            attributes=attributes,
            supports_streaming=True,
            reply_to=message.id,
        )

        await mark(
            video_id,
            sent_message.id,
        )

        await progress_message.delete()

        logger.info(
            "Video %s sent successfully to chat %s",
            video_id,
            message.chat_id,
        )

    except Exception as exc:
        logger.exception(
            "Failed to process video %s",
            video_id,
        )

        try:
            await progress_message.edit(
                f"❌ Download failed:\n{str(exc)[:700]}"
            )
        except Exception:
            pass

    finally:
        # Remove downloaded and processed files.
        for path in (
            downloaded_path,
            compressed_path,
            clean_path,
            thumbnail_path,
            prepared_thumbnail,
        ):
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    logger.warning(
                        "Could not remove temporary file: %s",
                        path,
                    )
