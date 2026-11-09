import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def env_int(name, default, minimum=0):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value

API_ID = env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USERS = {int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip().isdigit()}
EPORNER_REFERER = "https://www.eporner.com/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MAX_RES = env_int("MAX_RES", 1080, 144)
COMPRESS_ABOVE_MB = env_int("COMPRESS_ABOVE_MB", 500)
PORT = env_int("PORT", 8080, 1)
if PORT > 65535:
    raise RuntimeError("PORT must be between 1 and 65535")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).expanduser()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CHUNK_SIZE = 1024 * 1024
CLEAN = os.getenv("CLEAN", "true").lower() in {"true", "1", "yes", "on"}
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/database.sqlite3")
Path(DATABASE_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)
HOST = "0.0.0.0"
CAPTION_TEMPLATE = "{title}\n\n{hashtags}"
CLEAN_CAPTION_TEMPLATE = "{title}\n\n{hashtags}"
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFMPEG_VIDEO_CODEC = "libx264"
FFMPEG_AUDIO_CODEC = "aac"
FFMPEG_CRF = 26
FFMPEG_AUDIO_BITRATE = "96k"
FFMPEG_MAX_WIDTH = 1280
FFMPEG_EXTRA_ARGS = ["-map_metadata", "-1"]
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

def validate_config():
    errors = []
    if not API_ID: errors.append("API_ID is missing")
    if not API_HASH: errors.append("API_HASH is missing")
    if not BOT_TOKEN: errors.append("BOT_TOKEN is missing")
    if errors:
        raise RuntimeError("Configuration errors:\n- " + "\n- ".join(errors))
