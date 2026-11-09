"""yt-dlp extraction and download engine."""
from __future__ import annotations
import asyncio
import re
from pathlib import Path
from typing import Any, Callable
import yt_dlp
from bot.config import DOWNLOAD_DIR, MAX_RES, USER_AGENT, EPORNER_REFERER

class YtDlpError(RuntimeError):
    pass

def _options():
    return {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        "extractor_retries": 2,
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

def _download(url: str, resolution: int, output: Path, progress_hook: Callable | None):
    output.parent.mkdir(parents=True, exist_ok=True)
    options = _options()
    options.update({"format": f"best[height<={resolution}]",
                    "outtmpl": str(output.with_suffix(".%(ext)s")),
                    "merge_output_format": "mp4", "continuedl": True,
                    "overwrites": True,
                    "progress_hooks": [progress_hook] if progress_hook else []})
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
        paths = [prepared, prepared.with_suffix(".mp4")]
        final = next((p for p in paths if p.is_file()), None)
        if final is None:
            matches = sorted(output.parent.glob(output.stem + ".*"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            final = matches[0] if matches else None
        if final is None or final.stat().st_size <= 0:
            raise YtDlpError("Download finished but no valid output file was found.")
        target = output.with_suffix(final.suffix)
        if final != target:
            if target.exists(): target.unlink()
            final.replace(target)
        return target
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).strip() or "Video download failed.") from exc

async def download(url: str, resolution: int, filename: str,
                   progress_hook: Callable | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename).strip("._") or "video.mp4"
    return await asyncio.to_thread(_download, url, resolution, DOWNLOAD_DIR / safe, progress_hook)
