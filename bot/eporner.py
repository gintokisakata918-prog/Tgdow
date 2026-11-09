"""Eporner URL validation and yt-dlp metadata adapter."""
import re
from typing import Any
from urllib.parse import urlparse

from bot.config import MAX_RES
from bot.engines.ytdlp import extract, YtDlpError

VIDEO_ID_PATTERN = re.compile(r"(?:video-|hd-porn/|embed/)([A-Za-z0-9]{5,12})", re.I)


class EpornerError(RuntimeError):
    pass


class VideoNotFoundError(EpornerError):
    pass


class NoStreamsAvailableError(EpornerError):
    pass


def parse_id(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("Invalid URL.")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("eporner.com", "www.eporner.com"):
        raise ValueError("Only Eporner URLs are supported.")
    match = VIDEO_ID_PATTERN.search(parsed.path)
    if not match:
        raise ValueError("Video ID not found in URL.")
    return match.group(1)


def get_resolution(value: Any) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return 0
    match = re.search(r"(?<!\d)(\d{3,4})\s*p?", value, re.I)
    return int(match.group(1)) if match else 0


async def fetch_meta(video_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{5,12}", video_id):
        raise ValueError("Invalid video ID.")
    url = f"https://www.eporner.com/video-{video_id}/"
    try:
        info = await extract(url, MAX_RES)
    except YtDlpError as exc:
        raise NoStreamsAvailableError(str(exc)) from exc
    return {
        "id": video_id,
        "title": info["title"],
        "length_sec": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
        "keywords": info.get("tags") or [],
        "videos": info["formats"],
        "webpage_url": info.get("webpage_url") or url,
    }


def pick_source(metadata: dict[str, Any], max_res: int = MAX_RES) -> dict[str, Any]:
    streams = [
        item for item in metadata.get("videos", [])
        if get_resolution(item.get("resolution")) <= max_res
    ]
    if not streams:
        raise NoStreamsAvailableError(f"No stream available below {max_res}p.")
    return max(streams, key=lambda item: get_resolution(item.get("resolution")))


def build_candidates(metadata: dict[str, Any], video_id: str) -> list[str]:
    return [
        item["url"] for item in metadata.get("videos", [])
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and item["url"].startswith(("https://", "http://"))
    ]


def get_duration(metadata: dict[str, Any]) -> int:
    try:
        return max(0, int(metadata.get("length_sec") or 0))
    except (TypeError, ValueError):
        return 0


def get_keywords(metadata: dict[str, Any]) -> list[str]:
    values = metadata.get("keywords") or []
    if isinstance(values, str):
        values = values.split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def get_thumbnail(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("thumbnail")
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def get_title(metadata: dict[str, Any]) -> str:
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise VideoNotFoundError("Video title unavailable.")
    return title.strip()
