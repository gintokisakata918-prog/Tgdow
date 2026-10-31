import re
from typing import Any
from urllib.parse import urlparse

import httpx

from bot.config import (
    EPORNER_API,
    EPORNER_REFERER,
    USER_AGENT,
    MAX_RES,
)


VIDEO_ID_PATTERN = re.compile(
    r"(?:video-|hd-porn/)([A-Za-z0-9]{5,12})",
    re.IGNORECASE,
)

RESOLUTION_PATTERN = re.compile(
    r"(\d{3,4})p?",
    re.IGNORECASE,
)


class EpornerError(Exception):
    pass


class VideoNotFoundError(EpornerError):
    pass


class NoStreamsAvailableError(EpornerError):
    pass


def parse_id(url: str) -> str:
    """Extract video ID from an Eporner URL."""

    if not isinstance(url, str):
        raise ValueError("Invalid URL.")

    parsed = urlparse(url.strip())

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Invalid URL scheme.")

    if parsed.hostname not in (
        "eporner.com",
        "www.eporner.com",
    ):
        raise ValueError("Only Eporner URLs are supported.")

    match = VIDEO_ID_PATTERN.search(parsed.path)

    if not match:
        raise ValueError("Video ID not found in URL.")

    return match.group(1)


def get_resolution(value: Any) -> int:
    if not isinstance(value, str):
        return 0

    match = RESOLUTION_PATTERN.search(value)

    return int(match.group(1)) if match else 0


async def fetch_meta(video_id: str) -> dict[str, Any]:
    """Fetch video metadata from Eporner API v2."""

    if not re.fullmatch(r"[A-Za-z0-9]{5,12}", video_id):
        raise ValueError("Invalid video ID.")

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": EPORNER_REFERER,
    }

    params = {
        "id": video_id,
        "thumbsize": "big",
        "per_page": 1,
    }

    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=30,
            follow_redirects=True,
        ) as client:

            response = await client.get(
                EPORNER_API,
                params=params,
            )

            response.raise_for_status()
            data = response.json()

    except httpx.HTTPError as exc:
        raise EpornerError(
            f"API request failed: {exc}"
        ) from exc

    except ValueError as exc:
        raise EpornerError(
            "Invalid API response."
        ) from exc

    if not isinstance(data, dict) or not data:
        raise VideoNotFoundError("Video not found.")

    if data.get("error"):
        raise VideoNotFoundError(str(data["error"]))

    if str(data.get("id", "")) != video_id:
        raise VideoNotFoundError("Video ID mismatch.")

    streams = data.get("videos", [])

    if not isinstance(streams, list):
        streams = []

    data["videos"] = [
        stream
        for stream in streams
        if isinstance(stream, dict)
        and isinstance(stream.get("url"), str)
        and stream["url"].startswith("https://")
        and get_resolution(
            stream.get("resolution", "")
        ) > 0
    ]

    return data


def pick_source(
    metadata: dict[str, Any],
    max_res: int = MAX_RES,
) -> dict[str, Any]:
    """Select the highest stream not exceeding MAX_RES."""

    if max_res <= 0:
        raise ValueError("MAX_RES must be positive.")

    streams = metadata.get("videos", [])

    suitable = [
        stream
        for stream in streams
        if get_resolution(
            stream.get("resolution", "")
        ) <= max_res
    ]

    if not suitable:
        raise NoStreamsAvailableError(
            f"No stream available below {max_res}p."
        )

    return max(
        suitable,
        key=lambda stream: get_resolution(
            stream.get("resolution", "")
        ),
    )


def get_duration(metadata: dict[str, Any]) -> int:
    """Get duration in seconds."""

    try:
        minutes = int(metadata.get("length_min", 0))
        seconds = int(metadata.get("length_sec", 0))

        return max(0, minutes * 60 + seconds)

    except (TypeError, ValueError):
        return 0


def get_keywords(metadata: dict[str, Any]) -> list[str]:
    """Extract video keywords."""

    keywords = metadata.get("keywords", "")

    if isinstance(keywords, list):
        values = keywords

    elif isinstance(keywords, str):
        values = keywords.split(",")

    else:
        return []

    return [
        item.strip()
        for item in values
        if isinstance(item, str) and item.strip()
    ]


def get_thumbnail(metadata: dict[str, Any]) -> str | None:
    thumbnail = metadata.get("thumbnail")

    if (
        isinstance(thumbnail, str)
        and thumbnail.startswith("https://")
    ):
        return thumbnail

    return None


def get_title(metadata: dict[str, Any]) -> str:
    title = metadata.get("title", "")

    if not isinstance(title, str) or not title.strip():
        raise VideoNotFoundError("Video title unavailable.")

    return title.strip()
