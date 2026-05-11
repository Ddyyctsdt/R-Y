"""
main.py - حلقه اصلی ربات YouTube Bale Bot (نسخه ۳)
رفع باگ‌های ارسال سند، تحویل نتایج جستجو، پاسخ کال‌بک تکراری و مدیریت upload
اضافه شدن timeout برای عملیات‌ها، فرمان /channel، پایداری worker و محافظت در برابر هنگ
به‌روزرسانی برای جستجوی دو مرحله‌ای: scrapetube + scrape_watch
به‌روزرسانی timeoutها از تنظیمات پویا
"""

import os
import sys
import time
import threading
import uuid
import queue
import re
import json
import traceback
import concurrent.futures
from typing import Any, Dict, List, Optional, Tuple

import requests

import settings
from utils import (
    get_logger,
    load_json,
    save_json,
    split_file_binary,
    split_video_by_size,
    download_file,
    upload_manager,
    extract_video_id,
    safe_remove,
)
import youtube_core  # ماژول اصلی عملیات یوتیوب

_log = get_logger("main")

# ──────────────── ثابت‌های محلی ────────────────
MAX_SEND_SIZE = 20 * 1024 * 1024    # ۲۰ مگابایت
RESULTS_PER_PAGE = settings.UI_SETTINGS.get("result_page_size", 5)
LAST_SEARCH_FILE = os.path.join(settings.DATA_DIR, "last_search.json")
METHOD_CONFIG_FILE = os.path.join(settings.DATA_DIR, "method_config.json")
VIDEO_REGISTRY_FILE = os.path.join(settings.DATA_DIR, "video_registry.json")

# ──────────────── قفل‌ها و متغیرهای عمومی ────────────────
task_queue = queue.Queue()
queue_lock = threading.Lock()
registry_lock = threading.Lock()
state_lock = threading.Lock()

video_registry: Dict[str, str] = {}          # کلید = شماره (string), مقدار = video_id
last_search: Dict[str, Any] = {}             # {"results": [...], "query": "...", "method": "..."}
user_state: Dict[int, str] = {}              # chat_id -> "awaiting_query"/"awaiting_url"

# ──────────────── بارگذاری/ذخیره Video Registry ────────────────

def load_video_registry() -> Dict[str, str]:
    data = load_json(VIDEO_REGISTRY_FILE, {})
    if isinstance(data, dict):
        return data
    return {}

def save_video_registry() -> None:
    with registry_lock:
        save_json(VIDEO_REGISTRY_FILE, video_registry.copy())

# ──────────────── بارگذاری/ذخیره Last Search ────────────────

def load_last_search() -> Dict[str, Any]:
    return load_json(LAST_SEARCH_FILE, {})

def save_last_search(data: Dict[str, Any]) -> None:
    save_json(LAST_SEARCH_FILE, data)

# ──────────────── تنظیمات متدها (Method Config) ────────────────

def load_method_config() -> Dict[str, Any]:
    defaults = settings.DEFAULT_SESSION_SETTINGS.copy()
    user = load_json(METHOD_CONFIG_FILE, {})
    defaults.update(user)
    for key, val in settings.DEFAULT_SESSION_SETTINGS.items():
        if key not in defaults:
            defaults[key] = val
    return defaults

def save_method_config(config: Dict[str, Any]) -> None:
    save_json(METHOD_CONFIG_FILE, config)

# ──────────────── مدیریت کاربر ادمین ────────────────

def get_admin_chat_id() -> int:
    if os.path.exists(settings.ADMIN_FILE):
        data = load_json(settings.ADMIN_FILE, {})
        return data.get("admin_chat_id", settings.DEFAULT_ADMIN_CHAT_ID)
    save_json(settings.ADMIN_FILE, {"admin_chat_id": settings.DEFAULT_ADMIN_CHAT_ID})
    return settings.DEFAULT_ADMIN_CHAT_ID

def is_admin(chat_id: int) -> bool:
    return chat_id == get_admin_chat_id()

# ──────────────── توابع API بله ────────────────

def send_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> Optional[dict]:
    url = f"{settings.API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            _log.error(f"خطا در sendMessage: {data}")
            return None
        return data
    except Exception as e:
        _log.error(f"استثنا در sendMessage: {e}")
        return None

def send_document(chat_id: int, file_path: str, caption: str = "") -> Optional[dict]:
    """
    ارسال فایل به کاربر (تکه‌تکه در صورت نیاز).
    🟢 اصلاح‌شده: chat_id و caption به‌عنوان query parameter ارسال می‌شوند.
    """
    if not os.path.exists(file_path):
        _log.error(f"فایل برای ارسال یافت نشد: {file_path}")
        return None

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        _log.error(f"خطا در خواندن حجم فایل: {file_path}")
        return None

    if file_size > MAX_SEND_SIZE:
        _log.info(f"فایل {file_path} بزرگتر از ۲۰MB - تکه‌تکه می‌شود.")
        prefix = os.path.splitext(os.path.basename(file_path))[0]
        ext = os.path.splitext(file_path)[1]
        parts = split_file_binary(file_path, prefix, ext)
        if not parts:
            _log.error("تقسیم فایل موفق نبود.")
            return None

        total = len(parts)
        for idx, part_path in enumerate(parts, 1):
            part_caption = f"{caption} (بخش {idx}/{total})" if caption else f"بخش {idx}/{total}"
            send_document(chat_id, part_path, part_caption)
            try:
                os.remove(part_path)
            except OSError:
                _log.warning(f"حذف تکه {part_path} نشد.")

        try:
            os.remove(file_path)
        except OSError:
            _log.warning(f"حذف فایل اصلی {file_path} نشد.")
        return {"ok": True, "sent_parts": total}

    # ارسال مستقیم - استفاده از params بجای data
    url = f"{settings.API_BASE}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            params = {"chat_id": chat_id, "caption": caption}
            resp = requests.post(url, params=params, files=files,
                                 timeout=settings.REQUEST_TIMEOUT * 2)
            resp.raise_for_status()
            resp_data = resp.json()
            if not resp_data.get("ok"):
                _log.error(f"sendDocument خطا: {resp_data}")
                return None
            return resp_data
    except Exception as e:
        _log.exception(f"استثنا در sendDocument: {e}")
        return None

def get_updates(offset: int, timeout: int) -> Optional[dict]:
    url = f"{settings.API_BASE}/getUpdates"
    payload = {"offset": offset, "timeout": timeout}
    try:
        resp = requests.post(url, json=payload, timeout=timeout + 10)
        if resp.status_code != 200:
            _log.error(f"getUpdates status={resp.status_code}")
            return {"ok": True, "result": []}
        data = resp.json()
        if not data.get("ok"):
            _log.error(f"getUpdates خطا: {data}")
            return {"ok": True, "result": []}
        return data
    except Exception as e:
        _log.warning(f"خطا در getUpdates: {e}")
        return {"ok": True, "result": []}

def edit_reply_markup(chat_id: int, message_id: int, reply_markup: Dict) -> Optional[dict]:
    url = f"{settings.API_BASE}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
    try:
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _log.error(f"editMessageReplyMarkup: {e}")
        return None

def edit_message_text(chat_id: int, message_id: int, text: str,
                      reply_markup: Optional[Dict] = None) -> Optional[dict]:
    url = f"{settings.API_BASE}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _log.error(f"editMessageText: {e}")
        return None

def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """پاسخ به callback query. فقط یکبار در هر callback صدا شود."""
    url = f"{settings.API_BASE}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
    except Exception:
        pass

# ──────────────── منوها (Inline Keyboards) ────────────────

def build_main_menu() -> Dict:
    keyboard = [
        [{"text": "🔍 جستجو", "callback_data": "search"},
         {"text": "📥 دانلود با لینک", "callback_data": "download_link"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "settings"}],
        [{"text": "❓ راهنما", "callback_data": "help"},
         {"text": "📄 لاگ", "callback_data": "log"}],
    ]
    return {"inline_keyboard": keyboard}

def build_settings_menu() -> Dict:
    keyboard = [
        [{"text": "زنجیره دانلود", "callback_data": "settings_chain|download"},
         {"text": "زنجیره جستجو", "callback_data": "settings_chain|search"}],
        [{"text": "حالت ارسال", "callback_data": "settings_upload_mode"},
         {"text": "کیفیت", "callback_data": "settings_quality"}],
        [{"text": "حالت جستجو", "callback_data": "settings_search_mode"},
         {"text": "تعداد نتایج", "callback_data": "settings_page_size"}],
        [{"text": "🔙 بازگشت", "callback_data": "main_menu"}],
    ]
    return {"inline_keyboard": keyboard}

def build_method_chain_keyboard(category: str, method_config: Dict[str, Any]) -> Dict:
    chain = method_config.get(f"{category}_chain", [])
    if category == "search":
        methods = settings.SEARCH_METHODS
    else:
        methods = settings.DOWNLOAD_METHODS

    keyboard = []
    for key, meta in methods.items():
        active = key in chain
        text = f"{'✅' if active else '❌'} {meta['name']}"
        callback = f"toggle_method|{category}|{key}"
        keyboard.append([{"text": text, "callback_data": callback}])

    keyboard.append([
        {"text": "💾 ذخیره", "callback_data": f"save_chain|{category}"},
        {"text": "🔙 بازگشت", "callback_data": "settings"},
    ])
    return {"inline_keyboard": keyboard}

def build_quality_keyboard(current_quality: str) -> Dict:
    qualities = ["360p", "480p", "720p", "1080p", "4K"]
    keyboard = []
    for q in qualities:
        mark = "✅" if q == current_quality else "○"
        keyboard.append([{"text": f"{mark} {q}", "callback_data": f"quality_set|{q}"}])
    keyboard.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": keyboard}

# ──────────────── مدیریت Video Registry ────────────────

def register_videos_from_search(results: List[Dict[str, Any]]) -> None:
    """با آخرین نتایج جستجو، video_registry را جایگزین می‌کند."""
    with registry_lock:
        video_registry.clear()
        for idx, v in enumerate(results, 1):
            vid = v.get("video_id")
            if vid:
                video_registry[str(idx)] = vid
        save_video_registry()
    _log.info(f"video_registry با {len(video_registry)} ویدیو به‌روز شد.")

def get_video_id_by_command(cmd: str) -> Optional[str]:
    """استخراج video_id از یک فرمان /H1 یا /Download_1"""
    with registry_lock:
        return video_registry.get(cmd)

# ──────────────── سیستم صف و کارگر ────────────────

def enqueue_job(job: Dict[str, Any]) -> None:
    job.setdefault("job_id", uuid.uuid4().hex[:8])
    job.setdefault("created_at", time.time())
    with queue_lock:
        jobs = load_json(settings.QUEUE_FILE, [])
        jobs.append(job)
        save_json(settings.QUEUE_FILE, jobs)
    task_queue.put(job)
    _log.info(f"وظیفه {job['job_id']} ({job.get('command')}) اضافه شد.")

def process_job(job: Dict[str, Any]) -> None:
    """پردازش یک وظیفه از صف"""
    command = job.get("command")
    params = job.get("params", {})
    chat_id = job.get("chat_id")
    job_id = job.get("job_id", "unknown")
    job_folder = os.path.join(settings.DOWNLOADS_DIR, job_id)
    os.makedirs(job_folder, exist_ok=True)

    try:
        if command == "search":
            config = load_method_config()
            search_timeout = config.get("search_timeout", 90)
            query = params.get("query", "")
            limit = params.get("limit", 10)
            # جستجوی سریع با scrapetube
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    youtube_core.search_youtube, query=query, limit=limit
                )
                try:
                    results, method_used = future.result(timeout=search_timeout)
                except concurrent.futures.TimeoutError:
                    _log.warning(f"جستجو برای '{query}' بیش از {search_timeout}s طول کشید.")
                    send_message(chat_id, "⏰ عملیات بیش از حد طول کشید. می‌توانید timeout را در تنظیمات افزایش دهید.")
                    return
                except Exception as e:
                    _log.exception(f"خطا در جستجو: {e}")
                    send_message(chat_id, f"⛔ خطا در جستجو: {str(e)[:200]}")
                    return

            if not results:
                send_message(chat_id, "🔎 نتیجه‌ای یافت نشد.")
                return

            # ثبت فوری ویدیوها برای دستورات /H و /Download
            register_videos_from_search(results)

            # ذخیره آخرین جستجو
            save_last_search({
                "results": results,
                "query": query,
                "method": method_used or "scrapetube",
            })

            # ارسال پیام سرآیند و درخواست استخراج جزئیات برای هر ویدیو
            send_message(chat_id, f"🔍 {len(results)} نتیجه پیدا شد. اطلاعات در حال دریافت...")
            for idx, v in enumerate(results, 1):
                vid = v.get("video_id")
                if vid:
                    enqueue_job({
                        "command": "scrape_watch",
                        "params": {"video_id": vid, "index": idx},
                        "chat_id": chat_id
                    })

        elif command == "scrape_watch":
            config = load_method_config()
            watch_timeout = config.get("watch_timeout", 60)
            video_id = params.get("video_id")
            index = params.get("index", "?")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(youtube_core.scrape_watch_page, video_id)
                try:
                    info = future.result(timeout=watch_timeout)
                except concurrent.futures.TimeoutError:
                    send_message(chat_id, "⏰ عملیات بیش از حد طول کشید. می‌توانید timeout را در تنظیمات افزایش دهید.")
                    return
                except Exception as e:
                    _log.exception(f"scrape_watch error for {video_id}: {e}")
                    send_message(chat_id, f"⚠️ خطا در دریافت اطلاعات ویدیوی {index}: {str(e)[:200]}")
                    return

            if not info or not info.get("title"):
                send_message(chat_id, f"⚠️ اطلاعات ویدیوی {index} دریافت نشد. شناسه: {video_id}")
                return

            # دانلود و ارسال تامنیل
            try:
                thumb_path, _ = youtube_core.download_thumbnail(video_id, job_folder)
                if thumb_path:
                    send_document(chat_id, thumb_path, caption=f"{info['title']} (اسکرپ)")
                    try:
                        os.remove(thumb_path)
                    except OSError:
                        pass
            except Exception:
                pass

            desc = (info.get("description") or "")[:300]
            msg = (
                f"📹 {info.get('title')}\n"
                f"👤 {info.get('uploader', '؟')}\n"
                f"👁 {info.get('view_count', '؟')} | ⏱ {info.get('duration', '؟')} ثانیه\n"
                f"📝 {desc}\n"
                f"🔗 https://youtube.com/watch?v={video_id}\n"
                f"📥 /Download_{index}"
            )
            send_message(chat_id, msg)

        elif command == "info":
            config = load_method_config()
            info_timeout = config.get("info_timeout", 60)
            video_id = params.get("video_id")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(youtube_core.get_video_info, video_id=video_id)
                try:
                    info_dict, method_used = future.result(timeout=info_timeout)
                except concurrent.futures.TimeoutError:
                    _log.warning(f"دریافت اطلاعات برای {video_id} بیش از {info_timeout}s طول کشید.")
                    send_message(chat_id, "⏰ عملیات بیش از حد طول کشید. می‌توانید timeout را در تنظیمات افزایش دهید.")
                    return
                except Exception as e:
                    _log.exception(f"خطا در دریافت اطلاعات: {e}")
                    send_message(chat_id, f"⛔ خطا: {str(e)[:200]}")
                    return

            if not info_dict or not info_dict.get("title"):
                send_message(chat_id, "❌ اطلاعات ویدیو دریافت نشد.")
                return

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as thumb_exec:
                    thumb_future = thumb_exec.submit(youtube_core.download_thumbnail, video_id, job_folder)
                    thumb_path, _ = thumb_future.result(timeout=10)
                    if thumb_path:
                        send_document(chat_id, thumb_path, caption=f"{info_dict['title']} (روش: {method_used})")
                        try:
                            os.remove(thumb_path)
                        except OSError:
                            pass
            except concurrent.futures.TimeoutError:
                _log.warning("دانلود تامنیل بیش از ۱۰ ثانیه طول کشید - نادیده گرفته شد.")
            except Exception as e:
                _log.warning(f"خطای دانلود تامنیل: {e}")

            desc = (info_dict.get("description") or "")[:200]
            msg = (
                f"📹 {info_dict.get('title')}\n"
                f"👤 {info_dict.get('author', '؟')}\n"
                f"👁 {info_dict.get('view_count', '؟')} | ⏱ {info_dict.get('duration', '؟')} ثانیه\n"
                f"📝 {desc}\n\n"
                f"🔗 https://youtube.com/watch?v={video_id}\n"
                f"🧩 روش: {method_used}"
            )
            send_message(chat_id, msg)

        elif command == "download":
            video_id = params.get("video_id")
            config = load_method_config()
            quality = config.get("download_quality", "720p")
            upload_mode = config.get("upload_mode", "playable_chunks")
            file_path, method_used = youtube_core.download_video(video_id, job_folder, quality=quality)

            if not file_path:
                send_message(chat_id, "❌ دانلود ویدیو ناموفق بود.")
                return

            if upload_mode == "playable_chunks":
                parts = split_video_by_size(file_path, max_size_bytes=MAX_SEND_SIZE)
            else:
                import zipfile
                zip_path = os.path.join(job_folder, f"{video_id}.zip")
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.write(file_path, os.path.basename(file_path))
                parts = split_file_binary(zip_path, os.path.splitext(os.path.basename(zip_path))[0], ".zip")
                os.remove(zip_path)

            state_file = os.path.join(settings.DATA_DIR, f"upload_state_{job_id}.json")
            upload_timeout = settings.REQUEST_TIMEOUT * len(parts)
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as up_exec:
                    up_future = up_exec.submit(upload_manager, parts, chat_id, send_document, state_file, max_retries=2)
                    success = up_future.result(timeout=upload_timeout)
            except concurrent.futures.TimeoutError:
                _log.warning("ارسال ویدیو بیش از حد طول کشید - لغو شد.")
                send_message(chat_id, "⏰ عملیات بیش از حد طول کشید. می‌توانید timeout را در تنظیمات افزایش دهید.")
                success = False
            except Exception as e:
                _log.exception(f"خطا در ارسال ویدیو: {e}")
                success = False

            if success:
                send_message(chat_id, f"✅ ویدیو با موفقیت ارسال شد (روش: {method_used})")
            else:
                send_message(chat_id, "⚠️ خطایی در ارسال ویدیو رخ داد. لطفاً دوباره تلاش کنید.")

            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

        elif command == "channel":
            channel_id = params.get("channel_id")
            sort_by = params.get("sort_by", "newest")
            limit = params.get("limit", settings.CHANNEL_VIDEOS_LIMIT)
            config = load_method_config()
            search_timeout = config.get("search_timeout", 90)

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    youtube_core.get_channel_videos,
                    channel_id=channel_id, sort_by=sort_by, max_results=limit
                )
                try:
                    videos = future.result(timeout=search_timeout)
                except concurrent.futures.TimeoutError:
                    send_message(chat_id, "⏰ عملیات بیش از حد طول کشید. می‌توانید timeout را در تنظیمات افزایش دهید.")
                    return
                except Exception as e:
                    _log.exception(f"خطا در channel: {e}")
                    send_message(chat_id, f"⛔ خطا: {str(e)[:200]}")
                    return

            if not videos:
                send_message(chat_id, "❌ دریافت ویدیوهای کانال ناموفق بود.")
                return

            register_videos_from_search(videos)
            lines = [f"📺 ویدیوهای کانال (مرتب‌سازی: {sort_by}) — {len(videos)} نتیجه"]
            for idx, v in enumerate(videos, 1):
                title = (v.get('title') or 'بی‌نام')[:40]
                duration = v.get('duration', '?')
                views = v.get('views', '?')
                lines.append(f"{idx}️⃣ {title} | ⏱ {duration} | 👁 {views}")
                lines.append(f"   📋 /H{idx}   📥 /Download_{idx}")

            chunk_size = 15
            for i in range(0, len(lines), chunk_size):
                chunk = lines[i:i+chunk_size]
                send_message(chat_id, "\n".join(chunk))

            save_last_search({
                "results": videos,
                "query": f"channel:{channel_id}",
                "method": "playwright_channel",
            })

        elif command == "batch_download":
            last = load_last_search().get("results", [])
            if not last:
                send_message(chat_id, "⚠️ ابتدا یک جستجو انجام دهید.")
                return
            for v in last:
                vid = v.get("video_id")
                if vid:
                    enqueue_job({"command": "download", "params": {"video_id": vid}, "chat_id": chat_id})
            send_message(chat_id, f"📥 دانلود {len(last)} ویدیو به صف اضافه شد.")

        else:
            send_message(chat_id, "⚠️ فرمان ناشناخته در صف.")

    except Exception as e:
        _log.exception(f"خطا در پردازش وظیفه {job_id}: {e}")
        send_message(chat_id, f"⛔ خطایی رخ داد: {str(e)[:200]}")
    finally:
        try:
            if os.path.exists(job_folder) and not os.listdir(job_folder):
                os.rmdir(job_folder)
        except Exception:
            pass


def run_job_with_timeout(job: dict, timeout_seconds: int) -> Optional[Exception]:
    """
    اجرای process_job در یک ترد جداگانه با زمان‌بندی.
    در صورتی که ترد پس از timeout_seconds ثانیه هنوز زنده بود،
    ترد رها می‌شود و TimeoutError برگردانده می‌شود.
    """
    exception_occurred = None

    def target():
        nonlocal exception_occurred
        try:
            process_job(job)
        except Exception as e:
            exception_occurred = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)

    if t.is_alive():
        _log.critical(f"وظیفه {job.get('job_id')} ({job.get('command')}) پس از {timeout_seconds} ثانیه پایان نیافت — رها شد.")
        return TimeoutError("Job execution timed out")
    return exception_occurred


def worker_loop() -> None:
    """حلقه کارگر صف با محافظت در برابر خرابی و هنگ"""
    _log.info("کارگر صف آغاز به کار کرد.")
    with queue_lock:
        pending = load_json(settings.QUEUE_FILE, [])
        for job in pending:
            task_queue.put(job)
        _log.info(f"{len(pending)} وظیفه از فایل صف بارگذاری شد.")

    while True:
        try:
            job = task_queue.get(timeout=30)
        except queue.Empty:
            continue

        job_id = job.get('job_id', '?')
        command = job.get('command', '?')
        chat_id = job.get('chat_id')

        # انتخاب timeout بر اساس نوع دستور و تنظیمات کاربر
        config = load_method_config()
        if command == "scrape_watch":
            outer_timeout = config.get("watch_timeout", 60) + 10
        elif command == "search":
            outer_timeout = config.get("search_timeout", 90) + 15
        elif command == "info":
            outer_timeout = config.get("info_timeout", 60) + 10
        elif command == "download":
            outer_timeout = 120
        elif command == "channel":
            outer_timeout = config.get("search_timeout", 90) + 15
        else:
            outer_timeout = 35

        _log.info(f"شروع پردازش {job_id} ({command}) با timeout {outer_timeout}s")
        err = run_job_with_timeout(job, outer_timeout)

        if err is None:
            _log.info(f"پایان وظیفه {job_id}")
        elif isinstance(err, TimeoutError):
            try:
                if chat_id:
                    send_message(chat_id, "⏰ عملیات بیش از حد طول کشید. می‌توانید timeout را در تنظیمات افزایش دهید.")
            except:
                pass
        else:
            _log.exception(f"وظیفه {job_id} با خطا مواجه شد: {err}")
            try:
                if chat_id:
                    send_message(chat_id, f"⛔ خطا در پردازش درخواست: {str(err)[:200]}")
            except:
                pass

        task_queue.task_done()

        # حذف وظیفه از فایل صف
        with queue_lock:
            jobs = load_json(settings.QUEUE_FILE, [])
            jobs = [j for j in jobs if j.get("job_id") != job_id]
            save_json(settings.QUEUE_FILE, jobs)


# ──────────────── مدیریت پیام‌های کاربر ────────────────

def handle_message(chat_id: int, text: str) -> None:
    if not is_admin(chat_id):
        send_message(chat_id, "⛔ دسترسی ندارید.")
        return

    with state_lock:
        state = user_state.get(chat_id)

    if state == "awaiting_query" and not text.startswith("/"):
        with state_lock:
            user_state.pop(chat_id, None)
        job = {"command": "search", "params": {"query": text, "limit": RESULTS_PER_PAGE}, "chat_id": chat_id}
        enqueue_job(job)
        send_message(chat_id, f"🔎 جستجوی «{text}» در صف قرار گرفت.")
        return

    if state == "awaiting_url" and not text.startswith("/"):
        with state_lock:
            user_state.pop(chat_id, None)
        video_id = extract_video_id(text)
        if not video_id:
            send_message(chat_id, "❌ لینک یوتیوب نامعتبر است.")
            return
        job = {"command": "info", "params": {"video_id": video_id}, "chat_id": chat_id}
        enqueue_job(job)
        send_message(chat_id, "🔍 دریافت اطلاعات ویدیو...")
        return

    if text == "/start":
        send_message(chat_id, "🤖 به ربات YouTube Bale خوش آمدید!", reply_markup=build_main_menu())

    elif text == "/help":
        help_text = (
            "📖 **راهنما**\n"
            "از منوی زیر برای جستجو و دانلود استفاده کنید.\n"
            "همچنین می‌توانید فرمان‌های سریع زیر را بفرستید:\n"
            "/H1 → اطلاعات ویدیوی شماره ۱\n"
            "/Download_1 → دانلود ویدیوی شماره ۱\n"
            "/channel @handle newest → ویدیوهای کانال\n"
            "/batchdownload → دانلود همه نتایج آخرین جستجو\n"
            "/log → دریافت فایل لاگ\n"
            "/start → نمایش منو"
        )
        send_message(chat_id, help_text)

    elif text.startswith("/H"):
        match = re.match(r"/H(\d+)", text)
        if match:
            idx = match.group(1)
            video_id = get_video_id_by_command(idx)
            if video_id:
                enqueue_job({"command": "info", "params": {"video_id": video_id}, "chat_id": chat_id})
                send_message(chat_id, "⏳ دریافت اطلاعات...")
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نادرست. مثال: /H1")

    elif text.startswith("/Download_"):
        match = re.match(r"/Download_(\d+)", text)
        if match:
            idx = match.group(1)
            video_id = get_video_id_by_command(idx)
            if video_id:
                enqueue_job({"command": "download", "params": {"video_id": video_id}, "chat_id": chat_id})
                send_message(chat_id, "📥 دانلود آغاز شد...")
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نادرست. مثال: /Download_1")

    elif text.startswith("/channel"):
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "❗ مثال:\n/channel @Google newest\n"
                                 "/channel UCXuqSBlHAE6Xw-yeJA0Tunw popular\n\n"
                                 "مرتب‌سازی: newest, oldest, popular")
            return

        channel_id = parts[1]
        sort_by = parts[2] if len(parts) > 2 else "newest"

        if sort_by not in ("newest", "oldest", "popular"):
            send_message(chat_id, "❗ مرتب‌سازی باید newest, oldest یا popular باشد.")
            return

        enqueue_job({
            "command": "channel",
            "params": {"channel_id": channel_id, "sort_by": sort_by},
            "chat_id": chat_id
        })
        send_message(chat_id, f"📺 در حال دریافت ویدیوهای کانال {channel_id} (مرتب‌سازی: {sort_by})...")

    elif text == "/batchdownload":
        last = load_last_search().get("results", [])
        if not last:
            send_message(chat_id, "⚠️ ابتدا جستجو کنید.")
        else:
            for v in last:
                vid = v.get("video_id")
                if vid:
                    enqueue_job({"command": "download", "params": {"video_id": vid}, "chat_id": chat_id})
            send_message(chat_id, f"📥 دانلود {len(last)} ویدیو آغاز شد.")

    elif text == "/log":
        if os.path.exists(settings.LOG_FILE):
            send_document(chat_id, settings.LOG_FILE, caption="📄 فایل لاگ")
        else:
            send_message(chat_id, "📭 فایل لاگ خالی است.")

    else:
        send_message(chat_id, "⚠️ فرمان نامعتبر. از منوی زیر استفاده کنید:", reply_markup=build_main_menu())

# ──────────────── مدیریت callback ها ────────────────

def handle_callback(chat_id: int, data: str, message_id: int, callback_query_id: str) -> None:
    if not is_admin(chat_id):
        answer_callback_query(callback_query_id, "⛔ دسترسی ندارید.")
        return

    parts = data.split("|")
    action = parts[0]

    if action == "search":
        with state_lock:
            user_state[chat_id] = "awaiting_query"
        answer_callback_query(callback_query_id, "🔍 عبارت جستجو را تایپ کنید.")
        edit_message_text(chat_id, message_id, "🔍 عبارت مورد نظر خود را تایپ کنید:")

    elif action == "download_link":
        with state_lock:
            user_state[chat_id] = "awaiting_url"
        answer_callback_query(callback_query_id, "📥 لینک یوتیوب را بفرستید.")
        edit_message_text(chat_id, message_id, "📥 لینک ویدیوی یوتیوب را بفرستید:")

    elif action == "settings":
        keyboard = build_settings_menu()
        edit_message_text(chat_id, message_id, "⚙️ تنظیمات:", reply_markup=keyboard)

    elif action == "help":
        edit_message_text(chat_id, message_id, "/help را ببینید.")
        handle_message(chat_id, "/help")

    elif action == "log":
        edit_message_text(chat_id, message_id, "📄 در حال ارسال فایل لاگ...")
        handle_message(chat_id, "/log")

    elif action == "settings_chain":
        category = parts[1]
        config = load_method_config()
        keyboard = build_method_chain_keyboard(category, config)
        edit_message_text(chat_id, message_id,
                          f"زنجیره {'جستجو' if category == 'search' else 'دانلود'}:",
                          reply_markup=keyboard)

    elif action == "toggle_method":
        category = parts[1]
        key = parts[2]
        config = load_method_config()
        chain_key = f"{category}_chain"
        chain = config.get(chain_key, [])
        if key in chain:
            chain.remove(key)
        else:
            chain.append(key)
        config[chain_key] = chain
        save_method_config(config)
        new_kb = build_method_chain_keyboard(category, config)
        edit_reply_markup(chat_id, message_id, new_kb)
        answer_callback_query(callback_query_id, "✅ وضعیت متد تغییر کرد.")

    elif action == "save_chain":
        category = parts[1]
        answer_callback_query(callback_query_id, "💾 زنجیره ذخیره شد.")
        keyboard = build_settings_menu()
        edit_message_text(chat_id, message_id, "⚙️ تنظیمات:", reply_markup=keyboard)

    elif action == "settings_upload_mode":
        config = load_method_config()
        current = config.get("upload_mode", "playable_chunks")
        new_mode = "zip" if current == "playable_chunks" else "playable_chunks"
        config["upload_mode"] = new_mode
        save_method_config(config)
        answer_callback_query(callback_query_id, f"حالت ارسال: {new_mode}")

    elif action == "settings_quality":
        config = load_method_config()
        current = config.get("download_quality", "720p")
        keyboard = build_quality_keyboard(current)
        edit_message_text(chat_id, message_id, "کیفیت دانلود:", reply_markup=keyboard)

    elif action == "quality_set":
        quality = parts[1]
        config = load_method_config()
        config["download_quality"] = quality
        save_method_config(config)
        answer_callback_query(callback_query_id, f"کیفیت روی {quality} تنظیم شد.")
        new_kb = build_quality_keyboard(quality)
        edit_reply_markup(chat_id, message_id, new_kb)

    elif action == "settings_search_mode":
        config = load_method_config()
        current = config.get("search_mode", "browser")
        new_mode = "api" if current == "browser" else "browser"
        config["search_mode"] = new_mode
        save_method_config(config)
        answer_callback_query(callback_query_id, f"حالت جستجو: {new_mode}")

    elif action == "settings_page_size":
        config = load_method_config()
        sizes = [5, 10, 15]
        current = config.get("page_size", RESULTS_PER_PAGE)
        idx = sizes.index(current) if current in sizes else 0
        new_size = sizes[(idx + 1) % len(sizes)]
        config["page_size"] = new_size
        save_method_config(config)
        answer_callback_query(callback_query_id, f"تعداد نتایج: {new_size}")

    elif action == "main_menu":
        keyboard = build_main_menu()
        edit_message_text(chat_id, message_id, "🏠 منوی اصلی:", reply_markup=keyboard)

    else:
        answer_callback_query(callback_query_id, "⚠️ عملیات نامعتبر.")


# ──────────────── حلقه اصلی ربات ────────────────

def main() -> None:
    _log.info("ربات YouTube Bale Bot (نسخه ۳) آغاز به کار کرد.")
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.makedirs(settings.DOWNLOADS_DIR, exist_ok=True)

    global video_registry
    video_registry = load_video_registry()
    _log.info(f"{len(video_registry)} ویدیو در رجیستری بارگذاری شد.")

    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    offset = 0
    while True:
        try:
            resp = get_updates(offset, settings.LONG_POLL_TIMEOUT)
            if resp is None or not resp.get("ok"):
                time.sleep(2)
                continue

            for update in resp.get("result", []):
                if "message" in update and "text" in update["message"]:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg["text"]
                    threading.Thread(target=handle_message, args=(chat_id, text), daemon=True).start()

                if "callback_query" in update:
                    cq = update["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    message_id = cq["message"]["message_id"]
                    data = cq.get("data", "")
                    callback_query_id = cq["id"]
                    threading.Thread(
                        target=handle_callback,
                        args=(chat_id, data, message_id, callback_query_id),
                        daemon=True
                    ).start()

                offset = update["update_id"] + 1

        except Exception as e:
            _log.exception(f"خطا در حلقه اصلی: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
