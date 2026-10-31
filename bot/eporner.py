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
    r"(?:video-|hd-porn/|embed/)([A-Za-z0-9]{5,12})",
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


def encode_base36(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"

    if number == 0:
        return "0"

    result = ""

    while number:
        number, remainder = divmod(number, 36)
        result = alphabet[remainder] + result

    return result


def calculate_hash(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise EpornerError("Invalid player hash.")

    return "".join(
        encode_base36(int(value[index:index + 8], 16))
        for index in range(0, 32, 8)
    )


async def fetch_metadata(
    client: httpx.AsyncClient,
    video_id: str,
) -> dict[str, Any]:

    params = {
        "id": video_id,
        "thumbsize": "big",
        "per_page": 1,
    }

    try:
        response = await client.get(
            EPORNER_API,
            params=params,
        )

        response.raise_for_status()
        data = response.json()

    except (httpx.HTTPError, ValueError) as exc:
        raise EpornerError(
            f"Eporner API request failed: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise VideoNotFoundError("Invalid API response.")

    if data.get("error"):
        raise VideoNotFoundError(str(data["error"]))

    # Support API responses containing a videos list.
    if isinstance(data.get("videos"), list):
        videos = data["videos"]

        video = next(
            (
                item for item in videos
                if isinstance(item, dict)
                and str(item.get("id", "")) == video_id
            ),
            None,
        )

        if video is None:
            raise VideoNotFoundError("Video not found.")

        data = video

    if str(data.get("id", "")) != video_id:
        raise VideoNotFoundError("Video ID mismatch.")

    return data


async def fetch_player_streams(
    client: httpx.AsyncClient,
    video_id: str,
) -> list[dict[str, Any]]:

    page_url = (
        f"https://www.eporner.com/video-{video_id}/"
    )

    try:
        response = await client.get(page_url)
        response.raise_for_status()

        html = response.text

    except httpx.HTTPError as exc:
        raise EpornerError(
            f"Could not open Eporner video page: {exc}"
        ) from exc

    hash_match = re.search(
        r'hash\s*[:=]\s*["\']([0-9a-fA-F]{32})',
        html,
    )

    if not hash_match:
        raise NoStreamsAvailableError(
            "Player hash not found on video page."
        )

    player_hash = calculate_hash(hash_match.group(1))

    params = {
        "hash": player_hash,
        "device": "generic",
        "domain": "www.eporner.com",
        "fallback": "false",
    }

    xhr_url = (
        f"https://www.eporner.com/xhr/video/{video_id}"
    )

    try:
        response = await client.get(
            xhr_url,
            params=params,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page_url,
            },
        )

        response.raise_for_status()
        player_data = response.json()

    except (httpx.HTTPError, ValueError) as exc:
        raise EpornerError(
            f"Player stream request failed: {exc}"
        ) from exc

    if not isinstance(player_data, dict):
        raise EpornerError("Invalid player response.")

    if player_data.get("available") is False:
        raise VideoNotFoundError(
            player_data.get(
                "message",
                "Video is unavailable.",
            )
        )

    sources = player_data.get("sources", {})

    if not isinstance(sources, dict):
        raise NoStreamsAvailableError(
            "No video sources found."
        )

    streams = []

    for source_type, formats in sources.items():

        if not isinstance(formats, dict):
            continue

        # Direct MP4 sources only.
        if source_type == "hls":
            continue

        for format_id, format_data in formats.items():

            if not isinstance(format_data, dict):
                continue

            source_url = format_data.get("src")

            if not isinstance(source_url, str):
                continue

            if not source_url.startswith("https://"):
                continue

            resolution_match = re.search(
                r"(\d{3,4})p",
                str(format_id),
                re.IGNORECASE,
            )

            if not resolution_match:
                continue

            resolution = int(resolution_match.group(1))

            streams.append({
                "resolution": f"{resolution}p",
                "url": source_url,
                "type": source_type,
            })

    # Remove duplicate resolutions and URLs.
    unique_streams = {}

    for stream in streams:
        unique_streams[
            (
                stream["resolution"],
                stream["url"],
            )
        ] = stream

    return list(unique_streams.values())


async def fetch_meta(video_id: str) -> dict[str, Any]:

    if not re.fullmatch(r"[A-Za-z0-9]{5,12}", video_id):
        raise ValueError("Invalid video ID.")

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": EPORNER_REFERER,
    }

    timeout = httpx.Timeout(
        connect=30,
        read=60,
        write=30,
        pool=30,
    )

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        metadata = await fetch_metadata(
            client,
            video_id,
        )

        streams = await fetch_player_streams(
            client,
            video_id,
        )

    metadata["videos"] = streams

    if not streams:
        raise NoStreamsAvailableError(
            "Eporner player returned no direct MP4 streams."
        )

    return metadata


def pick_source(
    metadata: dict[str, Any],
    max_res: int = MAX_RES,
) -> dict[str, Any]:

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
        available = sorted({
            get_resolution(
                stream.get("resolution", "")
            )
            for stream in streams
        })

        if available:
            raise NoStreamsAvailableError(
                f"No stream available below {max_res}p. "
                f"Available: {available}"
            )

        raise NoStreamsAvailableError(
            "No downloadable streams found."
        )

    return max(
        suitable,
        key=lambda stream: get_resolution(
            stream.get("resolution", "")
        ),
    )


def get_duration(metadata: dict[str, Any]) -> int:
    try:
        minutes = metadata.get("length_min", 0)
        seconds = metadata.get("length_sec", 0)

        if isinstance(minutes, str) and ":" in minutes:
            parts = minutes.split(":")
            return int(parts[0]) * 60 + int(parts[1])

        return max(
            0,
            int(minutes) * 60 + int(seconds),
        )

    except (TypeError, ValueError):
        return 0


def get_keywords(metadata: dict[str, Any]) -> list[str]:
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

    if not isinstance(thumbnail, str):
        thumbnail = metadata.get("default_thumb", "")

    if isinstance(thumbnail, str) and thumbnail.startswith("https://"):
        return thumbnail

    return None


def get_title(metadata: dict[str, Any]) -> str:
    title = metadata.get("title", "")

    if not isinstance(title, str) or not title.strip():
        raise VideoNotFoundError(
            "Video title unavailable."
        )

    return title.strip()
