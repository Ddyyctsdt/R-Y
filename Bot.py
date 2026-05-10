#!/usr/bin/env python3
"""
YouTube Bale Bot - Single File Version
ربات دانلود ویدیو یوتیوب برای پیام‌رسان بله
بدون تحریم، با استفاده از API واسط hub.ytconvert.org
"""

import os
import sys
import time
import json
import logging
import threading
import uuid
import re
import requests
from typing import Optional, Dict, List
from urllib.parse import urlparse, unquote

# ═══════════════════════════ تنظیمات ═══════════════════════════
BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
MAX_SEND_SIZE = 20 * 1024 * 1024  # 20MB for chunks
ZIP_PART_SIZE = MAX_SEND_SIZE

# مسیرها
DATA_DIR = "data"
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")
LOG_FILE = "bot.log"
DOWNLOADS_DIR = "downloads"

# شناسه ادمین پیش‌فرض
DEFAULT_ADMIN_CHAT_ID = 46829437  # 👈 اینجا شناسه خودت را بگذار

# تنظیمات API واسط
CONVERTER_API = "https://hub.ytconvert.org/api/download"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# کیفیت‌های پشتیبانی‌شده
VIDEO_QUALITIES = ['144p', '360p', '480p', '720p', '1080p']
DEFAULT_QUALITY = '720p'

# ═══════════════════════════ لاگ ضدکرش ═══════════════════════════
class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        FlushFileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ytbot")

# ═══════════════════════════ ابزارهای کمکی ═══════════════════════════
def split_file_binary(file_path: str, prefix: str, ext: str) -> List[str]:
    """تقسیم فایل به تکه‌های ۲۰ مگابایتی"""
    parts = []
    part_size = ZIP_PART_SIZE
    output_dir = os.path.dirname(file_path) or '.'
    if ext == '.zip':
        pattern = f"{prefix}.zip.{{:03d}}"
    else:
        pattern = f"{prefix}.part{{:03d}}{ext}"

    with open(file_path, 'rb') as f:
        i = 0
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            i += 1
            part_path = os.path.join(output_dir, pattern.format(i))
            with open(part_path, 'wb') as pf:
                pf.write(chunk)
            parts.append(part_path)
    return parts

def download_file(url: str, save_dir: str, filename: str = None, timeout: int = 120) -> Optional[str]:
    """دانلود فایل از اینترنت"""
    os.makedirs(save_dir, exist_ok=True)
    if not filename:
        filename = os.path.basename(unquote(urlparse(url).path)) or "downloaded_file"
    base, ext = os.path.splitext(filename)
    counter = 1
    dest = os.path.join(save_dir, filename)
    while os.path.exists(dest):
        dest = os.path.join(save_dir, f"{base}_{counter}{ext}")
        counter += 1
    headers = {'User-Agent': USER_AGENT}
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        logger.info(f"⬇️ دانلود موفق: {dest}")
        return dest
    except Exception as e:
        logger.error(f"❌ دانلود ناموفق: {e}")
        return None

def load_json(path: str, default=None):
    if default is None:
        default = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default

def save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

# ═══════════════════════════ API بله ═══════════════════════════
def send_message(chat_id: int, text: str) -> Optional[dict]:
    url = f"{API_BASE}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=REQUEST_TIMEOUT)
        return resp.json() if resp.ok else None
    except Exception as e:
        logger.error(f"sendMessage: {e}")
        return None

def send_document(chat_id: int, file_path: str, caption: str = "") -> Optional[dict]:
    if not os.path.exists(file_path):
        return None
    size = os.path.getsize(file_path)
    if size > MAX_SEND_SIZE:
        prefix = os.path.splitext(os.path.basename(file_path))[0]
        ext = os.path.splitext(file_path)[1]
        parts = split_file_binary(file_path, prefix, ext)
        total = len(parts)
        for idx, part in enumerate(parts, 1):
            c = f"{caption} (بخش {idx}/{total})" if caption else f"بخش {idx}/{total}"
            send_document(chat_id, part, c)  # recursive but smaller
            os.remove(part)
        try:
            os.remove(file_path)
        except:
            pass
        return {"ok": True, "sent_parts": total}

    url = f"{API_BASE}/sendDocument"
    try:
        with open(file_path, 'rb') as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT * 2)
            return resp.json() if resp.ok else None
    except Exception as e:
        logger.error(f"sendDocument: {e}")
        return None

def get_updates(offset: int, timeout: int = LONG_POLL_TIMEOUT) -> dict:
    url = f"{API_BASE}/getUpdates"
    try:
        resp = requests.post(url, json={"offset": offset, "timeout": timeout}, timeout=timeout + 10)
        if resp.status_code != 200:
            return {"ok": True, "result": []}
        return resp.json()
    except:
        return {"ok": True, "result": []}

# ═══════════════════════════ مدیریت ادمین ═══════════════════════════
def get_admin_id() -> int:
    data = load_json(ADMIN_FILE, {"admin_chat_id": DEFAULT_ADMIN_CHAT_ID})
    return data.get("admin_chat_id", DEFAULT_ADMIN_CHAT_ID)

# ═══════════════════════════ API واسط یوتیوب ═══════════════════════════
def fetch_download_url(youtube_url: str, quality: str = DEFAULT_QUALITY) -> Optional[Dict[str, str]]:
    """
    ارسال درخواست به hub.ytconvert.org و دریافت downloadUrl.
    خروجی: دیکشنری شامل url (string) و filename (string) یا None
    """
    session = requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://media.ytmp3.gg/",
        "Content-Type": "application/json",
        "Origin": "https://media.ytmp3.gg",
    }

    # پری‌بازدید از سایت مبدا
    try:
        session.get("https://media.ytmp3.gg/", headers=headers, timeout=10)
    except:
        pass

    payload = {
        "url": youtube_url,
        "os": "linux",
        "output": {
            "type": "video",
            "format": "mp4",
            "quality": quality
        }
    }

    logger.info(f"📤 ارسال درخواست تبدیل: {youtube_url} کیفیت {quality}")
    try:
        resp = session.post(CONVERTER_API, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"❌ خطا در درخواست تبدیل: {e}")
        return None

    status_url = data.get("statusUrl")
    title = data.get("title", "video")

    if not status_url:
        logger.error("❌ statusUrl در پاسخ موجود نیست")
        return None

    # Polling
    download_url = None
    for attempt in range(60):
        time.sleep(2)
        try:
            sr = session.get(status_url, headers=headers, timeout=15)
            sr.raise_for_status()
            status_data = sr.json()
        except Exception as e:
            logger.warning(f"⚠️ خطا در polling: {e}")
            continue

        status = status_data.get("status")
        progress = status_data.get("progress", 0)
        if attempt % 5 == 0:
            logger.info(f"   [{attempt+1}] وضعیت: {status} ({progress}%)")

        if status == "completed":
            download_url = status_data.get("downloadUrl")
            if download_url:
                logger.info("✅ تبدیل کامل شد")
                break
        elif status == "failed":
            logger.error("❌ تبدیل ناموفق")
            return None

    if not download_url:
        logger.error("⏰ تایم‌اوت")
        return None

    # نام‌گذاری
    video_id_match = re.search(r'(?:v=|\/)([a-zA-Z0-9_-]{11})', youtube_url)
    video_id = video_id_match.group(1) if video_id_match else "unknown"
    safe_title = re.sub(r'[^a-zA-Z0-9\-_]', '_', title.replace(' ', '_'))[:60].strip('_') or video_id
    filename = f"{safe_title}_{video_id}_{quality}.mp4"

    return {"url": download_url, "filename": filename}

# ═══════════════════════════ هسته ربات ═══════════════════════════
def handle_message(chat_id: int, text: str):
    if chat_id != get_admin_id():
        send_message(chat_id, "⛔ دسترسی ندارید")
        return

    text = text.strip()
    logger.info(f"📩 پیام: {text}")

    if text.startswith("/start"):
        send_message(chat_id, "👋 سلام! لینک یوتیوب را با /download بفرستید")

    elif text.startswith("/help"):
        send_message(chat_id, "📘 /download <لینک>\n🔍 /log\nℹ️ /help")

    elif text.startswith("/log"):
        if os.path.exists(LOG_FILE):
            send_document(chat_id, LOG_FILE, caption="📄 لاگ")
        else:
            send_message(chat_id, "📭 لاگ خالی است")

    elif text.startswith("/download"):
        url = text[len("/download"):].strip()
        if not url:
            send_message(chat_id, "❗ مثال: /download https://youtu.be/xxx")
            return
        if not url.startswith("http"):
            send_message(chat_id, "❗ لینک نامعتبر")
            return

        # فرآیند دانلود در ترد جدا
        def download_job():
            send_message(chat_id, f"⏳ در حال دریافت لینک دانلود...")
            result = fetch_download_url(url, DEFAULT_QUALITY)
            if not result:
                send_message(chat_id, "❌ دریافت لینک دانلود ناموفق")
                return

            send_message(chat_id, f"⬇️ دانلود فایل...")
            job_dir = os.path.join(DOWNLOADS_DIR, uuid.uuid4().hex[:8])
            os.makedirs(job_dir, exist_ok=True)
            file_path = download_file(result["url"], job_dir, result["filename"])
            if not file_path:
                send_message(chat_id, "❌ دانلود فایل ناموفق")
                return

            send_message(chat_id, "📤 ارسال فایل...")
            send_document(chat_id, file_path, caption=f"🎬 {result['filename']}")
            # پاکسازی
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                os.rmdir(job_dir)
            except:
                pass

        threading.Thread(target=download_job, daemon=True).start()

    else:
        send_message(chat_id, "⚠️ دستور نامعتبر. /help")

# ═══════════════════════════ حلقه اصلی ═══════════════════════════
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    logger.info("🤖 ربات شروع شد")
    offset = 0
    while True:
        try:
            resp = get_updates(offset)
            if not resp.get("ok"):
                time.sleep(2)
                continue
            for update in resp.get("result", []):
                if "message" in update and "text" in update["message"]:
                    msg = update["message"]
                    threading.Thread(
                        target=handle_message,
                        args=(msg["chat"]["id"], msg["text"]),
                        daemon=True
                    ).start()
                offset = update["update_id"] + 1
        except Exception as e:
            logger.error(f"❌ خطای حلقه اصلی: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
