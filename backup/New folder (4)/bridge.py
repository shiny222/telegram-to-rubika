# -*- coding: utf-8 -*-

import os
import json
import tempfile
import logging
import asyncio
import re
import time
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
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
TELEGRAM_MAX_DOWNLOAD_BYTES = 18 * 1024 * 1024  # 18MB
TG_GETFILE_RETRIES = 4
TG_RETRY_BASE_DELAY = 1.0
MAX_SEND_ATTEMPTS = 6

LOG_TAG_RUBIKA = "#BRIDGE_LOG_RUBIKA"
LOG_TAG_BALE = "#BRIDGE_LOG_BALE"
LOG_PREFIX = f"🔔 [Rubika Bridge] مربوط به برنامه «روبیکا»\n{LOG_TAG_RUBIKA}\n"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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

def rubika_upload_file(upload_url: str, file_path: str) -> str:
    try:
        with open(file_path, "rb") as f:
            r = requests.post(upload_url, files={"file": f}, timeout=240, headers={"Connection": "close"})
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
    return f"⚠️ فایل برای دانلود بزرگ است.\nنوع: {kind}\nحجم: {_human_size(file_size)}"

def _is_bridge_log_message(msg_obj) -> bool:
    t = (getattr(msg_obj, "text", None) or getattr(msg_obj, "caption", None) or "")
    return (LOG_TAG_RUBIKA in t) or (LOG_TAG_BALE in t)

async def telegram_notify_error(context, tg_chat_id: int, reply_to_message_id: int, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=tg_chat_id, text=LOG_PREFIX + text, reply_to_message_id=reply_to_message_id)
    except Exception:
        pass

async def download_telegram_file_safe(context, file_id: str, suffix: str) -> str:
    for attempt in range(1, TG_GETFILE_RETRIES + 1):
        try:
            tg_file = await context.bot.get_file(file_id)
            fd, path = tempfile.mkstemp(prefix="tg_", suffix=suffix)
            os.close(fd)
            await tg_file.download_to_drive(custom_path=path)
            return path
        except (TimedOut, NetworkError) as e:
            delay = TG_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
    raise TimedOut("Telegram download failed")

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

    local_path = None
    try:
        local_path = await download_telegram_file_safe(context, job.file_id, job.suffix or ".bin")
        upload_url = await asyncio.to_thread(rubika_request_upload_url, job.rubika_type)
        rubika_file_id = await asyncio.to_thread(rubika_upload_file, upload_url, local_path)

        await asyncio.to_thread(rubika_send_file, job.rubika_chat_id, rubika_file_id, text=final_text)
        await asyncio.sleep(3)

    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)

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
                
                # Fixed: Changed to MAX_SEND_ATTEMPTS
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
                # Fixed: Changed to MAX_SEND_ATTEMPTS
                await telegram_notify_error(
                    context, job.tg_chat_id, job.tg_message_id,
                    f"⚠️ خطا ناشناخته. تلاش {attempt}/{MAX_SEND_ATTEMPTS}\n"
                    f"نوع: {job.kind}\n"
                    f"تلاش بعدی تا 5 ثانیه دیگر...\n"
                    f"جزئیات: {type(e).__name__}: {str(e)[:200]}"
                )
                
                log.exception(f"Unknown error on attempt {attempt}: {e}")
                await asyncio.sleep(5)
                
        if not success:
            err_text = f"جزئیات: {type(last_error).__name__}: {str(last_error)[:300]}" if last_error else ""
            await telegram_notify_error(
                context, job.tg_chat_id, job.tg_message_id,
                f"⚠️ ارسال انجام نشد و بعد از {MAX_SEND_ATTEMPTS} تلاش رها شد.\n"
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
            item = AlbumItem(tg_message_id, p.file_id, ".jpg", "Image", "photo", None, getattr(p, "file_size", None), caption)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "photo", "Image", p.file_id, ".jpg", None, getattr(p, "file_size", None), None, caption))
            return

        if getattr(msg_obj, "video", None):
            v = msg_obj.video
            item = AlbumItem(tg_message_id, v.file_id, ".mp4", "Video", "video", getattr(v, "file_name", None), getattr(v, "file_size", None), caption)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "video", "Video", v.file_id, ".mp4", getattr(v, "file_name", None), getattr(v, "file_size", None), None, caption))
            return

        if getattr(msg_obj, "document", None):
            d = msg_obj.document
            suf = _doc_suffix(getattr(d, "file_name", None), ".bin")
            item = AlbumItem(tg_message_id, d.file_id, suf, "File", "document", getattr(d, "file_name", None), getattr(d, "file_size", None), caption)
            if not add_to_album(item): await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "document", "File", d.file_id, suf, getattr(d, "file_name", None), getattr(d, "file_size", None), None, caption))
            return

        if getattr(msg_obj, "audio", None):
            a = msg_obj.audio
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "audio", "File", a.file_id, _doc_suffix(getattr(a, "file_name", None), ".mp3"), getattr(a, "file_name", None), getattr(a, "file_size", None), None, caption))
            return
            
        if getattr(msg_obj, "voice", None):
            v = msg_obj.voice
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "voice", "File", v.file_id, ".ogg", None, getattr(v, "file_size", None), None, caption))
            return
            
        if getattr(msg_obj, "sticker", None):
            s = msg_obj.sticker
            suf = ".webm" if getattr(s, "is_video", False) else ".tgs" if getattr(s, "is_animated", False) else ".webp"
            await SEND_QUEUE.put(SendJob(source_kind, tg_chat_id, tg_message_id, rubika_chat_id, "sticker", "File", s.file_id, suf, None, getattr(s, "file_size", None), None, caption))
            return

    except Exception as e:
        log.exception("enqueue failed: %s", e)

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post
    if not msg or _is_bridge_log_message(msg): return
    
    # ✨ NEW FEATURE: Ignore messages that are older than 10 minutes (600 seconds)
    now = datetime.now(timezone.utc)
    if (now - msg.date).total_seconds() > 600:
        log.info(f"Ignored old channel message (older than 10 mins). Message ID: {msg.message_id}")
        return
        
    await handle_telegram_message_to_rubika(context, _route_rubika_chat_id("channel"), msg, "channel")

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or _is_bridge_log_message(msg): return
    
    # ✨ NEW FEATURE: Ignore messages that are older than 10 minutes (600 seconds)
    now = datetime.now(timezone.utc)
    if (now - msg.date).total_seconds() > 600:
        log.info(f"Ignored old group message (older than 10 mins). Message ID: {msg.message_id}")
        return
        
    await handle_telegram_message_to_rubika(context, _route_rubika_chat_id("group"), msg, "group")

# =========================
# Main
# =========================
async def start_background_tasks(app):
    for i in range(MAX_PARALLEL_UPLOADS):
        asyncio.create_task(send_worker(app, i + 1))
    asyncio.create_task(async_ticker())

def main() -> None:
    threading.Thread(target=watchdog_thread, daemon=True).start()

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=90.0, write_timeout=90.0, pool_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_post))
    app.add_handler(MessageHandler((filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & ~filters.COMMAND, on_group_message))

    class _FakeCtx:
        def __init__(self, bot): self.bot = bot
    app.bot_data["_ctx_for_workers"] = _FakeCtx(app.bot)

    app.post_init = start_background_tasks

    log.info("Bridge is running... (Polling)")
    
    # Allow pending updates through so we can manually filter them by the 10-minute rule
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()