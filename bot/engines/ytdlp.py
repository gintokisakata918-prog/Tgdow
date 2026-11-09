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
    """Keep every usable format candidate; prefer muxed audio/video streams."""
    result = []
    seen = set()
    for item in info.get("formats") or []:
        if not isinstance(item, dict):
            continue
        height, media_url = item.get("height"), item.get("url")
        if not isinstance(height, int) or not 1 <= height <= max_res:
            continue
        if not isinstance(media_url, str) or not media_url.startswith(("https://", "http://")):
            continue
        key = (height, str(item.get("format_id") or ""), media_url)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "resolution": f"{height}p",
            "url": media_url,
            "format_id": str(item.get("format_id") or ""),
            "ext": str(item.get("ext") or "mp4"),
            "has_audio": item.get("acodec") not in (None, "none"),
            "has_video": item.get("vcodec") not in (None, "none"),
            "tbr": item.get("tbr") or 0,
        })
    return sorted(
        result,
        key=lambda item: (
            int(item["resolution"][:-1]),
            item["has_audio"] and item["has_video"],
            item["tbr"],
        ),
        reverse=True,
    )

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
                        progress_hook({"status": "downloading", "downloaded_bytes": done,
                                       "total_bytes": total})
    if not output.is_file() or output.stat().st_size <= 0:
        raise YtDlpError("The server returned an empty video file.")
    return output


def _download_with_ytdlp(url: str, resolution: int, output: Path,
                         progress_hook: Callable | None):
    options = _options()
    options.update({
        "format": f"best[height={resolution}]/bestvideo[height={resolution}]+bestaudio",
        "outtmpl": str(output.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "overwrites": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
    })
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))
    choices = [prepared, prepared.with_suffix(".mp4")]
    final = next((path for path in choices if path.is_file()), None)
    if final is None:
        matches = sorted(output.parent.glob(output.stem + ".*"),
                         key=lambda path: path.stat().st_mtime, reverse=True)
        final = matches[0] if matches else None
    if final is None or final.stat().st_size <= 0:
        raise YtDlpError("yt-dlp completed without producing a valid file.")
    target = output.with_suffix(final.suffix)
    if final != target:
        target.unlink(missing_ok=True)
        final.replace(target)
    return target


def _download(url: str, resolution: int, output: Path,
              progress_hook: Callable | None):
    output.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(1, 4):
        try:
            # Re-extract each attempt so expiring signed URLs are refreshed.
            info = _extract(url)
            candidates = [
                item for item in _normalise_formats(info, resolution)
                if item["resolution"] == f"{resolution}p"
            ]
            if not candidates:
                raise YtDlpError(f"Exact {resolution}p stream is unavailable.")
            # Try muxed streams first; then alternate formats at the same height.
            candidates.sort(
                key=lambda item: (item["has_audio"] and item["has_video"], item["tbr"]),
                reverse=True,
            )
            for candidate in candidates:
                try:
                    output.unlink(missing_ok=True)
                    return _download_once(candidate["url"], EPORNER_REFERER,
                                           output, progress_hook)
                except Exception as exc:
                    last = exc
                    output.unlink(missing_ok=True)
            # If direct streams are separate audio/video tracks, let yt-dlp merge them.
            try:
                output.unlink(missing_ok=True)
                return _download_with_ytdlp(url, resolution, output, progress_hook)
            except Exception as exc:
                last = exc
                output.unlink(missing_ok=True)
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
