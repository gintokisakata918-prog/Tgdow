import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# =========================
# Telegram Configuration
# =========================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()

# Telethon StringSession
STRING_SESSION = os.getenv("STRING_SESSION", "").strip()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Destination channel ID
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()

# Allowed Telegram user IDs
ALLOWED_USERS = {
    int(user.strip())
    for user in os.getenv("ALLOWED_USERS", "").split(",")
    if user.strip().isdigit()
}


# =========================
# Eporner Configuration
# =========================

EPORNER_API = "https://www.eporner.com/api/v2/video/id/"

EPORNER_REFERER = "https://www.eporner.com/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MAX_RES = int(os.getenv("MAX_RES", "1080"))


# =========================
# Download Configuration
# =========================

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "downloads")
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

CHUNK_SIZE = 1024 * 1024

COMPRESS_ABOVE_MB = int(
    os.getenv("COMPRESS_ABOVE_MB", "500")
)

CLEAN = os.getenv("CLEAN", "true").lower() in {
    "true",
    "1",
    "yes",
    "on",
}


# =========================
# Database Configuration
# =========================

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "data/database.sqlite3",
)

Path(DATABASE_PATH).parent.mkdir(
    parents=True,
    exist_ok=True,
)


# =========================
# Server Configuration
# =========================

PORT = int(os.getenv("PORT", "8080"))

HOST = "0.0.0.0"


# =========================
# Caption Configuration
# =========================

CAPTION_TEMPLATE = (
    "{title}\n\n"
    "{hashtags}"
)

CLEAN_CAPTION_TEMPLATE = (
    "{title}\n\n"
    "{hashtags}"
)


# =========================
# FFmpeg Configuration
# =========================

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

FFMPEG_VIDEO_CODEC = "libx264"
FFMPEG_AUDIO_CODEC = "aac"

FFMPEG_CRF = 26
FFMPEG_AUDIO_BITRATE = "96k"

FFMPEG_MAX_WIDTH = 1280

FFMPEG_EXTRA_ARGS = [
    "-map_metadata",
    "-1",
]


# =========================
# Logging Configuration
# =========================

LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO",
).upper()


# =========================
# Validation
# =========================

def validate_config():
    errors = []

    if not API_ID:
        errors.append("API_ID is missing")

    if not API_HASH:
        errors.append("API_HASH is missing")

    if not STRING_SESSION:
        errors.append("STRING_SESSION is missing")

    if not BOT_TOKEN:
        errors.append("BOT_TOKEN is missing")

    if not TARGET_CHANNEL:
        errors.append("TARGET_CHANNEL is missing")

    if MAX_RES <= 0:
        errors.append("MAX_RES must be greater than zero")

    if errors:
        raise RuntimeError(
            "Configuration errors:\n- "
            + "\n- ".join(errors)
)
