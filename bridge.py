# -*- coding: utf-8 -*-

import os
import json
import subprocess
import logging
import asyncio
import re
import time
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

import requests

from telegram import Update
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# Load Config & Links
# =========================
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

TELEGRAM_BOT_TOKEN = CONFIG["TELEGRAM_BOT_TOKEN"]
RUBIKA_TOKEN = CONFIG["RUBIKA_TOKEN"]
RUBIKA_DEFAULT_CHAT_ID = CONFIG["RUBIKA_DEFAULT_CHAT_ID"]
MY_CHANNEL_LINK = CONFIG["MY_CHANNEL_LINK"]

RUBIKA_ROUTE = {}
ALBUM_FLUSH_DELAY_PHOTO_SECONDS = CONFIG.get("ALBUM_FLUSH_DELAY_PHOTO_SECONDS", 5)
ALBUM_FLUSH_DELAY_VIDEO_SECONDS = CONFIG.get("ALBUM_FLUSH_DELAY_VIDEO_SECONDS", 10)

MAX_PARALLEL_UPLOADS = 1
TG_GETFILE_RETRIES = 4
TG_RETRY_BASE_DELAY = 1.0

# ---- Optional Local Bot API server ----
LOCAL_API = CONFIG.get("LOCAL_API", {})
LOCAL_API_ENABLED = bool(LOCAL_API.get("enabled", False))
LOCAL_API_HOST = LOCAL_API.get("host", "127.0.0.1")
LOCAL_API_PORT = int(LOCAL_API.get("port", 8081))
LOCAL_API_START_SERVER = bool(LOCAL_API.get("start_server", False))
LOCAL_API_EXECUTABLE = LOCAL_API.get("executable_path", "")
LOCAL_API_ID = LOCAL_API.get("api_id", "")
LOCAL_API_HASH = LOCAL_API.get("api_hash", "")
LOCAL_MAX_MB = int(LOCAL_API.get("max_download_mb", 2000))

# Download limit: 20MB on the public API, max_download_mb on the local server.
TELEGRAM_MAX_DOWNLOAD_BYTES = (LOCAL_MAX_MB if LOCAL_API_ENABLED else 20) * 1024 * 1024
TG_MAX_DOWNLOAD_MB = TELEGRAM_MAX_DOWNLOAD_BYTES // (1024 * 1024)

# PTB appends the token to base_url/base_file_url itself (verified).
LOCAL_TG_BASE = f"http://{LOCAL_API_HOST}:{LOCAL_API_PORT}/bot"
LOCAL_TG_FILE_BASE = f"http://{LOCAL_API_HOST}:{LOCAL_API_PORT}/file/bot"

# ---- Shared download cache (SAME dir for BOTH bridges -> download once) ----
SHARED_CACHE = CONFIG.get("SHARED_CACHE", {})
CACHE_DIR = SHARED_CACHE.get("dir", "bridge_cache")
CACHE_KEEP_SECONDS = float(SHARED_CACHE.get("keep_hours", 1)) * 3600.0
CACHE_MAX_BYTES = int(SHARED_CACHE.get("max_gb", 20)) * 1024 ** 3

# ---- Upload behaviour ----
UPLOAD_CFG = CONFIG.get("UPLOAD", {})
MAX_SEND_ATTEMPTS = int(UPLOAD_CFG.get("max_attempts", 3))
UPLOAD_BASE_TIMEOUT = float(UPLOAD_CFG.get("base_timeout_sec", 120))
UPLOAD_TIMEOUT_PER_MB = float(UPLOAD_CFG.get("timeout_per_mb", 5))
PENDING_FILE = UPLOAD_CFG.get("pending_queue", "pending_uploads.jsonl")

LOG_TAG_RUBIKA = "#BRIDGE_LOG_RUBIKA"
LOG_TAG_BALE = "#BRIDGE_LOG_BALE"
LOG_PREFIX = f"🔔 [Rubika Bridge] مربوط به برنامه «روبیکا»\n{LOG_TAG_RUBIKA}\n"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bridge.log", encoding="utf-8")],
)
log = logging.getLogger("bridge")

# =========================
# Watchdog (5-Minute Anti-Freeze)
# =========================
LAST_TICK = time.time()

async def async_ticker():
    global LAST_TICK
    while True:
        LAST_TICK = time.time()
        await asyncio.sleep(30)

def watchdog_thread():
    while True:
        time.sleep(30)
        # Changed from 120 seconds to 300 seconds (5 minutes)
        if time.time() - LAST_TICK > 300:
            log.error("Watchdog: Bot is stuck! Forcing crash for .bat to restart...")
            os._exit(1)

# =========================
# Rubika helpers
# =========================
class RubikaError(Exception): pass
class RubikaUploadTemporaryError(RubikaError): pass

def _rubika_url(method: str) -> str:
    return f"https://botapi.rubika.ir/v3/{RUBIKA_TOKEN}/{method}"

def _rubika_call(method: str, payload: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    url = _rubika_url(method)
    payload = payload or {}
    try:
        r = requests.post(url, json=payload, timeout=timeout, headers={"Connection": "close"})
    except requests.RequestException as e:
        raise RubikaError(f"Rubika request failed: {e}") from e

    try:
        data = r.json()
    except Exception:
        raise RubikaError(f"Rubika non-JSON response: {r.text[:400]}")

    if r.status_code >= 400:
        raise RubikaError(f"Rubika HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:600]}")

    if isinstance(data, dict):
        status = str(data.get("status", "")).upper()
        if data.get("ok") is False or status == "ERROR":
            raise RubikaError(f"Rubika Error: {data}")
    return data

def rubika_send_text(chat_id: str, text: str) -> None:
    _rubika_call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=20)

def rubika_request_upload_url(file_type: str) -> str:
    data = _rubika_call("requestSendFile", {"type": file_type}, timeout=20)
    root = (data.get("data") or data.get("result") or data)
    return root.get("upload_url") or data.get("upload_url")

def rubika_upload_file(upload_url: str, file_path: str, timeout: int = 240) -> str:
    try:
        with open(file_path, "rb") as f:
            r = requests.post(upload_url, files={"file": f}, timeout=timeout, headers={"Connection": "close"})
    except requests.RequestException as e:
        raise RubikaUploadTemporaryError(f"Upload failed: {e}") from e

    data = r.json()
    root = (data.get("data") or data.get("result") or data)
    return root.get("file_id") or data.get("file_id") or root.get("fileId") or root.get("id")

def rubika_send_file(chat_id: str, file_id: str, text: Optional[str] = None) -> None:
    payload = {"chat_id": chat_id, "file_id": file_id}
    if text:
        payload["text"] = text
    _rubika_call("sendFile", payload, timeout=20)

# =========================
# Utils
# =========================
def _route_rubika_chat_id(source_kind: str) -> str:
    return RUBIKA_ROUTE.get(source_kind, RUBIKA_DEFAULT_CHAT_ID)

def clean_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return MY_CHANNEL_LINK
    cleaned = re.sub(r'https?://\S+|#\S+|@\S+', '', text).strip()
    return f"{cleaned}\n\n{MY_CHANNEL_LINK}" if cleaned else MY_CHANNEL_LINK

def _safe_caption(msg_obj) -> Optional[str]:
    cap = getattr(msg_obj, "caption", None)
    return clean_text(cap.strip() if cap else None)

def _doc_suffix(file_name: Optional[str], default: str = ".bin") -> str:
    if file_name and "." in file_name:
        suf = "." + file_name.split(".")[-1]
        if 1 < len(suf) <= 10: return suf
    return default

def _human_size(n: Optional[int]) -> str:
    if not n: return "?"
    units = ["B", "KB", "MB", "GB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]: return f"{v:.1f}{u}" if u != "B" else f"{int(v)}B"
        v /= 1024
    return f"{int(n)}B"

def _is_too_big(file_size: Optional[int]) -> bool:
    return bool(file_size and file_size > TELEGRAM_MAX_DOWNLOAD_BYTES)

def _too_big_text(kind: str, file_name: Optional[str], file_size: Optional[int], caption: Optional[str]) -> str:
    return f"⚠️ فایل بزرگتر از حد مجاز {TG_MAX_DOWNLOAD_MB} مگابایت است و ارسال نشد.\nنوع: {kind}\nحجم: {_human_size(file_size)}"

def _is_bridge_log_message(msg_obj) -> bool:
    t = (getattr(msg_obj, "text", None) or getattr(msg_obj, "caption", None) or "")
    return (LOG_TAG_RUBIKA in t) or (LOG_TAG_BALE in t)

def _upload_timeout(file_size: Optional[int]) -> int:
    """Scale the Rubika upload timeout with file size so big videos are not killed mid-upload."""
    if not file_size:
        return 240
    return int(min(3600.0, UPLOAD_BASE_TIMEOUT + (file_size / (1024 * 1024)) * UPLOAD_TIMEOUT_PER_MB))

async def telegram_notify_error(context, tg_chat_id: int, reply_to_message_id: int, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=tg_chat_id, text=LOG_PREFIX + text, reply_to_message_id=reply_to_message_id)
    except Exception:
        pass

# =========================
# Shared download cache
# =========================
def _ensure_cache_dir():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError as e:
        log.warning("Cannot create cache dir %s: %s", CACHE_DIR, e)

def _cache_path(file_unique_id: str, suffix: str) -> str:
    return os.path.join(CACHE_DIR, f"{file_unique_id}{suffix}")

def _sweep_cache():
    """Delete cache files older than keep_hours; if still over max_gb, delete oldest first."""
    _ensure_cache_dir()
    now = time.time()
    cutoff = now - CACHE_KEEP_SECONDS
    try:
        total = 0
        files = []
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            total += st.st_size
            files.append((st.st_mtime, st.st_size, p))

        # age-based deletion
        for mt, sz, p in files:
            if mt < cutoff:
                try:
                    os.remove(p)
                except OSError:
                    pass

        # size-cap deletion (oldest first) - never touch fresh/active files
        files = [f for f in files if f[0] >= cutoff]
        files.sort()
        for mt, sz, p in files:
            if total <= CACHE_MAX_BYTES:
                break
            if now - mt < 600:  # might be mid-upload
                continue
            if ".part." in os.path.basename(p):  # in-progress/abandoned temp
                continue
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
    except OSError as e:
        log.warning("cache sweep error: %s", e)

async def cache_sweep_loop():
    while True:
        await asyncio.sleep(600)
        _sweep_cache()

async def _download_to_cache(context, file_id: str, file_unique_id: str, suffix: str) -> str:
    """Download a Telegram file into the SHARED cache. Returns the local path.

    If the file is already in the cache (downloaded by this bot or the Bale
    bridge), it is reused - Telegram is only contacted once per file.
    """
    _ensure_cache_dir()
    final = _cache_path(file_unique_id, suffix)
    if os.path.exists(final) and os.path.getsize(final) > 0:
        return final  # cache hit

    tmp = f"{final}.part.{os.getpid()}"  # unique temp: both bots share CACHE_DIR
    try:
        for attempt in range(1, TG_GETFILE_RETRIES + 1):
            try:
                tg_file = await context.bot.get_file(file_id)
                await tg_file.download_to_drive(custom_path=tmp)
                os.replace(tmp, final)  # atomic
                return final
            except (TimedOut, NetworkError) as e:
                delay = TG_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.warning("Telegram download attempt %d failed: %s", attempt, e)
                await asyncio.sleep(delay)
        raise TimedOut("Telegram download failed")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

# =========================
# Pending upload queue (persists across restarts)
# =========================
def _load_pending() -> Dict[str, dict]:
    if not os.path.exists(PENDING_FILE):
        return {}
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return {json.loads(l)["key"]: json.loads(l) for l in f if l.strip()}
    except Exception as e:
        log.warning("pending load failed: %s", e)
        return {}

def _save_pending(records: Dict[str, dict]):
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            for rec in records.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("pending save failed: %s", e)

def _add_pending(rec: dict):
    records = _load_pending()
    records[rec["key"]] = rec
    _save_pending(records)

def _remove_pending(key: str):
    records = _load_pending()
    if key in records:
        records.pop(key)
        _save_pending(records)

# =========================
# Queue job model
# =========================
@dataclass
class SendJob:
    source_kind: str
    tg_chat_id: int
    tg_message_id: int
    rubika_chat_id: str
    kind: str
    rubika_type: str
    file_id: Optional[str]
    suffix: Optional[str]
    file_name: Optional[str]
    file_size: Optional[int]
    text: Optional[str]
    caption: Optional[str]
    file_unique_id: Optional[str] = None

    def pending_key(self) -> Optional[str]:
        if not self.file_id:
            return None  # text-only: not persisted
        return f"{self.tg_chat_id}:{self.tg_message_id}:{self.file_id}"

SEND_QUEUE: asyncio.Queue[SendJob] = asyncio.Queue()

# =========================
# Worker logic
# =========================
def _calc_retry_delay(attempts: int) -> float:
    return min(20.0, 1.0 * (2 ** max(0, attempts - 1)))

async def _process_send_job(context: ContextTypes.DEFAULT_TYPE, job: SendJob) -> None:
    final_text = clean_text(job.text) if job.text else job.caption

    if final_text and not job.file_id:
        await asyncio.to_thread(rubika_send_text, job.rubika_chat_id, final_text)
        await asyncio.sleep(2)
        return

    if _is_too_big(job.file_size):
        await telegram_notify_error(context, job.tg_chat_id, job.tg_message_id, _too_big_text(job.kind, job.file_name, job.file_size, job.caption))
        return

    # NOTE: the file lives in the SHARED cache - it is NOT deleted after sending.
    # The 1-hour TTL sweep removes it so the other bridge can reuse it meanwhile.
    local_path = await _download_to_cache(context, job.file_id, job.file_unique_id or job.file_id, job.suffix or ".bin")
    upload_url = await asyncio.to_thread(rubika_request_upload_url, job.rubika_type)
    timeout = _upload_timeout(job.file_size)
    rubika_file_id = await asyncio.to_thread(rubika_upload_file, upload_url, local_path, timeout)

    await asyncio.to_thread(rubika_send_file, job.rubika_chat_id, rubika_file_id, text=final_text)
    await asyncio.sleep(3)

async def send_worker(app, worker_id: int) -> None:
    context = app.bot_data.get("_ctx_for_workers")

    while True:
        job = await SEND_QUEUE.get()
        success = False
        last_error = None

        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                await _process_send_job(context, job)
                success = True
                break
            except (RubikaUploadTemporaryError, RubikaError, TimedOut, NetworkError, requests.RequestException, BadRequest) as e:
                last_error = e
                delay = _calc_retry_delay(attempt)
                if "429" in str(e) or "TOO_MANY_REQUESTS" in str(e): delay = 60.0

                await telegram_notify_error(
                    context, job.tg_chat_id, job.tg_message_id,
                    f"⚠️ ارسال به روبیکا ناموفق بود. تلاش {attempt}/{MAX_SEND_ATTEMPTS}\n"
                    f"نوع: {job.kind}\n"
                    f"تلاش بعدی تا {int(delay)} ثانیه دیگر...\n"
                    f"جزئیات: {type(e).__name__}: {str(e)[:200]}"
                )

                log.warning(f"Attempt {attempt}/{MAX_SEND_ATTEMPTS} failed. Retrying...")
                await asyncio.sleep(delay)
            except Exception as e:
                last_error = e
                await telegram_notify_error(
                    context, job.tg_chat_id, job.tg_message_id,
                    f"⚠️ خطا ناشناخته. تلاش {attempt}/{MAX_SEND_ATTEMPTS}\n"
                    f"نوع: {job.kind}\n"
                    f"تلاش بعدی تا 5 ثانیه دیگر...\n"
                    f"جزئیات: {type(e).__name__}: {str(e)[:200]}"
                )

                log.exception(f"Unknown error on attempt {attempt}: {e}")
                await asyncio.sleep(5)

        if success:
            key = job.pending_key()
            if key:
                _remove_pending(key)
        else:
            key = job.pending_key()
            rec = None
            if key:
                rec = {"key": key, "job": asdict(job)}
                _add_pending(rec)
            err_text = f"جزئیات: {type(last_error).__name__}: {str(last_error)[:300]}" if last_error else ""
            await telegram_notify_error(
                context, job.tg_chat_id, job.tg_message_id,
                f"⚠️ ارسال انجام نشد و بعد از {MAX_SEND_ATTEMPTS} تلاش رها شد."
                f"{' در صف تلاش مجدد ذخیره شد.' if rec else ''}\n"
                f"نوع: {job.kind}\n{err_text}"
            )
            log.error("DROP job after max attempts")

        SEND_QUEUE.task_done()

# =========================
# Album buffering
# =========================
@dataclass
class AlbumItem:
    message_id: int
    file_id: str
    suffix: str
    rubika_type: str
    kind: str
    file_name: Optional[str]
    file_size: Optional[int]
    caption: Optional[str] = None
    file_unique_id: Optional[str] = None

MEDIA_GROUPS: Dict[str, Dict[str, Any]] = {}

def _album_key(source_kind: str, chat_id: int, media_group_id: Any) -> str:
    return f"{source_kind}:{chat_id}:{media_group_id}"

async def flush_album_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    key = context.job.data["key"]
    group = MEDIA_GROUPS.pop(key, None)
    if not group: return

    items: List[AlbumItem] = group["items"]
    items.sort(key=lambda x: x.message_id)

    album_caption = None
    for it in items:
        if it.caption:
            album_caption = it.caption
            break

    for it in items: it.caption = None
    if album_caption: items[-1].caption = album_caption

    for it in items:
        await SEND_QUEUE.put(SendJob(
            source_kind=group["source_kind"], tg_chat_id=group["tg_chat_id"],
            tg_message_id=group["reply_message_id"], rubika_chat_id=group["rubika_chat_id"],
            kind=it.kind, rubika_type=it.rubika_type, file_id=it.file_id, suffix=it.suffix,
            file_name=it.file_name, file_size=it.file_size, text=None, caption=it.caption,
            file_unique_id=it.file_unique_id,
        ))

# =========================
# Core handler
# =========================
async def handle_telegram_message_to_rubika(context, rubika_chat_id: str, msg_obj, source_kind: str) -> None:
    try:
        tg_chat_id, tg_message_id = msg_obj.chat_id, msg_obj.message_id

        if getattr(msg_obj, "text", None) and not (getattr(msg_obj, "photo", None) or getattr(msg_obj, "video", None) or getattr(msg_obj, "document", None) or getattr(msg_obj, "animation", None) or getattr(msg_obj, "audio", None) or getattr(msg_obj, "voice", None) or getattr(msg_obj, "video_note", None) or getattr(msg_obj, "sticker", None)):
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "text", "File", None, None, None, None, msg_obj.text, None))
            return

        caption = _safe_caption(msg_obj)
        mgid = getattr(msg_obj, "media_group_id", None)

        def add_to_album(item: AlbumItem) -> bool:
            nonlocal mgid
            if not mgid: return False
            key = _album_key(source_kind, tg_chat_id, mgid)
            g = MEDIA_GROUPS.setdefault(key, {"items": [], "rubika_chat_id": rubika_chat_id, "has_video": False, "source_kind": source_kind, "tg_chat_id": tg_chat_id, "reply_message_id": tg_message_id})
            g["items"].append(item)
            if item.rubika_type == "Video": g["has_video"] = True

            job_name = f"flush_{key}"
            for j in context.job_queue.jobs():
                if j.name == job_name:
                    try: j.schedule_removal()
                    except: pass

            delay = ALBUM_FLUSH_DELAY_VIDEO_SECONDS if g.get("has_video") else ALBUM_FLUSH_DELAY_PHOTO_SECONDS
            context.job_queue.run_once(flush_album_job, when=delay, data={"key": key}, name=job_name)
            return True

        if getattr(msg_obj, "photo", None):
            p = msg_obj.photo[-1]
            fuid = getattr(p, "file_unique_id", p.file_id)
            item = AlbumItem(tg_message_id, p.file_id, ".jpg", "Image", "photo", None, getattr(p, "file_size", None), caption, fuid)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "photo", "Image", p.file_id, ".jpg", None, getattr(p, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "video", None):
            v = msg_obj.video
            fuid = getattr(v, "file_unique_id", v.file_id)
            item = AlbumItem(tg_message_id, v.file_id, ".mp4", "Video", "video", getattr(v, "file_name", None), getattr(v, "file_size", None), caption, fuid)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "video", "Video", v.file_id, ".mp4", getattr(v, "file_name", None), getattr(v, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "animation", None):
            # FIX: animations/GIFs were silently dropped - now sent as video
            a = msg_obj.animation
            fuid = getattr(a, "file_unique_id", a.file_id)
            item = AlbumItem(tg_message_id, a.file_id, ".mp4", "Video", "animation", getattr(a, "file_name", None), getattr(a, "file_size", None), caption, fuid)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "animation", "Video", a.file_id, ".mp4", getattr(a, "file_name", None), getattr(a, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "document", None):
            d = msg_obj.document
            suf = _doc_suffix(getattr(d, "file_name", None), ".bin")
            fuid = getattr(d, "file_unique_id", d.file_id)
            item = AlbumItem(tg_message_id, d.file_id, suf, "File", "document", getattr(d, "file_name", None), getattr(d, "file_size", None), caption, fuid)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "document", "File", d.file_id, suf, getattr(d, "file_name", None), getattr(d, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "audio", None):
            a = msg_obj.audio
            fuid = getattr(a, "file_unique_id", a.file_id)
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "audio", "File", a.file_id, _doc_suffix(getattr(a, "file_name", None), ".mp3"), getattr(a, "file_name", None), getattr(a, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "voice", None):
            v = msg_obj.voice
            fuid = getattr(v, "file_unique_id", v.file_id)
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "voice", "File", v.file_id, ".ogg", None, getattr(v, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "video_note", None):
            v = msg_obj.video_note
            fuid = getattr(v, "file_unique_id", v.file_id)
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "video_note", "File", v.file_id, ".mp4", None, getattr(v, "file_size", None), None, caption, fuid))
            return

        if getattr(msg_obj, "sticker", None):
            s = msg_obj.sticker
            suf = ".webm" if getattr(s, "is_video", False) else ".tgs" if getattr(s, "is_animated", False) else ".webp"
            fuid = getattr(s, "file_unique_id", s.file_id)
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "sticker", "File", s.file_id, suf, None, getattr(s, "file_size", None), None, caption, fuid))
            return

    except Exception as e:
        log.exception("enqueue failed: %s", e)

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if not msg or _is_bridge_log_message(msg): return

    # Ignore messages that are older than 10 minutes (600 seconds)
    now = datetime.now(timezone.utc)
    if (now - msg.date).total_seconds() > 600:
        log.info(f"Ignored old channel message (older than 10 mins). Message ID: {msg.message_id}")
        return

    await handle_telegram_message_to_rubika(context, _route_rubika_chat_id("channel"), msg, "channel")

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or _is_bridge_log_message(msg): return

    # Ignore messages that are older than 10 minutes (600 seconds)
    now = datetime.now(timezone.utc)
    if (now - msg.date).total_seconds() > 600:
        log.info(f"Ignored old group message (older than 10 mins). Message ID: {msg.message_id}")
        return

    await handle_telegram_message_to_rubika(context, _route_rubika_chat_id("group"), msg, "group")

# =========================
# Local Bot API server
# =========================
LOCAL_API_PROC: Optional[subprocess.Popen] = None

def _local_api_healthy() -> bool:
    try:
        r = requests.get(f"{LOCAL_TG_BASE}{TELEGRAM_BOT_TOKEN}/getMe", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False

def start_local_api_server_if_needed():
    """If LOCAL_API is enabled, make sure the local server is reachable; optionally start it."""
    global LOCAL_API_PROC
    if not LOCAL_API_ENABLED:
        return
    if _local_api_healthy():
        log.info("Local Bot API server already reachable on port %s", LOCAL_API_PORT)
        return

    if not LOCAL_API_START_SERVER:
        log.error(
            "LOCAL_API is enabled but the server is not reachable and start_server is false. "
            "Start telegram-bot-api manually or via systemd first."
        )
        raise SystemExit(1)

    if not (LOCAL_API_ID and LOCAL_API_HASH and LOCAL_API_EXECUTABLE):
        log.error("LOCAL_API start_server=true requires api_id, api_hash and executable_path in config.json")
        raise SystemExit(1)

    cmd = [
        LOCAL_API_EXECUTABLE,
        f"--api-id={LOCAL_API_ID}",
        f"--api-hash={LOCAL_API_HASH}",
        f"--http-port={LOCAL_API_PORT}",
        "--local",
    ]
    log.info("Starting telegram-bot-api: %s", " ".join(cmd))
    LOCAL_API_PROC = subprocess.Popen(cmd)

    for _ in range(30):
        if _local_api_healthy():
            log.info("Local Bot API server is up.")
            return
        time.sleep(1)

    log.error("Local Bot API server did not become healthy within 30s.")
    raise SystemExit(1)

def stop_local_api_server():
    global LOCAL_API_PROC
    if LOCAL_API_PROC and LOCAL_API_PROC.poll() is None:
        LOCAL_API_PROC.terminate()
        try:
            LOCAL_API_PROC.wait(timeout=10)
        except Exception:
            LOCAL_API_PROC.kill()

# =========================
# Main
# =========================
async def start_background_tasks(app):
    for i in range(MAX_PARALLEL_UPLOADS):
        asyncio.create_task(send_worker(app, i + 1))
    asyncio.create_task(async_ticker())
    asyncio.create_task(cache_sweep_loop())
    _sweep_cache()

    # Recover pending uploads from a previous run (survives restarts)
    for rec in _load_pending().values():
        try:
            SEND_QUEUE.put_nowait(SendJob(**rec["job"]))
        except Exception as e:
            log.warning("Could not restore pending job: %s", e)

def main() -> None:
    start_local_api_server_if_needed()
    threading.Thread(target=watchdog_thread, daemon=True).start()

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=90.0, write_timeout=90.0, pool_timeout=30.0)
    builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).post_init(start_background_tasks)
    if LOCAL_API_ENABLED:
        builder = builder.base_url(LOCAL_TG_BASE).base_file_url(LOCAL_TG_FILE_BASE)
    app = builder.build()

    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_post))
    app.add_handler(MessageHandler((filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & ~filters.COMMAND, on_group_message))

    class _FakeCtx:
        def __init__(self, bot): self.bot = bot
    app.bot_data["_ctx_for_workers"] = _FakeCtx(app.bot)

    log.info("Bridge is running... (Polling)")

    # Allow pending updates through so we can manually filter them by the 10-minute rule
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
