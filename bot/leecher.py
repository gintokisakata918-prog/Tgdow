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
    COMPRESS_ABOVE_MB,
    CLEAN,
    FFMPEG_BIN,
    FFMPEG_VIDEO_CODEC,
    FFMPEG_AUDIO_CODEC,
    FFMPEG_CRF,
    FFMPEG_AUDIO_BITRATE,
    FFMPEG_MAX_WIDTH,
    FFMPEG_EXTRA_ARGS,
    CLEAN_CAPTION_TEMPLATE,
    CAPTION_TEMPLATE,
)

from bot.database import seen, mark

from bot.eporner import (
    parse_id,
    fetch_meta,
    pick_source,
    build_candidates,
    get_title,
    get_duration,
    get_keywords,
    get_thumbnail,
)

from bot.downloader import download_thumbnail
from bot.engines.ytdlp import download as ytdlp_download


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
# FFMPEG
# --------------------------------------------------

async def compress_video(input_path, output_path):

    command = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        f"scale='min({FFMPEG_MAX_WIDTH},iw)':-2",
        "-c:v",
        FFMPEG_VIDEO_CODEC,
        "-preset",
        "veryfast",
        "-crf",
        str(FFMPEG_CRF),
        "-c:a",
        FFMPEG_AUDIO_CODEC,
        "-b:a",
        FFMPEG_AUDIO_BITRATE,
        *FFMPEG_EXTRA_ARGS,
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")[-2000:]
        )

    return Path(output_path)


async def strip_metadata(input_path, output_path):

    command = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-map_metadata",
        "-1",
        "-c",
        "copy",
        str(output_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")[-2000:]
        )

    return Path(output_path)


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

    if not force and now - last_update < 2:
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
    processed_path = None

    try:

        # Fetch metadata

        await message.edit(
            "🔎 Fetching video information..."
        )

        metadata = await fetch_meta(video_id)

        title = get_title(metadata)
        duration = get_duration(metadata)
        keywords = get_keywords(metadata)

        # Select exact quality

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

        if selected_resolution:

            if resolution:

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

        loop = asyncio.get_running_loop()

        def ytdlp_progress(data):
            if data.get("status") not in ("downloading", "finished"):
                return
            current = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            asyncio.run_coroutine_threadsafe(
                download_progress(current, total), loop
            )

        video_path = await ytdlp_download(
            url=url,
            resolution=selected_resolution or 1080,
            filename=filename,
            progress_hook=ytdlp_progress,
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

        # Compression / metadata cleaning

        file_size_mb = (
            video_path.stat().st_size / (1024 * 1024)
        )

        if file_size_mb > COMPRESS_ABOVE_MB:

            await message.edit(
                "⚙️ Compressing video...\n"
                "Please wait."
            )

            processed_path = DOWNLOAD_DIR / (
                f"{video_id}_compressed.mp4"
            )

            await compress_video(
                video_path,
                processed_path,
            )

            video_path.unlink(missing_ok=True)
            video_path = processed_path

        elif CLEAN:

            await message.edit(
                "🧹 Cleaning video metadata..."
            )

            processed_path = DOWNLOAD_DIR / (
                f"{video_id}_clean.mp4"
            )

            await strip_metadata(
                video_path,
                processed_path,
            )

            video_path.unlink(missing_ok=True)
            video_path = processed_path

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

        # Prepare Telegram attributes

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

        # Telegram upload progress

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

        # Send video to same chat

        sent_message = await bot_client.send_file(
            entity=message.chat_id,
            file=str(video_path),
            caption=caption,
            thumb=(
                str(thumbnail_path)
                if thumbnail_path and thumbnail_path.exists()
                else None
            ),
            attributes=attributes,
            supports_streaming=True,
            reply_to=message.id,
            progress_callback=upload_progress,
        )

        await show_progress(
            message,
            f"📤 Uploading {selected_resolution or 1080}P video",
            video_path.stat().st_size,
            video_path.stat().st_size,
            upload_start,
            upload_last_update,
            force=True,
        )

        # Save database record

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

        detail = f"{type(error).__name__}: {error}".strip(": ")

        try:

            await message.edit(
                f"❌ Failed:\n"
                f"{detail[:700] or 'no details — check Colab logs'}"
            )

        except Exception:
            pass

    finally:

        # Cleanup temporary files

        for path in (
            video_path,
            thumbnail_path,
            processed_path,
        ):

            if path:

                try:
                    Path(path).unlink(missing_ok=True)

                except Exception:
                    logger.exception(
                        "File cleanup failed"
                )
    
