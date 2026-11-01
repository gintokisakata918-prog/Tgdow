import os
import re
import time
import shutil
import asyncio
from pathlib import Path
from typing import Callable, Awaitable

import httpx

from bot.config import (
    USER_AGENT,
    EPORNER_REFERER,
    CHUNK_SIZE,
    DOWNLOAD_DIR,
)

ProgressCallback = Callable[[int, int], Awaitable[None]]

# Parallel connections per file. Colab pe 16 theek hai.
# Eporner 403/429 de to 8 kar do.
ARIA2_CONNECTIONS = int(os.getenv("ARIA2_CONNECTIONS", "16"))
ARIA2_SPLIT_MB = "1M"


def human(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{value:.2f} TB"


def aria2_available() -> bool:
    return shutil.which("aria2c") is not None


async def probe_size(
    url: str,
    headers: dict[str, str],
) -> int:
    """Content-Length / Content-Range se total size nikaalo."""

    try:
        async with httpx.AsyncClient(
            headers={**headers, "Range": "bytes=0-0"},
            timeout=httpx.Timeout(30),
            follow_redirects=True,
            trust_env=False,
        ) as client:

            response = await client.get(url)

        content_range = response.headers.get("content-range", "")

        match = re.search(r"/(\d+)\s*$", content_range)

        if match:
            return int(match.group(1))

        if response.status_code == 200:
            return int(response.headers.get("content-length", 0))

    except Exception:
        pass

    return 0


async def watch_progress(
    path: Path,
    process: asyncio.subprocess.Process,
    total: int,
    progress_callback: ProgressCallback | None,
    stop: asyncio.Event,
) -> None:

    if not progress_callback:
        return

    while not stop.is_set():

        try:
            current = path.stat().st_size
        except OSError:
            current = 0

        try:
            await progress_callback(current, total)
        except Exception:
            pass

        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            continue

        break


async def download_aria2(
    url: str,
    temporary_path: Path,
    headers: dict[str, str],
    progress_callback: ProgressCallback | None,
) -> Path:
    """aria2c se multi-connection download (Range resume ke saath)."""

    total = await probe_size(url, headers)

    command = [
        "aria2c",
        "--continue=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--file-allocation=none",
        f"--max-connection-per-server={ARIA2_CONNECTIONS}",
        f"--split={ARIA2_CONNECTIONS}",
        f"--min-split-size={ARIA2_SPLIT_MB}",
        "--max-tries=5",
        "--retry-wait=3",
        "--connect-timeout=30",
        "--timeout=60",
        "--lowest-speed-limit=10K",
        "--summary-interval=0",
        "--console-log-level=warn",
        f"--user-agent={headers['User-Agent']}",
        f"--referer={headers['Referer']}",
        "--dir",
        str(temporary_path.parent),
        "--out",
        temporary_path.name,
        url,
    ]

    print(
        f"aria2c: {ARIA2_CONNECTIONS} connections -> "
        f"{httpx.URL(url).host}"
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stop = asyncio.Event()

    watcher = asyncio.create_task(
        watch_progress(
            temporary_path,
            process,
            total,
            progress_callback,
            stop,
        )
    )

    try:
        _, stderr = await process.communicate()
    finally:
        stop.set()
        watcher.cancel()

    if process.returncode != 0:
        detail = stderr.decode(errors="ignore").strip().splitlines()
        detail = " | ".join(detail[-3:])[:400]

        raise RuntimeError(
            f"aria2c failed (exit {process.returncode}): "
            f"{detail or 'no output'}"
        )

    return temporary_path


async def download_httpx(
    url: str,
    temporary_path: Path,
    headers: dict[str, str],
    progress_callback: ProgressCallback | None,
) -> Path:
    """Fallback: single connection, Range resume, descriptive errors."""

    timeout = httpx.Timeout(
        connect=60,
        read=300,
        write=30,
        pool=30,
    )

    limits = httpx.Limits(
        max_connections=5,
        max_keepalive_connections=2,
    )

    last_error: Exception | None = None

    for attempt in range(1, 4):

        resume_from = (
            temporary_path.stat().st_size
            if temporary_path.exists()
            else 0
        )

        request_headers = dict(headers)

        if resume_from:
            request_headers["Range"] = f"bytes={resume_from}-"

        downloaded = resume_from
        last_update = 0
        start_time = time.monotonic()

        try:
            print(
                f"Download attempt {attempt}/3 "
                f"(resume from {human(resume_from)})"
            )

            async with httpx.AsyncClient(
                headers=request_headers,
                timeout=timeout,
                follow_redirects=True,
                trust_env=False,
                limits=limits,
            ) as client:

                async with client.stream("GET", url) as response:

                    response.raise_for_status()

                    if response.status_code == 206:

                        content_range = response.headers.get(
                            "content-range", ""
                        )

                        match = re.search(
                            r"/(\d+)\s*$",
                            content_range,
                        )

                        total = (
                            int(match.group(1))
                            if match
                            else resume_from + int(
                                response.headers.get(
                                    "content-length", 0
                                )
                            )
                        )

                        mode = "ab"

                    else:

                        total = int(
                            response.headers.get(
                                "content-length", 0
                            )
                        )

                        downloaded = 0
                        mode = "wb"

                    with open(temporary_path, mode) as file:

                        async for chunk in response.aiter_bytes(
                            chunk_size=CHUNK_SIZE
                        ):

                            if not chunk:
                                continue

                            file.write(chunk)
                            downloaded += len(chunk)

                            now = time.monotonic()

                            if (
                                progress_callback
                                and now - last_update >= 2
                            ):

                                await progress_callback(
                                    downloaded,
                                    total,
                                )

                                last_update = now

            if downloaded == 0:
                raise RuntimeError(
                    "Downloaded file is empty."
                )

            print(
                f"Download completed: {human(downloaded)} "
                f"in {time.monotonic() - start_time:.2f}s"
            )

            return temporary_path

        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ) as error:

            last_error = error

            print(
                f"Attempt {attempt}/3 failed: "
                f"{type(error).__name__}: {error}"
            )

            if attempt < 3:
                await asyncio.sleep(attempt * 5)

        except httpx.HTTPStatusError as error:

            raise RuntimeError(
                f"CDN rejected: HTTP "
                f"{error.response.status_code} for "
                f"{error.request.url.host}"
            ) from error

    raise RuntimeError(
        f"Download failed after 3 attempts "
        f"({type(last_error).__name__}: "
        f"{last_error or 'no details'})"
    ) from last_error


async def download(
    url: str,
    filename: str,
    progress_callback: ProgressCallback | None = None,
) -> Path:

    if not url.startswith("https://"):
        raise ValueError("Only HTTPS download URLs are supported.")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    output_path = DOWNLOAD_DIR / filename
    temporary_path = output_path.with_suffix(
        output_path.suffix + ".part"
    )

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": EPORNER_REFERER,
        "Accept": "*/*",
    }

    use_aria2 = aria2_available()

    try:
        if use_aria2:

            try:
                await download_aria2(
                    url,
                    temporary_path,
                    headers,
                    progress_callback,
                )

            except Exception as error:

                print(
                    f"aria2c path failed "
                    f"({type(error).__name__}: {error}), "
                    f"falling back to single connection"
                )

                temporary_path.unlink(missing_ok=True)

                await download_httpx(
                    url,
                    temporary_path,
                    headers,
                    progress_callback,
                )

        else:

            print(
                "aria2c not installed, using single connection "
                "(apt install aria2 for parallel download)"
            )

            await download_httpx(
                url,
                temporary_path,
                headers,
                progress_callback,
            )

        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            raise RuntimeError("Downloaded file is empty.")

        os.replace(temporary_path, output_path)

        return output_path

    except Exception:
        temporary_path.unlink(missing_ok=True)
        Path(str(temporary_path) + ".aria2").unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        raise


async def download_thumbnail(
    url: str,
    filename: str,
) -> Path:

    if not url.startswith("https://"):
        raise ValueError(
            "Invalid thumbnail URL."
        )

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    output_path = DOWNLOAD_DIR / filename

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": EPORNER_REFERER,
    }

    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30),
            follow_redirects=True,
            trust_env=False,
        ) as client:

            response = await client.get(url)

            response.raise_for_status()

            if not response.content:
                raise RuntimeError(
                    "Thumbnail is empty."
                )

            output_path.write_bytes(
                response.content
            )

        return output_path

    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    
