"""
utils.py – جعبه ابزار پروژه YouTube Bale Bot
شامل توابع کمکی برای لاگ‌گیری، مدیریت فایل، دانلود، اعتبارسنجی و کار با JSON
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import threading
from typing import Dict, Optional, List
from urllib.parse import urlparse, unquote

import requests

import settings

# ──────────────────────────── لاگ‌گیری ضدکرش ────────────────────────────

class FlushFileHandler(logging.FileHandler):
    """یک FileHandler که پس از هر لاگ flush می‌کند تا در صورت کرش داده‌ها از دست نروند."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logging() -> None:
    """تنظیم logger اصلی پروژه (youtube_bot) با handlers مربوط به فایل و کنسول."""
    logger = logging.getLogger('youtube_bot')
    if logger.handlers:  # قبلاً تنظیم شده
        return

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Handler برای فایل لاگ
    os.makedirs(os.path.dirname(settings.LOG_FILE) or '.', exist_ok=True)
    file_handler = FlushFileHandler(settings.LOG_FILE, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Handler برای کنسول
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    # جلوگیری از ارسال لاگ‌ها به loggerهای بالادستی (root)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """برگرداندن یک logger فرزند از youtube_bot با نام داده‌شده."""
    if not logging.getLogger('youtube_bot').handlers:
        setup_logging()
    return logging.getLogger(f'youtube_bot.{name}')


# ──────────────────────────── مدیریت فایل ────────────────────────────

def split_file_binary(file_path: str, prefix: str, ext: str) -> List[str]:
    """
    تقسیم فایل باینری به قطعه‌های با اندازه‌ی ZIP_PART_SIZE.

    Args:
        file_path: مسیر فایل اصلی.
        prefix: پیشوند نام قطعه‌ها.
        ext: پسوند اصلی فایل (مثلاً .mp4 یا .zip).

    Returns:
        لیست مسیرهای کامل قطعه‌های ساخته‌شده.
    """
    part_paths = []
    part_size = settings.ZIP_PART_SIZE
    output_dir = os.path.dirname(file_path) or '.'

    if ext == '.zip':
        name_pattern = f"{prefix}.zip.{{:03d}}"
    else:
        # برای فایل‌های غیر zip : prefix.part001.ext
        name_pattern = f"{prefix}.part{{:03d}}{ext}"

    with open(file_path, 'rb') as f:
        part_num = 1
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            part_name = name_pattern.format(part_num)
            part_path = os.path.join(output_dir, part_name)
            with open(part_path, 'wb') as part_file:
                part_file.write(chunk)
            part_paths.append(part_path)
            part_num += 1

    return part_paths


def merge_file_parts(parts: List[str], output_path: str) -> None:
    """
    الحاق قطعات یک فایل و ساخت فایل نهایی.

    Args:
        parts: لیست مسیر قطعات به ترتیب.
        output_path: مسیر فایل خروجی.
    """
    with open(output_path, 'wb') as out_f:
        for part_path in parts:
            with open(part_path, 'rb') as in_f:
                out_f.write(in_f.read())


def split_video_by_size(video_path: str, max_size_bytes: int = 19 * 1024 * 1024) -> List[str]:
    """
    تقسیم ویدئو به قطعات قابل پخش MP4 با حداکثر حجم مشخص.
    از ffprobe برای محاسبهٔ مدت زمان و از ffmpeg برای تقسیم استفاده می‌کند.
    خروجی در همان پوشهٔ ویدیوی اصلی قرار می‌گیرد.
    """
    logger = get_logger('utils.split_video_by_size')
    if not os.path.exists(video_path):
        logger.error(f"فایل ویدئو یافت نشد: {video_path}")
        return []

    output_dir = os.path.dirname(video_path) or '.'
    total_size = os.path.getsize(video_path)
    if total_size <= max_size_bytes:
        dest = os.path.join(output_dir, os.path.basename(video_path))
        shutil.copy2(video_path, dest)
        return [dest]

    # دریافت مدت زمان با ffprobe
    ffprobe_cmd = [
        settings.FFMPEG_PATH.replace("ffmpeg", "ffprobe") if "ffmpeg" in settings.FFMPEG_PATH else "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        video_path
    ]
    try:
        result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, check=True)
        total_duration = float(result.stdout.strip())
        if total_duration <= 0:
            raise ValueError("مدت زمان نامعتبر")
    except Exception as e:
        logger.error(f"دریافت مدت زمان ویدئو با ffprobe ناموفق بود: {e}")
        return []

    num_chunks = math.ceil(total_size / max_size_bytes)
    chunk_duration = total_duration / num_chunks

    output_pattern = os.path.join(output_dir, "chunk_%03d.mp4")
    cmd = [
        settings.FFMPEG_PATH,
        "-y", "-i", video_path,
        "-c", "copy", "-map", "0",
        "-f", "segment",
        "-segment_time", str(chunk_duration),
        "-reset_timestamps", "1",
        output_pattern
    ]
    logger.info(f"FFmpeg split by size: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg split by size با خطا مواجه شد: {e.stderr}")
        return []

    chunk_files = []
    for fname in sorted(os.listdir(output_dir)):
        if re.match(r'^chunk_\d{3}\.mp4$', fname):
            chunk_files.append(os.path.join(output_dir, fname))
    logger.info(f"{len(chunk_files)} قطعه ویدئو با محدودیت حجم ایجاد شد.")
    return chunk_files


def upload_manager(parts: List[str], chat_id: int, send_func: callable, state_file: str,
                   max_retries: int = 3) -> bool:
    """
    مدیریت ارسال قطعات فایل به ترتیب با قابلیت ذخیره و بازیابی وضعیت.
    """
    state = load_json(state_file, {"sent": []})
    sent = set(state.get("sent", []))
    for idx, part_path in enumerate(parts):
        if part_path in sent:
            continue
        for attempt in range(max_retries):
            try:
                send_func(chat_id, part_path, f"بخش {idx+1}/{len(parts)}")
                sent.add(part_path)
                save_json(state_file, {"sent": list(sent)})
                break
            except Exception:
                time.sleep(2 * (attempt + 1))
        else:
            get_logger("upload_manager").error(f"ارسال {part_path} ناموفق ماند.")
            return False
    return True


# ──────────────────────────── اعتبارسنجی URL و استخراج ID ──────────────────

def is_valid_url(url: str) -> bool:
    """بررسی آغاز URL با http:// یا https://"""
    return url.startswith(('http://', 'https://'))


def extract_video_id(url: str) -> Optional[str]:
    """
    استخراج شناسه‌ی ویدیو از URL یوتیوب.

    از الگوهای رایج مانند watch?v=، youtu.be/، embed/، v/ پشتیبانی می‌کند.
    """
    if not url:
        return None
    # الگوی استاندارد
    pattern = r'(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&/#]|$)'
    # بررسی مستقیم youtu.be
    parsed = urlparse(url)
    if parsed.netloc in ('youtu.be', 'www.youtu.be'):
        vid = parsed.path.lstrip('/')
        if re.match(r'^[0-9A-Za-z_-]{11}$', vid):
            return vid
    # جستجو با regex
    match = re.search(pattern, url)
    return match.group(1) if match else None


def get_filename_from_url(url: str, default: str = "video") -> str:
    """
    استخراج نام فایل از انتهای مسیر URL.

    Args:
        url: آدرس فایل.
        default: نام پیش‌فرض در صورت نامعتبر بودن.

    Returns:
        نام فایل استخراج‌شده یا پیش‌فرض.
    """
    try:
        path = urlparse(url).path
        name = os.path.basename(unquote(path))
        if not name or '.' not in name:
            return default
        return name
    except Exception:
        return default


# ──────────────────────────── دانلود با requests ──────────────────────────

def download_file(url: str, save_dir: str, filename: Optional[str] = None,
                  timeout: int = 60) -> Optional[str]:
    """
    دانلود فایل از اینترنت و ذخیره در پوشه‌ی داده‌شده.

    Args:
        url: آدرس فایل.
        save_dir: پوشه‌ی مقصد.
        filename: نام فایل ذخیره (در صورت نبود از URL استخراج می‌شود).
        timeout: تایم‌اوت دانلود.

    Returns:
        مسیر کامل فایل ذخیره‌شده یا None در صورت شکست.
    """
    log = get_logger('download_file')
    os.makedirs(save_dir, exist_ok=True)

    if not filename:
        filename = get_filename_from_url(url, default='downloaded_file')
    # جلوگیری از بازنویسی فایل موجود
    base, ext = os.path.splitext(filename)
    counter = 1
    dest_path = os.path.join(save_dir, filename)
    while os.path.exists(dest_path):
        dest_path = os.path.join(save_dir, f"{base}_{counter}{ext}")
        counter += 1

    headers = {'User-Agent': settings.USER_AGENT}
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        log.info(f"دانلود موفق: {url} -> {dest_path}")
        return dest_path
    except Exception as e:
        log.error(f"خطا در دانلود {url}: {e}")
        return None


def safe_request(url: str, timeout: Optional[int] = None) -> Optional[requests.Response]:
    """
    درخواست GET با دو بار تلاش و مدیریت خطا.

    Args:
        url: آدرس درخواست.
        timeout: تایم‌اوت (در صورت None از settings.REQUEST_TIMEOUT استفاده می‌شود).

    Returns:
        شیء Response یا None.
    """
    log = get_logger('safe_request')
    timeout = timeout or settings.REQUEST_TIMEOUT
    headers = {'User-Agent': settings.USER_AGENT}
    retries = 2
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp
        except Exception as e:
            log.warning(f"تلاش {attempt}/{retries} برای {url} شکست خورد: {e}")
            time.sleep(1)
    log.error(f"تمام تلاش‌ها برای {url} شکست خورد.")
    return None


# ──────────────────────────── ابزارهای JSON ──────────────────────────────

def load_json(file_path: str, default: Optional[Dict] = None) -> Dict:
    """
    بارگذاری محتوای فایل JSON.

    Args:
        file_path: مسیر فایل.
        default: مقدار پیش‌فرض در صورت نبودن فایل یا خطا.

    Returns:
        دیکشنری حاصل.
    """
    log = get_logger('load_json')
    if default is None:
        default = {}
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        log.error(f"خطا در خواندن JSON از {file_path}: {e}")
        return default
    except Exception as e:
        log.error(f"خطای غیرمنتظره {file_path}: {e}")
        return default


def save_json(file_path: str, data: Dict) -> None:
    """
    ذخیره دیکشنری در فایل JSON به صورت اتمیک و غیرهمزمان.
    در صورت هنگ کردن فایل‌سیستم، عملیات حداکثر ۵ ثانیه صبر می‌کند.
    """
    log = get_logger('save_json')
    temp_path = file_path + '.tmp'
    try:
        os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # اجرای os.replace در یک ترد جداگانه با timeout
        def replace_operation():
            os.replace(temp_path, file_path)

        t = threading.Thread(target=replace_operation, daemon=True)
        t.start()
        t.join(timeout=5)

        if t.is_alive():
            log.warning(f"ذخیره JSON برای {file_path} بیش از ۵ ثانیه طول کشید - نادیده گرفته شد")
        else:
            # در صورت موفقیت، t.is_alive() == False
            pass  # موفقیت در لاگ ذخیره نمی‌شود؛ می‌توان اضافه کرد
    except Exception as e:
        log.error(f"خطا در ذخیره JSON در {file_path}: {e}")


def safe_remove(file_path: str) -> None:
    """حذف یک فایل در صورت وجود بدون ایجاد خطا."""
    log = get_logger('safe_remove')
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        log.error(f"خطا در حذف فایل {file_path}: {e}")
