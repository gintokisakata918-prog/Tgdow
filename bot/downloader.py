import os
import time
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


def human(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{value:.2f} TB"


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
        "Connection": "keep-alive",
    }

    downloaded = 0
    last_update = 0
    start_time = time.monotonic()

    try:
        timeout = httpx.Timeout(
            connect=30,
            read=120,
            write=30,
            pool=30,
        )

        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:

            async with client.stream("GET", url) as response:

                response.raise_for_status()

                total = int(
                    response.headers.get("content-length", 0)
                )

                with open(temporary_path, "wb") as file:

                    async for chunk in response.aiter_bytes(
                        chunk_size=CHUNK_SIZE
                    ):

                        if not chunk:
                            continue

                        file.write(chunk)
                        downloaded += len(chunk)

                        current_time = time.monotonic()

                        if (
                            progress_callback
                            and current_time - last_update >= 2
                        ):

                            await progress_callback(
                                downloaded,
                                total,
                            )

                            last_update = current_time

        if downloaded == 0:
            raise RuntimeError("Downloaded file is empty.")

        os.replace(
            temporary_path,
            output_path,
        )

        if progress_callback:
            await progress_callback(
                downloaded,
                total,
            )

        return output_path

    except Exception:
        temporary_path.unlink(missing_ok=True)
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
            timeout=30,
            follow_redirects=True,
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
