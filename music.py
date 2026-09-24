import os
import re
import io
import html
import math
import shutil
import asyncio
import logging
import sqlite3
import datetime
import tempfile
import time
import subprocess
import contextlib

import yt_dlp
from ytmusicapi import YTMusic
from dotenv import load_dotenv
from telegram import Update, User, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ============================== CONFIG ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в лог попадают URL с токеном
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))   # настройки из файла .env рядом с music.py

TOKEN = os.environ.get("TELEGRAM_TOKEN")
# локальный прокси VPN-клиента, для Happ: socks5://127.0.0.1:10808
PROXY_URL = os.environ.get("PROXY_URL") or None
# что идёт через прокси: yt, sc и tg (сам Telegram — если отправка файлов медленная)
PROXY_SOURCES = set(os.environ.get("PROXY_SOURCES", "yt,sc").split(","))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")          # не задан -> polling
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
PORT = int(os.environ.get("PORT", 8443))
DB_FILE = os.environ.get("DB_FILE", os.path.join(BASE_DIR, "data", "users.db"))
FFMPEG_LOCATION = os.environ.get("FFMPEG_LOCATION")  # папка с ffmpeg.exe, если его нет в PATH
COOKIES_FILE = os.environ.get("YTDLP_COOKIES")
# GIF для приветствия: file_id / URL в START_GIF или файл assets/start.gif (необязательно)
START_GIF = os.environ.get("START_GIF") or os.path.join(BASE_DIR, "assets", "start.gif")

PAGE_SIZE = 8
SEARCH_RESULTS_COUNT = 20
SEARCH_CACHE_TTL = 30 * 60     # сколько секунд помнить результаты поиска
DOWNLOAD_SLOTS = 3             # сколько треков качается одновременно на весь бот
DOWNLOAD_TIMEOUT = 150
MAX_FILESIZE = 49 * 1024 * 1024       # лимит Bot API на отправку файлов ~50 МБ
KEEP_SEARCHES = 20                    # сколько последних выдач помнить на пользователя

# Один акцентный цвет — только у активного источника. "primary" синий, "danger" красный,
# "success" зелёный, None — без цвета. Остальные кнопки нейтральные: так чище.
ACCENT = "primary"

# SOURCES=yt,sc — какие источники включены и в каком порядке идут кнопки
ALL_SOURCES = {"yt": "YouTube Music", "sc": "SoundCloud"}
SOURCES = {k: ALL_SOURCES[k] for k in os.environ.get("SOURCES", "yt,sc").split(",")
           if k in ALL_SOURCES}
if not SOURCES:
    SOURCES = {"yt": "YouTube"}
DEFAULT_SOURCE = next(iter(SOURCES))

WELCOME = "<b>bakermusic</b>\nПришли название трека или имя артиста."


# ============================== DATABASE ==============================
def init_database():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT, last_name TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                url TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                created TEXT NOT NULL
            )""")
    logger.info("database at '%s' initialized", DB_FILE)


# Кэш Telegram file_id: трек, который уже кто-то скачивал, отправляется мгновенно,
# без повторной загрузки и конвертации.
def get_cached_file(url: str):
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute("SELECT file_id FROM tracks WHERE url = ?", (url,)).fetchone()
    return row[0] if row else None


def save_cached_file(url: str, file_id: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as con:
        con.execute("INSERT OR REPLACE INTO tracks (url, file_id, created) VALUES (?, ?, ?)",
                    (url, file_id, now))


def drop_cached_file(url: str):
    with sqlite3.connect(DB_FILE) as con:
        con.execute("DELETE FROM tracks WHERE url = ?", (url,))


def add_or_update_user(user: User):
    if user is None:
        return
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with sqlite3.connect(DB_FILE) as con:
            con.execute(
                """INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name,
                     last_name=excluded.last_name, last_seen=excluded.last_seen""",
                (user.id, user.username, user.first_name, user.last_name, now, now),
            )
    except Exception:
        logger.exception("failed to add/update user %s", user.id)


# ============================== HELPERS ==============================
def btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style) if style \
        else InlineKeyboardButton(text, callback_data=data)


def ff(tool: str) -> str:
    """Путь к ffmpeg/ffprobe с учётом FFMPEG_LOCATION."""
    return os.path.join(FFMPEG_LOCATION, tool) if FFMPEG_LOCATION else tool


def clean_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()[:150] or "track"


def format_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}:{sec:02d}"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# мусор в названиях с YouTube: (Official Video), [Lyrics], (Премьера клипа, 2024) …
_JUNK = re.compile(
    r"\s*[\(\[【][^\)\]】]*?(official|video|audio|lyric|visuali[sz]er|music\s*video|\bmv\b|\bhd\b|\b4k\b|"
    r"клип|премьера|текст|пр[еe]мьера|official)[^\)\]】]*[\)\]】]",
    re.IGNORECASE,
)


def tidy_title(title: str) -> str:
    title = _JUNK.sub("", title)
    title = re.sub(r"\s*\|.*$", "", title)          # "Song | Something" -> "Song"
    return re.sub(r"\s{2,}", " ", title).strip(" -–—") or title


# ============================== YOUTUBE / SOUNDCLOUD (yt-dlp) ==============================
def ydl_opts(src: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "concurrent_fragment_downloads": 8,      # HLS (SoundCloud) качается в 8 потоков
            "http_chunk_size": 10 * 1024 * 1024,     # обходит ограничение скорости YouTube
            "socket_timeout": 15, "retries": 3}
    if PROXY_URL and src in PROXY_SOURCES:
        opts["proxy"] = PROXY_URL
    else:
        opts["proxy"] = ""                       # напрямую, мимо системного прокси
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if src == "yt" and COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def yt_search(query: str) -> list:
    """YouTube Music (только песни), а если он недоступен или пуст — обычный YouTube
    с фильтром по длительности, чтобы не лезли стримы и влоги."""
    try:
        results = ytmusic_search(query)
        if results:
            return results
        logger.info("YouTube Music: 0 songs for %r, falling back to YouTube", query)
    except Exception:
        logger.exception("YouTube Music search failed for %r, falling back to YouTube", query)
    return youtube_search(query)


def youtube_search(query: str) -> list:
    with yt_dlp.YoutubeDL(ydl_opts("yt") | {"extract_flat": True}) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS_COUNT * 2}:{query}", download=False)
    results = []
    for e in info.get("entries") or []:
        url = e and (e.get("url") or e.get("webpage_url"))
        dur = e and e.get("duration")
        if not url or not dur or not 60 <= dur <= 12 * 60:   # песни, а не стримы/влоги
            continue
        title = e.get("title") or "Без названия"
        artist = re.sub(r"\s*-\s*Topic$|VEVO$", "", e.get("uploader") or e.get("channel") or "").strip()
        for sep in (" - ", " – ", " — "):
            if sep in title:
                artist, title = (p.strip() for p in title.split(sep, 1))
                break
        results.append({"src": "yt", "url": url, "artist": artist or "Неизвестен",
                        "title": tidy_title(title), "duration": dur})
    return results[:SEARCH_RESULTS_COUNT]


def ytmusic_search(query: str) -> list:
    """Поиск только по песням YouTube Music — без видео, стримов и влогов.
    Сразу отдаёт нормальные исполнителя, название и длительность."""
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL and "yt" in PROXY_SOURCES else None
    items = YTMusic(proxies=proxies).search(query, filter="songs", limit=SEARCH_RESULTS_COUNT)
    logger.info("YouTube Music raw: %d items, types: %s", len(items),
                sorted({str(it.get("resultType")) for it in items}))
    results = []
    for it in items:
        vid = it.get("videoId")
        if not vid:
            continue
        artists = ", ".join(a["name"] for a in it.get("artists") or [] if a.get("name"))
        results.append({"src": "yt", "url": f"https://music.youtube.com/watch?v={vid}",
                        "artist": artists or "Неизвестен", "title": it.get("title") or "Без названия",
                        "duration": it.get("duration_seconds")})
    return results[:SEARCH_RESULTS_COUNT]


def sc_search(query: str) -> list:
    with yt_dlp.YoutubeDL(ydl_opts("sc") | {"extract_flat": True}) as ydl:
        info = ydl.extract_info(f"scsearch{SEARCH_RESULTS_COUNT}:{query}", download=False)
    results = []
    for e in info.get("entries") or []:
        url = e and (e.get("url") or e.get("webpage_url"))
        if not url:
            continue
        title = e.get("title") or "Без названия"
        artist = e.get("uploader") or ""
        for sep in (" - ", " – ", " — "):          # "Artist - Song"
            if sep in title:
                artist, title = (p.strip() for p in title.split(sep, 1))
                break
        results.append({"src": "sc", "url": url, "artist": artist or "Неизвестен",
                        "title": tidy_title(title), "duration": e.get("duration")})
    return results


def probe(path: str) -> tuple:
    """(аудиокодек, длительность в секундах) одним вызовом ffprobe."""
    try:
        out = subprocess.run(
            [ff("ffprobe"), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name:format=duration", "-of", "default=nw=1", path],
            capture_output=True, text=True, timeout=30).stdout
        info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return info.get("codec_name"), float(info.get("duration") or 0) or None
    except Exception:
        return None, None


class PreviewOnly(Exception):
    pass


def make_cover(src_image: str, out: str, size: int) -> bool:
    """Квадратная обложка: центральный кроп (кадр YouTube 16:9 -> 1:1) и ресайз."""
    res = subprocess.run(
        [ff("ffmpeg"), "-y", "-loglevel", "error", "-i", src_image,
         "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}", "-q:v", "3", out],
        capture_output=True, timeout=30)
    return res.returncode == 0 and os.path.exists(out)


def ydl_download(track: dict, tmp: str) -> tuple:
    """Возвращает (путь к аудио, путь к обложке 320x320 или None).

    Скорость: берём сразу m4a (AAC) или mp3, если источник их отдаёт, и только
    перепаковываем с тегами и обложкой — без перекодирования (в ~10 раз быстрее,
    файл меньше). Перекодируем в mp3 только то, что Telegram не проигрывает (opus и т.п.).
    """
    opts = ydl_opts(track["src"]) | {
        # сначала цельный файл по http (один запрос), потом HLS-кусочки
        "format": ("bestaudio[ext=m4a][protocol^=http]/bestaudio[acodec=mp3][protocol^=http]/"
                   "bestaudio[ext=m4a]/bestaudio[acodec=mp3]/bestaudio/best"),
        "outtmpl": os.path.join(tmp, "src.%(ext)s"),
        "max_filesize": MAX_FILESIZE,
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(track["url"], download=True)
    logger.info("format %s via %s", info.get("format_id"), info.get("protocol"))

    raw = next((os.path.join(tmp, f) for f in os.listdir(tmp)
                if f.startswith("src.") and not f.endswith((".jpg", ".webp", ".png", ".part"))), None)
    if not raw:
        raise FileNotFoundError("yt-dlp did not produce an audio file")

    codec, real = probe(raw)
    # SoundCloud Go+ отдаёт только 30-секундное превью — такое не отправляем
    expected = track.get("duration")
    if expected and real and expected > 60 and real < min(45, expected * 0.5):
        raise PreviewOnly(f"only {real:.0f}s preview of {expected}s track")

    thumb_src = os.path.join(tmp, "src.jpg")
    cover_big, cover_small = os.path.join(tmp, "cover.jpg"), os.path.join(tmp, "thumb.jpg")
    has_cover = os.path.exists(thumb_src) and make_cover(thumb_src, cover_big, 600)
    if has_cover:
        make_cover(thumb_src, cover_small, 320)     # превью для Telegram (<=320px, <=200 КБ)

    if codec == "aac":
        final, audio_args = os.path.join(tmp, "final.m4a"), ["-c:a", "copy"]
    elif codec == "mp3":
        final, audio_args = os.path.join(tmp, "final.mp3"), ["-c:a", "copy", "-id3v2_version", "3"]
    else:
        final, audio_args = os.path.join(tmp, "final.mp3"), ["-c:a", "libmp3lame", "-b:a", "192k",
                                                             "-id3v2_version", "3"]

    # один проход ffmpeg: аудио + чистые теги + обложка внутри файла
    cmd = [ff("ffmpeg"), "-y", "-loglevel", "error", "-i", raw]
    if has_cover:
        cmd += ["-i", cover_big, "-map", "0:a:0", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic"]
    else:
        cmd += ["-map", "0:a:0"]
    cmd += audio_args + ["-map_metadata", "-1",
                         "-metadata", f"artist={track['artist']}", "-metadata", f"title={track['title']}",
                         final]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not os.path.exists(final):
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[-300:]}")
    logger.info("audio: %s -> %s", codec, "copy" if codec in ("aac", "mp3") else "mp3 transcode")
    return final, (cover_small if has_cover and os.path.exists(cover_small) else None)


# ============================== COMMON ==============================
SEARCHERS = {"yt": yt_search, "sc": sc_search}


def download_track(track: dict) -> tuple:
    with tempfile.TemporaryDirectory() as tmp:
        path, thumb = ydl_download(track, tmp)
        if os.path.getsize(path) > MAX_FILESIZE:
            raise ValueError("file is larger than telegram limit")
        with open(path, "rb") as f:
            audio = io.BytesIO(f.read())
        cover = None
        if thumb:
            with open(thumb, "rb") as f:
                cover = io.BytesIO(f.read())
    ext = os.path.splitext(path)[1]
    return audio, cover, f"{clean_filename(track['artist'] + ' - ' + track['title'])}{ext}"


# ============================== UI ==============================
def source_row(sid: int, active: str) -> list:
    return [btn(label, f"s:{sid}:{key}", ACCENT if key == active else None)
            for key, label in SOURCES.items()]


def render_search(sid: int, s: dict) -> tuple:
    """Заголовок + до 8 треков + навигация + переключатель источника."""
    name = SOURCES[s["src"]]
    q = html.escape(shorten(s["q"], 60))
    results, page = s["results"], s["page"]

    if s.get("error"):
        text = f"<b>{q}</b>\n{name} сейчас не отвечает. Попробуй ещё раз или другой источник."
    elif not results:
        text = f"<b>{q}</b>\nНа {name} ничего не нашлось."
    else:
        n = len(results)
        text = f"<b>{q}</b>\n{name} · {n} {plural(n, 'трек', 'трека', 'треков')}"

    rows = []
    start = page * PAGE_SIZE
    for i, t in enumerate(results[start:start + PAGE_SIZE], start=start):
        dur = format_duration(t["duration"])
        label = shorten(f"{t['artist']} — {t['title']}", 52)
        rows.append([btn(f"{label}  {dur}" if dur else label, f"t:{sid}:{i}")])

    pages = math.ceil(len(results) / PAGE_SIZE)
    if pages > 1:
        # всегда три кнопки, чтобы ряд не «прыгал» между страницами
        rows.append([
            btn("‹", f"p:{sid}:{page - 1}") if page > 0 else btn("·", "noop"),
            btn(f"{page + 1} / {pages}", "noop"),
            btn("›", f"p:{sid}:{page + 1}") if page < pages - 1 else btn("·", "noop"),
        ])

    if len(SOURCES) > 1:
        rows.append(source_row(sid, s["src"]))
    return text, InlineKeyboardMarkup(rows)


def save_search(context: ContextTypes.DEFAULT_TYPE, s: dict) -> int:
    searches = context.user_data.setdefault("searches", {})
    sid = context.user_data.get("next_sid", 1)
    context.user_data["next_sid"] = sid + 1
    searches[sid] = s
    for old in sorted(searches)[:-KEEP_SEARCHES]:
        searches.pop(old)
    return sid


_search_cache: dict = {}      # (src, запрос) -> (время, результаты)
_search_inflight: dict = {}   # (src, запрос) -> Future: не ищем одно и то же дважды параллельно


async def cached_search(src: str, q: str) -> list:
    key = (src, q.casefold())
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < SEARCH_CACHE_TTL:
        return hit[1]
    if key in _search_inflight:                  # тот же поиск уже идёт (например, предзагрузка)
        return await asyncio.shield(_search_inflight[key])
    fut = asyncio.get_running_loop().create_future()
    _search_inflight[key] = fut
    try:
        results = await _do_search(src, q, key)
        fut.set_result(results)
        return results
    except Exception as e:
        fut.set_exception(e)
        fut.exception()                          # чтобы не было "exception never retrieved"
        raise
    finally:
        _search_inflight.pop(key, None)


async def _do_search(src: str, q: str, key: tuple) -> list:
    t0 = time.monotonic()
    results = await asyncio.to_thread(SEARCHERS[src], q)
    logger.info("search %s %r: %d results in %.1fs", src, q, len(results), time.monotonic() - t0)
    if results:                                  # пустую выдачу не запоминаем
        _search_cache[key] = (time.monotonic(), results)
    if len(_search_cache) > 500:                 # не даём кэшу расти бесконечно
        for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:100]:
            _search_cache.pop(k, None)
    return results


async def prefetch_other_sources(q: str, current: str):
    """Пока пользователь смотрит выдачу, фоном ищем в другом источнике —
    переключение YouTube/SoundCloud потом происходит мгновенно."""
    for src in SOURCES:
        if src != current:
            with contextlib.suppress(Exception):
                await cached_search(src, q)


async def run_search(s: dict):
    try:
        s["results"] = await cached_search(s["src"], s["q"])
        s["error"] = False
    except Exception:
        logger.exception("search failed: %r on %s", s["q"], s["src"])
        s["results"], s["error"] = [], True
    s["page"] = 0


_download_slots = asyncio.Semaphore(DOWNLOAD_SLOTS)


async def keep_action(bot, chat_id: int, action: str):
    """Статус «отправляет аудио…» живёт 5 секунд — обновляем, пока идёт загрузка."""
    with contextlib.suppress(asyncio.CancelledError):
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, action)
            await asyncio.sleep(4)


# ============================== HANDLERS ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(add_or_update_user, update.effective_user)
    context.user_data.pop("searches", None)
    chat_id = update.effective_chat.id

    gif = context.bot_data.get("gif_file_id") or START_GIF
    looks_like_path = gif and not gif.startswith(("http://", "https://")) and (
        os.sep in gif or "/" in gif or gif.lower().endswith((".gif", ".mp4")))
    if looks_like_path and not os.path.isfile(gif):
        gif = None                      # файла нет — просто текст
    if gif:
        try:
            if os.path.isfile(gif):
                with open(gif, "rb") as f:
                    msg = await context.bot.send_animation(chat_id, f, caption=WELCOME,
                                                           parse_mode=ParseMode.HTML)
            else:
                msg = await context.bot.send_animation(chat_id, gif, caption=WELCOME,
                                                       parse_mode=ParseMode.HTML)
            if msg.animation:  # дальше шлём по file_id, без повторной загрузки
                context.bot_data["gif_file_id"] = msg.animation.file_id
            return
        except Exception:
            logger.warning("could not send start GIF %r, falling back to text", gif, exc_info=True)
    await context.bot.send_message(chat_id, WELCOME, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(add_or_update_user, update.effective_user)
    q = update.message.text.strip()[:200]
    src = context.user_data.get("source", DEFAULT_SOURCE)
    if src not in SOURCES:
        src = DEFAULT_SOURCE
    logger.info("user %s search %r on %s", update.effective_user.id, q, src)

    msg = await update.message.reply_text(f"Ищу на {SOURCES[src]}…")
    s = {"q": q, "src": src, "results": [], "page": 0}
    await run_search(s)
    sid = save_search(context, s)
    text, kb = render_search(sid, s)
    await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    if len(SOURCES) > 1:
        context.application.create_task(prefetch_other_sources(q, src))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    await asyncio.to_thread(add_or_update_user, query.from_user)

    if data[:2] not in ("p:", "s:", "t:"):
        await query.answer()
        return
    kind, sid, arg = data.split(":", 2)
    s = context.user_data.get("searches", {}).get(int(sid))
    if not s:
        await query.answer("Эта выдача устарела — пришли запрос заново.", show_alert=True)
        return
    if kind == "p":
        await query.answer()
        s["page"] = int(arg)
        text, kb = render_search(int(sid), s)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif kind == "s":
        await switch_source(query, context, int(sid), s, arg)
    else:
        await download_and_send(query, context, s, int(arg))


async def switch_source(query, context, sid: int, s: dict, src: str):
    if src not in SOURCES or (src == s["src"] and not s.get("error")):
        await query.answer()
        return
    await query.answer()
    context.user_data["source"] = src          # запоминаем выбор пользователя
    s["src"] = src
    hit = _search_cache.get((src, s["q"].casefold()))
    if not (hit and time.monotonic() - hit[0] < SEARCH_CACHE_TTL):
        await query.edit_message_text(f"Ищу на {SOURCES[src]}…")
    await run_search(s)
    text, kb = render_search(sid, s)
    await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def download_and_send(query: CallbackQuery, context, s: dict, idx: int):
    if idx >= len(s["results"]):
        await query.answer("Трек не найден — повтори поиск.", show_alert=True)
        return
    if context.user_data.get("busy"):
        await query.answer("Подожди, предыдущий трек ещё загружается.")
        return

    track = s["results"][idx]
    name = f"{track['artist']} — {track['title']}"
    await query.answer("Загружаю…")
    context.user_data["busy"] = True
    chat_id = query.message.chat_id
    logger.info("user %s download %r (%s)", query.from_user.id, name, track["src"])

    # 1) трек уже отправляли раньше — шлём по file_id, это мгновенно
    file_id = await asyncio.to_thread(get_cached_file, track["url"])
    if file_id:
        try:
            await context.bot.send_audio(chat_id, audio=file_id)
            logger.info("sent from cache: %r", name)
            context.user_data["busy"] = False
            return
        except BadRequest:
            await asyncio.to_thread(drop_cached_file, track["url"])

    # 2) иначе качаем
    action = asyncio.create_task(keep_action(context.bot, chat_id, ChatAction.UPLOAD_VOICE))
    try:
        t0 = time.monotonic()
        async with _download_slots:
            t_wait = time.monotonic()
            audio, cover, file_name = await asyncio.wait_for(
                asyncio.to_thread(download_track, track), timeout=DOWNLOAD_TIMEOUT)
        t_dl = time.monotonic()
        msg = await context.bot.send_audio(
            chat_id, audio=audio, filename=file_name, thumbnail=cover,
            performer=track["artist"], title=track["title"], duration=track.get("duration"),
            read_timeout=120, write_timeout=120,
        )
        t_up = time.monotonic()
        logger.info("timing %r: queue %.1fs, download+convert %.1fs, upload %.1fs (%.1f MB)",
                    name, t_wait - t0, t_dl - t_wait, t_up - t_dl, audio.getbuffer().nbytes / 1e6)
        if msg.audio:
            await asyncio.to_thread(save_cached_file, track["url"], msg.audio.file_id)
    except Exception as e:
        if isinstance(e, asyncio.TimeoutError):
            logger.warning("download timeout: %r", name)
            reason = "Загрузка заняла слишком долго."
        elif isinstance(e, PreviewOnly):
            logger.info("preview only: %r", name)
            reason = "На SoundCloud доступно только 30-секундное превью."
        else:
            logger.exception("download failed: %r", name)
            reason = "Не получилось загрузить."
        await context.bot.send_message(
            chat_id, f"<b>{html.escape(name)}</b>\n{reason} Попробуй другой трек или источник.",
            parse_mode=ParseMode.HTML)
    finally:
        action.cancel()
        context.user_data["busy"] = False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("unhandled error", exc_info=context.error)


# ============================== MAIN ==============================
async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "Начать")])


def build_app() -> Application:
    builder = ApplicationBuilder().token(TOKEN).concurrent_updates(True).post_init(post_init)
    if PROXY_URL and "tg" in PROXY_SOURCES:      # Telegram через прокси
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(error_handler)
    return app


def main():
    if not TOKEN:
        raise SystemExit("FATAL: TELEGRAM_TOKEN is not set (добавьте его в файл .env)")
    logger.info("sources: %s; proxy: %s for %s", ", ".join(SOURCES.values()), PROXY_URL or "off",
                ",".join(sorted(PROXY_SOURCES)))
    ffmpeg_dir = FFMPEG_LOCATION or os.path.dirname(shutil.which("ffmpeg") or "")
    if not ffmpeg_dir or not shutil.which("ffmpeg", path=ffmpeg_dir):
        logger.error("ffmpeg NOT FOUND — downloads will fail. Install it (winget install Gyan.FFmpeg) "
                     "and restart VS Code, or set FFMPEG_LOCATION to the folder with ffmpeg.exe")
    else:
        logger.info("ffmpeg found in %s", ffmpeg_dir)
    init_database()
    app = build_app()

    if WEBHOOK_URL:
        logger.info("starting in webhook mode on port %s", PORT)
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path="telegram",
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/telegram",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
        )
    else:
        logger.info("starting in polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
