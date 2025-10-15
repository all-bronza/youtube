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

# -------- Конфиг --------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-secret-path")  # любой твой путь
BASE_DIR = Path("/tmp")  # на Render можно писать во временную папку
MAX_SEND_BYTES = 48 * 1024 * 1024  # ~48 МБ - безопасный порог для отправки

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Опционально: статический ffmpeg из imageio-ffmpeg, чтобы делать MP3
FFMPEG_PATH: Optional[str] = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

# -------- Бот/роутер --------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

YOUTUBE_RX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+")

HELP_TEXT = (
    "Привет! Пришли ссылку на YouTube — я верну аудио 🎧\n\n"
    "Команды:\n"
    " /audio <url> — только звук (m4a; /mp3 <url> — в mp3)\n"
    " /video <url> — видео (пытаюсь ≤720p и разумный размер)\n"
    "Просто пришлёшь ссылку — пришлю аудио (m4a)."
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
    """
    kind: 'audio' | 'video' | 'document'
    Если файл большой, отправляем прямую ссылку на медиапоток (без скачивания).
    """
    # Оценка размера (если есть)
    est_size = None
    if "filesize_approx" in info:
        est_size = info.get("filesize_approx")
    elif "filesize" in info:
        est_size = info.get("filesize")

    # Если есть физический файл и он не огромный — отправляем файл
    if fpath and fpath.exists() and fpath.stat().st_size <= MAX_SEND_BYTES:
        if kind == "audio":
            await m.answer_audio(audio=fpath.open("rb"), caption=f"🎧 {fpath.name}")
        elif kind == "video":
            await m.answer_video(video=fpath.open("rb"), caption=f"🎬 {fpath.name}")
        else:
            await m.answer_document(document=fpath.open("rb"), caption=f"📎 {fpath.name}")
        return

    # Иначе пробуем дать прямой URL (Telegram сам подтянет)
    stream_url = info.get("url")
    title = info.get("title", "media")
    if stream_url:
        await m.answer(
            "Файл большой для прямой отправки ботом. Вот ссылка для воспроизведения/скачивания:\n"
            f"{title}\n{stream_url}\n\n"
            "⚠️ Ссылка может быть временной. Если не открывается — пришли /audio или /video снова."
        )
    else:
        await m.answer("Не удалось отправить файл или получить прямую ссылку.")

async def download_audio_m4a(url: str, dest_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    # m4a без перекодирования
    opts.update({
        "format": "bestaudio[ext=m4a]/bestaudio/best",
    })
    info = _extract_info(url, download=True, opts=opts)
    return Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)), info

async def download_audio_mp3(url: str, dest_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    # MP3 через ffmpeg (нужен FFMPEG_PATH — imageio-ffmpeg)
    post = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }]
    opts.update({
        "format": "bestaudio/best",
        "postprocessors": post,
    })
    info = _extract_info(url, download=True, opts=opts)
    # после постобработки расширение .mp3
    base = Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)).with_suffix(".mp3")
    # В редких случаях имя может отличаться — ищем .mp3 в папке
    final = next((p for p in dest_dir.glob("*.mp3") if info.get("title", "") in p.name), base)
    return final, info

async def download_video(url: str, dest_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(dest_dir / "%(title).200B.%(ext)s")
    opts = _base_opts(outtmpl)
    # Пытаемся брать ≤720p и ограничивать размер
    opts.update({
        "format": "mp4[height<=720][filesize<48M]/mp4[height<=480]/best[filesize<48M]/best",
    })
    info = _extract_info(url, download=True, opts=opts)
    return Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)), info

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
        return await m.answer("MP3 сейчас недоступно (нет ffmpeg). Доступно m4a. "
                              "Установи imageio-ffmpeg в requirements, задеплой — и заработает.")
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

# -------- FastAPI (webhook) --------
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
