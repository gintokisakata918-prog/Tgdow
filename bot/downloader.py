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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Parallel connections. 8 default — 16 pe Eporner ka CDN atak jaata hai.
ARIA2_CONNECTIONS = max(1, _env_int("ARIA2_CONNECTIONS", 8))

# Itne second tak 0 byte rahe to aria2 kill, single connection pe fallback.
ARIA2_FIRST_BYTE_TIMEOUT = _env_int("ARIA2_FIRST_BYTE_TIMEOUT", 40)

# Download chalu hone ke baad itne second speed 0 rahe to bhi kill.
ARIA2_STALL_TIMEOUT = _env_int("ARIA2_STALL_TIMEOUT", 45)

DISABLE_ARIA2 = os.getenv("DISABLE_ARIA2", "").lower() in {
    "1", "true", "yes", "on",
}


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
    """Total size nikaalo: HEAD -> Range probe -> plain headers."""

    timeout = httpx.Timeout(20)

    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as client:

            try:
                head = await client.head(url)
                length = int(head.headers.get("content-length", 0))

                if length > 0:
                    return length

            except Exception:
                pass

            try:
                probe = await client.get(
                    url,
                    headers={**headers, "Range": "bytes=0-0"},
                )

                match = re.search(
                    r"/(\d+)\s*$",
                    probe.headers.get("content-range", ""),
                )

                if match:
                    return int(match.group(1))

                length = int(probe.headers.get("content-length", 0))

                if probe.status_code == 200 and length > 0:
                    return length

            except Exception:
                pass

    except Exception:
        pass

    return 0


async def watch_progress(
    path: Path,
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
            return
        except asyncio.TimeoutError:
            continue


async def stall_guard(
    process: asyncio.subprocess.Process,
    path: Path,
    stop: asyncio.Event,
) -> str | None:
    """0 byte pe atka ya beech me ruk gaya to aria2 kill karo."""

    started = time.monotonic()
    last_size = -1
    last_change = started

    while not stop.is_set():

        await asyncio.sleep(2)

        if stop.is_set() or process.returncode is not None:
            return None

        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        now = time.monotonic()

        if size != last_size:
            last_size = size
            last_change = now

        if size == 0 and now - started > ARIA2_FIRST_BYTE_TIMEOUT:
            process.kill()
            return (
                f"aria2c ne {ARIA2_FIRST_BYTE_TIMEOUT}s me koi byte "
                f"nahi diya ({ARIA2_CONNECTIONS} connections)"
            )

        if size > 0 and now - last_change > ARIA2_STALL_TIMEOUT:
            process.kill()
            return (
                f"aria2c {ARIA2_STALL_TIMEOUT}s se ruka hua tha "
                f"({human(size)} pe)"
            )

    return None


async def download_aria2(
    url: str,
    temporary_path: Path,
    headers: dict[str, str],
    progress_callback: ProgressCallback | None,
) -> Path:

    total = await probe_size(url, headers)

    command = [
        "aria2c",
        "--continue=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--file-allocation=none",
        f"--max-connection-per-server={ARIA2_CONNECTIONS}",
        f"--split={ARIA2_CONNECTIONS}",
        "--min-split-size=1M",
        "--max-tries=5",
        "--retry-wait=3",
        "--connect-timeout=20",
        "--timeout=30",
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
        f"{httpx.URL(url).host} (total={human(total) if total else 'unknown'})"
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
            total,
            progress_callback,
            stop,
        )
    )

    guard = asyncio.create_task(
        stall_guard(process, temporary_path, stop)
    )

    try:
        _, stderr = await process.communicate()
    finally:
        stop.set()
        watcher.cancel()

    reason = await guard

    if process.returncode != 0:

        if reason:
            raise RuntimeError(reason)

        detail = " | ".join(
            stderr.decode(errors="ignore").strip().splitlines()[-3:]
        )[:400]

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

                        match = re.search(
                            r"/(\d+)\s*$",
                            response.headers.get("content-range", ""),
                        )

                        total = (
                            int(match.group(1))
                            if match
                            else resume_from + int(
                                response.headers.get("content-length", 0)
                            )
                        )

                        mode = "ab"

                    else:

                        total = int(
                            response.headers.get("content-length", 0)
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
                raise RuntimeError("Downloaded file is empty.")

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

    use_aria2 = aria2_available() and not DISABLE_ARIA2

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
                    f"({type(error).__name__}: {error}) -> "
                    f"single connection"
                )

                temporary_path.unlink(missing_ok=True)
                Path(str(temporary_path) + ".aria2").unlink(missing_ok=True)

                await download_httpx(
                    url,
                    temporary_path,
                    headers,
                    progress_callback,
                )

        else:

            print(
                "aria2c off (DISABLE_ARIA2 set ya install nahi hai), "
                "single connection"
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

        Path(str(temporary_path) + ".aria2").unlink(missing_ok=True)

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
        raise ValueError("Invalid thumbnail URL.")

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
                raise RuntimeError("Thumbnail is empty.")

            output_path.write_bytes(response.content)

        return output_path

    except Exception:
        output_path.unlink(missing_ok=True)
        raise
                               
