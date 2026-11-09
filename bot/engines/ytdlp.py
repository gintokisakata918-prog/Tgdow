"""yt-dlp extraction and download engine."""
from __future__ import annotations
import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable
import yt_dlp
import httpx
from bot.config import DOWNLOAD_DIR, MAX_RES, USER_AGENT, EPORNER_REFERER

class YtDlpError(RuntimeError):
    pass

def _options():
    return {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 60,
        "retries": 10, "fragment_retries": 10, "extractor_retries": 3,
        "http_chunk_size": 10 * 1024 * 1024,
        "source_address": "0.0.0.0",
        "nocheckcertificate": True,
        "continuedl": True,
        "http_headers": {"User-Agent": USER_AGENT, "Referer": EPORNER_REFERER},
    }

def _extract(url):
    try:
        with yt_dlp.YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise YtDlpError("Extractor returned invalid video information.")
        return info
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).strip() or "Could not inspect this video.") from exc

def _normalise_formats(info, max_res):
    selected = {}
    for item in info.get("formats") or []:
        if not isinstance(item, dict): continue
        height, media_url = item.get("height"), item.get("url")
        if not isinstance(height, int) or not 1 <= height <= max_res: continue
        if not isinstance(media_url, str) or not media_url.startswith(("https://", "http://")): continue
        score = (item.get("acodec") not in (None, "none"), item.get("vbr") or item.get("tbr") or 0)
        if height not in selected or score > selected[height]["_score"]:
            selected[height] = {"resolution": f"{height}p", "url": media_url,
                                "format_id": str(item.get("format_id") or ""),
                                "ext": str(item.get("ext") or "mp4"), "_score": score}
    result = list(selected.values())
    for item in result: item.pop("_score", None)
    return sorted(result, key=lambda x: int(x["resolution"][:-1]))

async def extract(url: str, max_res: int = MAX_RES) -> dict[str, Any]:
    info = await asyncio.to_thread(_extract, url)
    formats = _normalise_formats(info, max_res)
    if not formats:
        height, media_url = info.get("height"), info.get("url")
        if isinstance(height, int) and 1 <= height <= max_res and isinstance(media_url, str):
            formats = [{"resolution": f"{height}p", "url": media_url,
                        "format_id": str(info.get("format_id") or "best"),
                        "ext": str(info.get("ext") or "mp4")}]
    if not formats:
        raise YtDlpError(f"No downloadable format found up to {max_res}p.")
    return {"id": str(info.get("id") or ""), "title": str(info.get("title") or "Untitled video"),
            "duration": int(info.get("duration") or 0), "thumbnail": info.get("thumbnail"),
            "tags": info.get("tags") or [], "webpage_url": info.get("webpage_url") or url,
            "formats": formats}

def _download_once(stream_url: str, referer: str | None, output: Path,
                    progress_hook: Callable | None):
    headers = {"User-Agent": USER_AGENT, "Referer": referer or EPORNER_REFERER}
    timeout = httpx.Timeout(60.0, connect=30.0)
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        with client.stream("GET", stream_url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with output.open("wb") as file:
                for chunk in response.iter_bytes(1 << 20):
                    if not chunk:
                        continue
                    file.write(chunk)
                    done += len(chunk)
                    if progress_hook:
                        progress_hook({
                            "status": "downloading",
                            "downloaded_bytes": done,
                            "total_bytes": total,
                        })
    if not output.is_file() or output.stat().st_size <= 0:
        raise YtDlpError("The server returned an empty video file.")
    return output


def _download(url: str, resolution: int, output: Path,
              progress_hook: Callable | None):
    output.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(1, 4):
        try:
            info = _extract(url)
            formats = _normalise_formats(info, resolution)
            selected = next(
                (item for item in formats
                 if item["resolution"] == f"{resolution}p"),
                None,
            )
            selected = selected or (formats[-1] if formats else None)
            if not selected:
                raise YtDlpError(
                    f"No downloadable format found up to {resolution}p."
                )
            return _download_once(
                selected["url"], EPORNER_REFERER, output, progress_hook
            )
        except Exception as exc:
            last = exc
            output.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(2 * attempt)
    raise YtDlpError(str(last).strip() or "Video download failed.") from last

async def download(url: str, resolution: int, filename: str,
                   progress_hook: Callable | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "video.mp4"
    return await asyncio.to_thread(_download, url, resolution, DOWNLOAD_DIR / safe, progress_hook)
