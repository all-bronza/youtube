import os
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Update, Message
from aiogram.enums import ParseMode
import yt_dlp
from dotenv import load_dotenv

# -------- Загрузка .env --------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-secret-path")
BASE_DIR = Path("/tmp")
MAX_SEND_BYTES = 48 * 1024 * 1024  # ~48 МБ

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# -------- Настройки ffmpeg --------
FFMPEG_PATH: Optional[str] = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

# -------- Бот и роутер --------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

YOUTUBE_RX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+")

HELP_TEXT = (
    "Привет! Пришли ссылку на YouTube — я верну аудио 🎧\n\n"
    "Команды:\n"
    " /audio <url> — аудио (m4a)\n"
    " /mp3 <url> — аудио в mp3\n"
    " /video <url> — видео (до 720p)\n"
    "Можно просто прислать ссылку — я пришлю аудио."
)

@router.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(HELP_TEXT)

def _base_opts(outtmpl: str):
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts

def _extract_info(url: str, download: bool, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download)

async def _send_file_or_link(m: Message, fpath: Path, info: dict, kind: str):
    est_size = info.get("filesize_approx") or info.get("filesize")
    if fpath.exists() and fpath.stat().st_size <= MAX_SEND_BYTES:
        if kind == "audio":
            await m.answer_audio(audio=fpath.open("rb"), caption=f"🎧 {fpath.name}")
        elif kind == "video":
            await m.answer_video(video=fpath.open("rb"), caption=f"🎬 {fpath.name}")
        else:
            await m.answer_document(document=fpath.open("rb"), caption=f"📎 {fpath.name}")
        return
    stream_url = info.get("url")
    title = info.get("title", "media")
    if stream_url:
        await m.answer(
            f"Файл большой. Вот ссылка для скачивания или воспроизведения:\n"
            f"{title}\n{stream_url}"
        )
    else:
        await m.answer("Не удалось отправить файл или получить прямую ссылку.")

async def download_audio_m4a(url: str, dest_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    opts.update({"format": "bestaudio[ext=m4a]/bestaudio/best"})
    info = _extract_info(url, download=True, opts=opts)
    return Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)), info

async def download_audio_mp3(url: str, dest_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    opts.update({
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    info = _extract_info(url, download=True, opts=opts)
    base = Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)).with_suffix(".mp3")
    final = next((p for p in dest_dir.glob("*.mp3") if info.get("title", "") in p.name), base)
    return final, info

async def download_video(url: str, dest_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    opts.update({
        "format":
