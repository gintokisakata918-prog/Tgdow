# Eporner Leech Bot

Telegram bot that accepts public Eporner video URLs, downloads the selected video stream, optionally compresses it, and uploads it to a configured Telegram channel using a Telethon user session.

## Features

- Accepts Eporner video URLs.
- Selects the highest available resolution up to `MAX_RES`.
- Downloads videos asynchronously.
- Displays download progress.
- Optionally compresses large videos using FFmpeg.
- Removes metadata when `CLEAN=true`.
- Generates captions using video titles and keywords.
- Uses SQLite to avoid downloading the same video twice.
- Uploads videos through a Telethon user session.

## Project Structure

```text
Eporner-Leech-Bot/
├── bot/
│   ├── __init__.py
│   ├── bot.py
│   ├── config.py
│   ├── database.py
│   ├── downloader.py
│   ├── eporner.py
│   └── leecher.py
├── data/
├── downloads/
├── .env.example
├── .gitignore
├── Dockerfile
├── render.yaml
└── requirements.txt
