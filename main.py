import os
import re
from pathlib import Path
from typing import Optional, Tuple

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
BASE_DIR = Path("/tmp")  # временная директория на Render
MAX_SEND_BYTES = 48 * 1024 * 1024  # ~48 МБ безопасный порог отправки

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# -------- Настройки ffmpeg (для MP3) --------
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
    " /mp3 <url> — аудио в mp3 (если доступен ffmpeg)\n"
    " /video <url> — видео (пытаюсь ≤720p и разумный размер)\n"
    "Можно просто прислать ссылку — пришлю аудио (m4a)."
)

@router.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(HELP_TEXT)

def _base_opts(outtmpl: str) -> dict:
    """Базовые опции для yt_dlp."""
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts

def _extract_info(url: str, download: bool, opts: dict) -> dict:
    """Вызов yt_dlp с нужными опциями."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download)

async def _send_file_or_link(m: Message, fpath: Path, info: dict, kind: str) -> None:
    """
    Пытаемся отправить файл. Если он выходит за лимит — даём прямую ссылку (stream URL).
    kind: 'audio' | 'video' | 'document'
    """
    # Если файл есть и он не больше лимита — отправляем файл
    if fpath and fpath.exists() and fpath.stat().st_size <= MAX_SEND_BYTES:
        if kind == "audio":
            await m.answer_audio(audio=fpath.open("rb"), caption=f"🎧 {fpath.name}")
        elif kind == "video":
            await m.answer_video(video=fpath.open("rb"), caption=f"🎬 {fpath.name}")
        else:
            await m.answer_document(document=fpath.open("rb"), caption=f"📎 {fpath.name}")
        return

    # Иначе — отдаём прямую ссылку (если есть)
    stream_url = info.get("url")
    title = info.get("title", "media")
    if stream_url:
        await m.answer(
            "Файл слишком большой для прямой отправки ботом.\n"
            "Вот временная ссылка для воспроизведения/скачивания:\n"
            f"{title}\n{stream_url}\n\n"
            "Если ссылка перестанет работать — пришли команду ещё раз."
        )
    else:
        await m.answer("Не удалось отправить файл или получить прямую ссылку.")

async def download_audio_m4a(url: str, dest_dir: Path) -> Tuple[Path, dict]:
    """Скачиваем лучшую аудиодорожку без перекодирования (обычно .m4a)."""
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    opts.update({
        "format": "bestaudio[ext=m4a]/bestaudio/best",
    })
    info = _extract_info(url, download=True, opts=opts)
    fpath = Path(yt_dlp.YoutubeDL(opts).prepare_filename(info))
    return fpath, info

async def download_audio_mp3(url: str, dest_dir: Path) -> Tuple[Path, dict]:
    """Скачиваем и перекодируем в mp3 (нужен ffmpeg)."""
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
    # yt_dlp сформирует путь исходя из prepare_filename, заменим суффикс на .mp3
    base = Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)).with_suffix(".mp3")
    # На случай, если имя чуть отличается, попробуем найти любой .mp3 рядом с тайтлом
    final = next((p for p in dest_dir.glob("*.mp3") if info.get("title", "") in p.name), base)
    return final, info

async def download_video(url: str, dest_dir: Path) -> Tuple[Path, dict]:
    """
    Скачиваем видео с попыткой уложиться в разумный размер.
    Сначала ищем mp4 ≤720p и <48МБ, далее ≤480p, далее best c ограничением.
    """
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    opts.update({
        "format": "mp4[height<=720][filesize<48M]/mp4[height<=480]/best[filesize<48M]/best",
    })
    info = _extract_info(url, download=True, opts=opts)
    fpath = Path(yt_dlp.YoutubeDL(opts).prepare_filename(info))
    return fpath, info

@router.message(F.text.regexp(YOUTUBE_RX))
async def on_plain_link(m: Message):
    url = m.text.strip()
    await m.answer("Скачиваю аудио… ⏳")
    try:
        fpath, info = await download_audio_m4a(url, BASE_DIR)
        await _send_file_or_link(m, fpath, info, "audio")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@router.message(Command("audio"))
async def cmd_audio(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Пришли так: /audio <ссылка YouTube>")
    url = parts[1].strip()
    await m.answer("Скачиваю аудио… ⏳")
    try:
        fpath, info = await download_audio_m4a(url, BASE_DIR)
        await _send_file_or_link(m, fpath, info, "audio")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@router.message(Command("mp3"))
async def cmd_mp3(m: Message):
    if not FFMPEG_PATH:
        return await m.answer("MP3 временно недоступно (нет ffmpeg). Попробуй /audio (m4a).")
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Пришли так: /mp3 <ссылка YouTube>")
    url = parts[1].strip()
    await m.answer("Готовлю MP3… ⏳")
    try:
        fpath, info = await download_audio_mp3(url, BASE_DIR)
        await _send_file_or_link(m, fpath, info, "audio")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@router.message(Command("video"))
async def cmd_video(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Пришли так: /video <ссылка YouTube>")
    url = parts[1].strip()
    await m.answer("Скачиваю видео… ⏳")
    try:
        fpath, info = await download_video(url, BASE_DIR)
        await _send_file_or_link(m, fpath, info, "video")
    except Exception as e:
        await m.answer(
            "Возможно, файл слишком большой для отправки ботом. "
            "Попробуй /audio или /mp3.\n"
            f"Техническая ошибка: {e}"
        )

# -------- FastAPI (webhook endpoints) --------
app = FastAPI()

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid update")
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def health():
    return {"status": "ok"}
