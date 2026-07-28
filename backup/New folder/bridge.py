# -*- coding: utf-8 -*-

import os
import json
import tempfile
import logging
import asyncio
import re
import time
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

import requests
from telegram import Update
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# خواندن تنظیمات از فایل جیسون
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

TELEGRAM_BOT_TOKEN = CONFIG["TELEGRAM_BOT_TOKEN"]
RUBIKA_TOKEN = CONFIG["RUBIKA_TOKEN"]
RUBIKA_DEFAULT_CHAT_ID = CONFIG["RUBIKA_DEFAULT_CHAT_ID"]
MY_CHANNEL_LINK = CONFIG["MY_CHANNEL_LINK"]
ALBUM_FLUSH_DELAY_PHOTO_SECONDS = CONFIG["ALBUM_FLUSH_DELAY_PHOTO_SECONDS"]
ALBUM_FLUSH_DELAY_VIDEO_SECONDS = CONFIG["ALBUM_FLUSH_DELAY_VIDEO_SECONDS"]

TELEGRAM_MAX_DOWNLOAD_BYTES = 18 * 1024 * 1024  # 18MB
MAX_SEND_ATTEMPTS = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bridge")

# =========================
# Watchdog (برای ریستارت در صورت هنگ کردن)
# =========================
LAST_TICK = time.time()

async def async_ticker():
    # آپدیت زمان برای نشان دادن زنده بودن بات
    global LAST_TICK
    while True:
        LAST_TICK = time.time()
        await asyncio.sleep(30)

def watchdog_thread():
    # اگر بات بیشتر از ۲ دقیقه گیر کرد، اسکریپت را می‌بندد تا فایل bat دوباره آن را باز کند
    while True:
        time.sleep(30)
        if time.time() - LAST_TICK > 120:
            log.error("Watchdog: Bot is completely stuck! Forcing restart...")
            os._exit(1)

# =========================
# Rubika helpers
# =========================
class RubikaError(Exception): pass
class RubikaUploadTemporaryError(RubikaError): pass

def _rubika_call(method: str, payload: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=timeout, headers={"Connection": "close"})
    return r.json()

def rubika_send_text(chat_id: str, text: str) -> None:
    _rubika_call("sendMessage", {"chat_id": chat_id, "text": text})

def rubika_request_upload_url(file_type: str) -> str:
    data = _rubika_call("requestSendFile", {"type": file_type})
    root = (data.get("data") or data.get("result") or data)
    return root.get("upload_url") or data.get("upload_url")

def rubika_upload_file(upload_url: str, file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = requests.post(upload_url, files={"file": f}, timeout=240, headers={"Connection": "close"}).json()
    root = (r.get("data") or r.get("result") or r)
    return root.get("file_id") or r.get("file_id") or root.get("fileId") or root.get("id")

def rubika_send_file(chat_id: str, file_id: str) -> None:
    _rubika_call("sendFile", {"chat_id": chat_id, "file_id": file_id})

# =========================
# Utils
# =========================
def clean_text(text: Optional[str]) -> Optional[str]:
    # پاک کردن لینک‌ها و هشتگ‌های بقیه و اضافه کردن لینک کانال شما
    if not text:
        return None
    cleaned = re.sub(r'https?://\S+|#\S+|@\S+', '', text).strip()
    return f"{cleaned}\n\n{MY_CHANNEL_LINK}" if cleaned else MY_CHANNEL_LINK

async def download_telegram_file_safe(context, file_id: str, suffix: str) -> str:
    tg_file = await context.bot.get_file(file_id)
    fd, path = tempfile.mkstemp(prefix="tg_", suffix=suffix)
    os.close(fd)
    await tg_file.download_to_drive(custom_path=path)
    return path

# =========================
# Queue & Worker
# =========================
@dataclass
class SendJob:
    rubika_chat_id: str
    kind: str
    rubika_type: str
    file_id: Optional[str]
    suffix: Optional[str]
    file_size: Optional[int]
    text: Optional[str]

SEND_QUEUE: asyncio.Queue[SendJob] = asyncio.Queue()

async def _process_send_job(context, job: SendJob) -> None:
    # ارسال پیام‌های دارای فایل
    if job.file_id:
        local_path = None
        try:
            local_path = await download_telegram_file_safe(context, job.file_id, job.suffix or ".bin")
            upload_url = await asyncio.to_thread(rubika_request_upload_url, job.rubika_type)
            rubika_file_id = await asyncio.to_thread(rubika_upload_file, upload_url, local_path)
            
            await asyncio.to_thread(rubika_send_file, job.rubika_chat_id, rubika_file_id)
            await asyncio.sleep(2) # وقفه برای ترتیب آلبوم
            
            if job.text:
                await asyncio.to_thread(rubika_send_text, job.rubika_chat_id, job.text)
        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
    # ارسال پیام متنی خالی
    elif job.text:
        await asyncio.to_thread(rubika_send_text, job.rubika_chat_id, job.text)

async def send_worker(app) -> None:
    # تلاش مجدد پیام تا ۶ بار، بدون اینکه به آخر صف برود
    context = app.bot_data.get("_ctx")
    while True:
        job = await SEND_QUEUE.get()
        success = False
        
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                await _process_send_job(context, job)
                success = True
                break
            except Exception as e:
                log.warning(f"Error sending {job.kind} (Attempt {attempt}/{MAX_SEND_ATTEMPTS}): {e}")
                await asyncio.sleep(5)
                
        if not success:
            log.error(f"Dropped message after {MAX_SEND_ATTEMPTS} attempts: {job.kind}")
            
        await asyncio.sleep(1)
        SEND_QUEUE.task_done()

# =========================
# Album Buffering
# =========================
MEDIA_GROUPS = {}

async def flush_album_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # ارسال آلبوم جمع آوری شده به صف
    key = context.job.data["key"]
    group = MEDIA_GROUPS.pop(key, None)
    if not group: return

    items = group["items"]
    items.sort(key=lambda x: x["msg_id"])
    
    # گرفتن کپشن و چسباندن به آخرین عکس/ویدیو
    album_caption = next((it["cap"] for it in items if it["cap"]), None)
    for it in items: it["cap"] = None
    if album_caption: items[-1]["cap"] = album_caption

    for it in items:
        await SEND_QUEUE.put(SendJob(
            rubika_chat_id=group["rubika_chat_id"],
            kind=it["kind"], rubika_type=it["rtype"],
            file_id=it["fid"], suffix=it["suf"],
            file_size=it["size"], text=it["cap"]
        ))

# =========================
# Core Handler
# =========================
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post or update.message
    if not msg: return

    rubika_chat_id = RUBIKA_DEFAULT_CHAT_ID
    raw_text = getattr(msg, "text", None) or getattr(msg, "caption", None)
    final_text = clean_text(raw_text)

    # اگر فقط متن است
    if getattr(msg, "text", None) and not (msg.photo or msg.video or msg.document or msg.animation or msg.audio or msg.voice):
        await SEND_QUEUE.put(SendJob(rubika_chat_id, "text", "File", None, None, None, final_text))
        return

    # پیدا کردن نوع فایل
    fid, suf, rtype, kind, size = None, ".bin", "File", "document", None
    if msg.photo:
        fid, suf, rtype, kind, size = msg.photo[-1].file_id, ".jpg", "Image", "photo", msg.photo[-1].file_size
    elif msg.video:
        fid, suf, rtype, kind, size = msg.video.file_id, ".mp4", "Video", "video", msg.video.file_size
    elif msg.document:
        fid, suf, rtype, kind, size = msg.document.file_id, ".bin", "File", "document", msg.document.file_size
    
    if size and size > TELEGRAM_MAX_DOWNLOAD_BYTES:
        return # نادیده گرفتن فایل‌های بزرگتر از ۱۸ مگابایت

    # مدیریت آلبوم
    if msg.media_group_id:
        key = msg.media_group_id
        g = MEDIA_GROUPS.setdefault(key, {"items": [], "rubika_chat_id": rubika_chat_id, "has_video": False})
        g["items"].append({"msg_id": msg.message_id, "fid": fid, "suf": suf, "rtype": rtype, "kind": kind, "size": size, "cap": final_text})
        if rtype == "Video": g["has_video"] = True
        
        job_name = f"flush_{key}"
        for j in context.job_queue.get_jobs_by_name(job_name): j.schedule_removal()
        delay = ALBUM_FLUSH_DELAY_VIDEO_SECONDS if g["has_video"] else ALBUM_FLUSH_DELAY_PHOTO_SECONDS
        context.job_queue.run_once(flush_album_job, when=delay, data={"key": key}, name=job_name)
    else:
        await SEND_QUEUE.put(SendJob(rubika_chat_id, kind, rtype, fid, suf, size, final_text))

# =========================
# Main
# =========================
async def start_background_tasks(app):
    asyncio.create_task(send_worker(app))
    asyncio.create_task(async_ticker())

def main() -> None:
    # روشن کردن نخ محافظ برای بستن بات در زمان هنگ
    threading.Thread(target=watchdog_thread, daemon=True).start()

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=90.0, write_timeout=90.0, pool_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).build()
    app.add_handler(MessageHandler(filters.ALL, handle_msg))

    class Ctx: pass
    app.bot_data["_ctx"] = Ctx()
    app.bot_data["_ctx"].bot = app.bot

    app.post_init = start_background_tasks

    log.info("Bridge is running... (Polling)")
    # drop_pending_updates=True باعث میشود پیام های قدیمی که وقتی بات خاموش بود نادیده گرفته شوند
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()