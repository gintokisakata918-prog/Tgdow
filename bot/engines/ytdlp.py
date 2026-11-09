"""yt-dlp based Eporner extractor and downloader.

All blocking yt-dlp work runs in worker threads so Telethon's event loop
remains responsive. No player-hash scraping or guessed media URLs are used.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from bot.config import DOWNLOAD_DIR, MAX_RES, USER_AGENT, EPORNER_REFERER

log = logging.getLogger(__name__)


class YtDlpError(RuntimeError):
    """Raised when yt-dlp cannot inspect or download a video."""


def _options(*, quiet: bool = True) -> dict[str, Any]:
    return {
        "quiet": quiet,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Referer": EPORNER_REFERER,
        },
    }


def _extract(url: str) -> dict[str, Any]:
    try:
        with yt_dlp.YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise YtDlpError("Extractor returned invalid video information.")
        return info
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).strip() or "Could not inspect this video.") from exc


def _normalise_formats(info: dict[str, Any], max_res: int) -> list[dict[str, Any]]:
    formats = info.get("formats") or []
    selected: dict[int, dict[str, Any]] = {}
    for item in formats:
        if not isinstance(item, dict):
            continue
        height = item.get("height")
        media_url = item.get("url")
        if not isinstance(height, int) or height < 1 or height > max_res:
            continue
        if not isinstance(media_url, str) or not media_url.startswith(("https://", "http://")):
            continue
        # Prefer a format with both audio and video, then the larger bitrate.
        score = (item.get("acodec") not in (None, "none"), item.get("vbr") or item.get("tbr") or 0)
        old = selected.get(height)
        if old is None or score > old["_score"]:
            selected[height] = {
                "resolution": f"{height}p",
                "url": media_url,
                "format_id": str(item.get("format_id") or ""),
                "ext": str(item.get("ext") or "mp4"),
                "format": str(item.get("format") or ""),
                "_score": score,
            }
    result = []
    for item in selected.values():
        item.pop("_score", None)
        result.append(item)
    return sorted(result, key=lambda item: int(item["resolution"][:-1]))


async def extract(url: str, max_res: int = MAX_RES) -> dict[str, Any]:
    info = await asyncio.to_thread(_extract, url)
    formats = _normalise_formats(info, max_res)
    if not formats:
        # Some extractors expose a single combined format at the top level.
        height = info.get("height")
        media_url = info.get("url")
        if isinstance(height, int) and height <= max_res and isinstance(media_url, str):
            formats = [{
                "resolution": f"{height}p",
                "url": media_url,
                "format_id": str(info.get("format_id") or "best"),
                "ext": str(info.get("ext") or "mp4"),
            }]
    if not formats:
        raise YtDlpError(f"No downloadable format found up to {max_res}p.")
    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or "Untitled video"),
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "formats": formats,
        "extractor": info.get("extractor_key") or info.get("extractor") or "unknown",
    }


def _download(url: str, resolution: int, output: Path, progress_hook: Callable | None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    fmt = f"best[height<={resolution}]"
    options = _options(quiet=True)
    options.update({
        "format": fmt,
        "outtmpl": str(output.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "continuedl": True,
        "overwrites": False,
        "progress_hooks": [progress_hook] if progress_hook else [],
    })
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = Path(ydl.prepare_filename(info))
        candidates = [filename, filename.with_suffix(".mp4"), output]
        final = next((path for path in candidates if path.is_file()), None)
        if final is None:
            matches = sorted(output.parent.glob(output.stem + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
            final = matches[0] if matches else None
        if final is None:
            raise YtDlpError("Download finished but output file was not found.")
        target = output if final.suffix.lower() == output.suffix.lower() else output.with_suffix(final.suffix)
        if final != target:
            final.replace(target)
        return target
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).strip() or "Video download failed.") from exc


async def download(url: str, resolution: int, filename: str, progress_hook: Callable | None = None) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "video.mp4"
    output = DOWNLOAD_DIR / safe_name
    return await asyncio.to_thread(_download, url, resolution, output, progress_hook)
