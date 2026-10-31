import os, re, asyncio, shutil, uuid
from pyrogram import Client, filters
from pyrogram.types import Message

API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED   = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x}
WORKDIR   = "/tmp/dl"
MAX_MB    = 1900          # 2000 se neeche rakho

os.makedirs(WORKDIR, exist_ok=True)
app = Client("epbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def human(n):
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


async def dl(url, outdir):
    cmd = [
        "yt-dlp", "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--write-info-json",
        "--no-playlist",
        "-o", f"{outdir}/%(title).80s.%(ext)s",
        url,
    ]
    p = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    tail = []
    async for line in p.stdout:
        tail.append(line.decode(errors="ignore"))
        tail = tail[-40:]
    await p.wait()
    if p.returncode != 0:
        raise RuntimeError("".join(tail))
    return tail


@app.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def handle(c: Client, m: Message):
    if ALLOWED and m.from_user.id not in ALLOWED:
        return await m.reply("Access nahi hai.")

    url = m.text.strip()
    if not re.match(r"^https?://", url):
        return await m.reply("Valid link bhejo.")

    job = uuid.uuid4().hex[:8]
    outdir = os.path.join(WORKDIR, job)
    os.makedirs(outdir, exist_ok=True)
    status = await m.reply("Download shuru...")

    try:
        await dl(url, outdir)
        files = [f for f in os.listdir(outdir) if f.endswith(".mp4")]
        if not files:
            raise RuntimeError("Koi mp4 nahi mila (shayad HLS/DRM issue hai).")
        path = os.path.join(outdir, files[0])
        size_mb = os.path.getsize(path) / 1048576

        if size_mb > MAX_MB:
            await status.edit(f"{size_mb:.0f} MB — 2GB se zyada, split kar raha hoon...")
            subprocess_split(path, outdir)

        thumb = next((os.path.join(outdir, f) for f in os.listdir(outdir)
                      if f.endswith(".jpg")), None)

        await status.edit("Upload ho raha hai...")
        for f in sorted(os.listdir(outdir)):
            if not f.endswith(".mp4"):
                continue
            await c.send_video(
                chat_id=m.chat.id,
                video=os.path.join(outdir, f),
                thumb=thumb,
                caption=f"`{f}`",
                supports_streaming=True,
                duration=0,
                width=0, height=0,
                progress=progress_cb(status),
            )
        await status.edit("Ho gaya.")
    except Exception as e:
        await status.edit(f"Fail: `{str(e)[-600:]}`")
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def subprocess_split(path, outdir):
    import subprocess
    subprocess.run([
        "ffmpeg", "-y", "-i", path,
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", "3600",
        "-reset_timestamps", "1",
        f"{outdir}/part_%03d.mp4",
    ], check=True)
    os.remove(path)


def progress_cb(status):
    async def cb(current, total):
        pct = current * 100 / total
        if int(pct) % 10 == 0:
            try:
                await status.edit(f"Upload: {pct:.0f}% ({human(current)}/{human(total)})")
            except Exception:
                pass
    return cb


app.run()
