"""
utils.py - توابع کمکی و ابزارهای عمومی ربات یوتیوب Bale Ultimate
شامل لاگینگ ضد کرش، تقسیم فایل، تقسیم ویدئو با FFmpeg (زمانی و حجمی)،
مدیریت آپلود هوشمند با ذخیره وضعیت، دانلود فایل و غیره.
نسخه بازنگری‌شده: افزوده‌شدن split_video_by_size
"""

import logging
import os
import json
import time
import subprocess
import re
import shutil
import math                               # برای محاسبات تقسیم حجمی
from urllib.parse import unquote, urlparse
from typing import Optional, List, Dict, Any, Callable

import requests

import settings

# ═══════════════════════════════════════════════════════════
# کلاس Handler برای لاگینگ ضد کرش (فلش اجباری بعد از هر ثبت)
# ═══════════════════════════════════════════════════════════
class FlushFileHandler(logging.FileHandler):
    """FileHandler که بعد از هر emit دستور flush() را اجرا می‌کند."""
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()

# ═══════════════════════════════════════════════════════════
# تنظیمات لاگینگ
# ═══════════════════════════════════════════════════════════
_logger_initialized = False

def setup_logging() -> None:
    """تنظیم لاگر اصلی با قالب استاندارد و دو خروجی (فایل و کنسول)."""
    global _logger_initialized
    if _logger_initialized:
        return

    root_logger = logging.getLogger('youtube_bot')
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # فایل لاگ (ضد کرش)
    fh = FlushFileHandler(settings.LOG_FILE, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    root_logger.addHandler(fh)

    # کنسول
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    root_logger.addHandler(ch)

    root_logger.propagate = False
    _logger_initialized = True

def get_logger(name: str) -> logging.Logger:
    """برگرداندن یک logger زیرمجموعه 'youtube_bot'. در صورت نیاز، setup_logging را صدا می‌زند."""
    if not _logger_initialized or not logging.getLogger('youtube_bot').handlers:
        setup_logging()
    return logging.getLogger(f'youtube_bot.{name}')

# ═══════════════════════════════════════════════════════════
# تقسیم فایل به روش باینری (برای ZIP و ...)
# ═══════════════════════════════════════════════════════════
def split_file_binary(file_path: str, prefix: str, ext: str) -> List[str]:
    """
    یک فایل را به چند بخش با اندازه ZIP_PART_SIZE تقسیم می‌کند.
    نام بخش‌ها: prefix.zip.001 و ... یا prefix.part001{ext} ...
    """
    logger = get_logger('utils.split_binary')
    if not os.path.exists(file_path):
        logger.error(f"فایل برای تقسیم وجود ندارد: {file_path}")
        return []

    dir_name = os.path.dirname(file_path)
    chunks = []
    try:
        with open(file_path, 'rb') as f:
            part_num = 0
            while True:
                chunk = f.read(settings.ZIP_PART_SIZE)
                if not chunk:
                    break
                part_num += 1
                if ext == '.zip':
                    chunk_name = f"{prefix}.zip.{part_num:03d}"
                else:
                    chunk_name = f"{prefix}.part{part_num:03d}{ext}"
                chunk_path = os.path.join(dir_name, chunk_name)
                with open(chunk_path, 'wb') as cf:
                    cf.write(chunk)
                chunks.append(chunk_path)
                logger.debug(f"بخش {part_num} ایجاد شد: {chunk_path}")
        logger.info(f"فایل {file_path} به {len(chunks)} بخش تقسیم شد.")
    except Exception as e:
        logger.exception(f"خطا در تقسیم فایل {file_path}: {e}")
        return []
    return chunks

# ═══════════════════════════════════════════════════════════
# تقسیم ویدئو به بخش‌های قابل پخش با FFmpeg (زمانی)
# ═══════════════════════════════════════════════════════════
def split_video_playable(video_path: str, output_dir: str, segment_duration: int = 60) -> List[str]:
    """
    با استفاده از FFmpeg ویدئو را به قطعات MP4 قابل پخش تقسیم می‌کند (بر اساس زمان).
    هر بخش یک فایل ویدئویی مستقل است.
    """
    logger = get_logger('utils.split_video_playable')
    if not os.path.exists(video_path):
        logger.error(f"فایل ویدئو یافت نشد: {video_path}")
        return []

    os.makedirs(output_dir, exist_ok=True)

    # الگوی نام برای خروجی
    output_pattern = os.path.join(output_dir, "chunk_%03d.mp4")

    cmd = [
        settings.FFMPEG_PATH,
        '-y',                                   # overwrite output files
        '-i', video_path,
        '-c', 'copy',                           # stream copy (بدون رمزگذاری مجدد)
        '-map', '0',                            # همه stream ها
        '-f', 'segment',
        '-segment_time', str(segment_duration),
        '-reset_timestamps', '1',
        output_pattern
    ]

    logger.info(f"در حال تقسیم ویدئو (زمانی): {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg با خطا مواجه شد (کد {e.returncode}): {e.stderr}")
        return []

    # پیدا کردن همه فایل‌های chunk تولید شده
    chunk_files = []
    for fname in sorted(os.listdir(output_dir)):
        if re.match(r'^chunk_\d{3}\.mp4$', fname):
            chunk_files.append(os.path.join(output_dir, fname))
    if not chunk_files:
        logger.warning("FFmpeg هیچ قطعه‌ای تولید نکرد.")
    else:
        logger.info(f"{len(chunk_files)} قطعه ویدئو در {output_dir} ایجاد شد.")
    return chunk_files

# ═══════════════════════════════════════════════════════════
# تقسیم ویدئو به قطعات قابل پخش با محدودیت حجم (split_video_by_size)
# ═══════════════════════════════════════════════════════════
def split_video_by_size(
    video_path: str,
    output_dir: str,
    max_size_bytes: int = 19 * 1024 * 1024
) -> List[str]:
    """
    تقسیم یک فایل ویدئو به قطعات قابل پخش MP4 که حجم هرکدام حداکثر max_size_bytes باشد.
    از روش تقسیم زمانی تقریبی (با فرض توزیع یکنواخت بیت‌ریت) استفاده می‌کند.

    Args:
        video_path: مسیر فایل ویدئوی اصلی.
        output_dir: دایرکتوری خروجی برای قطعات.
        max_size_bytes: حداکثر حجم مجاز برای هر قطعه (پیش‌فرض 19 مگابایت).

    Returns:
        لیست مرتب‌شده از مسیرهای کامل قطعات تولیدشده. در صورت خطا یا عدم موفقیت، لیست خالی.
    """
    logger = get_logger('utils.split_video_by_size')
    if not os.path.exists(video_path):
        logger.error(f"فایل ویدئو یافت نشد: {video_path}")
        return []

    os.makedirs(output_dir, exist_ok=True)

    total_size = os.path.getsize(video_path)
    if total_size <= max_size_bytes:
        # فایل کوچک‌تر از حد مجاز - فقط کپی کن
        dest = os.path.join(output_dir, os.path.basename(video_path))
        shutil.copy2(video_path, dest)
        logger.info(f"ویدئو بدون تقسیم (حجم {total_size} ≤ {max_size_bytes}) کپی شد: {dest}")
        return [dest]

    # دریافت مدت زمان ویدئو با ffprobe
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

    # محاسبه تعداد قطعات و مدت زمان تقریبی هر کدام
    num_chunks = math.ceil(total_size / max_size_bytes)
    chunk_duration = total_duration / num_chunks
    logger.info(f"تقسیم ویدئو به {num_chunks} قطعه با مدت زمان ~{chunk_duration:.2f}s هرکدام")

    # اجرای FFmpeg با segment_time
    output_pattern = os.path.join(output_dir, "chunk_%03d.mp4")
    cmd = [
        settings.FFMPEG_PATH,
        "-y",
        "-i", video_path,
        "-c", "copy",
        "-map", "0",
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

    # جمع‌آوری قطعات تولیدی
    chunk_files = []
    for fname in sorted(os.listdir(output_dir)):
        if re.match(r'^chunk_\d{3}\.mp4$', fname):
            chunk_files.append(os.path.join(output_dir, fname))
    if not chunk_files:
        logger.warning("split_video_by_size: هیچ قطعه‌ای تولید نشد.")
    else:
        logger.info(f"{len(chunk_files)} قطعه ویدئو با محدودیت حجم ایجاد شد.")
    return chunk_files

# ═══════════════════════════════════════════════════════════
# دانلود فایل از اینترنت (با تلاش مجدد)
# ═══════════════════════════════════════════════════════════
def download_file(url: str, save_dir: str, filename: Optional[str] = None,
                  timeout: int = 120, max_retries: int = 3) -> Optional[str]:
    """
    دانلود فایل از طریق HTTP GET با پشتیبانی از تلاش مجدد.
    در صورت عدم ارائه نام، از مسیر URL استخراج می‌کند.
    """
    logger = get_logger('utils.download_file')
    os.makedirs(save_dir, exist_ok=True)

    headers = {'User-Agent': settings.USER_AGENT}

    # تعیین نام فایل
    if not filename:
        parsed = urlparse(url)
        filename = unquote(os.path.basename(parsed.path))
        if not filename:
            filename = 'downloaded_file'

    # جلوگیری از بازنویسی فایل‌ها
    base, ext = os.path.splitext(filename)
    dest_path = os.path.join(save_dir, filename)
    counter = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(save_dir, f"{base}_{counter}{ext}")
        counter += 1

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"دانلود {url} (تلاش {attempt}/{max_retries})")
            resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
            resp.raise_for_status()

            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"فایل با موفقیت دانلود شد: {dest_path}")
            return dest_path
        except Exception as e:
            logger.warning(f"دانلود شکست خورد: {e}")
            if attempt < max_retries:
                sleep_time = 2 ** attempt
                logger.info(f"تلاش مجدد پس از {sleep_time} ثانیه...")
                time.sleep(sleep_time)
            else:
                logger.error(f"دانلود پس از {max_retries} تلاش ناموفق ماند.")
    return None

# ═══════════════════════════════════════════════════════════
# درخواست امن HTTP
# ═══════════════════════════════════════════════════════════
def safe_request(url: str, timeout: Optional[int] = None) -> Optional[requests.Response]:
    """
    یک درخواست GET ساده با User-Agent و حداکثر 2 تلاش انجام می‌دهد.
    """
    logger = get_logger('utils.safe_request')
    if timeout is None:
        timeout = settings.REQUEST_TIMEOUT

    headers = {'User-Agent': settings.USER_AGENT}
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            logger.warning(f"درخواست ناموفق (تلاش {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(2)
    return None

# ═══════════════════════════════════════════════════════════
# ابزارهای JSON
# ═══════════════════════════════════════════════════════════
def load_json(file_path: str, default=None) -> dict:
    """
    بارگذاری فایل JSON. در صورت عدم وجود یا خرابی، مقدار پیش‌فرض را برمی‌گرداند.
    """
    logger = get_logger('utils.json')
    if default is None:
        default = {}
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.debug(f"فایل JSON بارگذاری شد: {file_path}")
        return data
    except Exception as e:
        logger.warning(f"خطا در بارگذاری JSON از {file_path}: {e}")
        return default

def save_json(file_path: str, data: dict) -> None:
    """
    ذخیره امن JSON با نوشتن در فایل موقت و سپس جایگزینی.
    """
    logger = get_logger('utils.json')
    os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
    temp_path = file_path + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, file_path)  # عملیات اتمیک روی اکثر سیستم‌ها
        logger.debug(f"JSON ذخیره شد: {file_path}")
    except Exception as e:
        logger.error(f"خطا در ذخیره JSON در {file_path}: {e}")

# ═══════════════════════════════════════════════════════════
# استخراج شناسه ویدئو از URL
# ═══════════════════════════════════════════════════════════
def extract_video_id(url: str) -> Optional[str]:
    """
    استخراج YouTube video ID از URL‌های استاندارد.
    بازگشت None در صورت عدم تطبیق.
    """
    pattern = (
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)'
        r'([a-zA-Z0-9_-]{11})'
    )
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return None

# ═══════════════════════════════════════════════════════════
# مدیریت هوشمند آپلود با ذخیره وضعیت و تلاش مجدد
# ═══════════════════════════════════════════════════════════
def upload_manager(file_parts: List[str],
                   chat_id: int,
                   send_func: Callable[[int, str, str], bool],
                   state_file: Optional[str] = None,
                   max_retries: int = 3) -> bool:
    """
    ارسال لیستی از فایل‌ها به کاربر به ترتیب، همراه با تلاش مجدد و ذخیره وضعیت.
    send_func(chat_id, file_path, caption) -> bool
    """
    logger = get_logger('utils.upload_manager')

    # بارگذاری یا ایجاد وضعیت
    state = {}
    if state_file:
        state = load_json(state_file, {})
        if 'parts' not in state:
            state['parts'] = []

    # تطبیق فایل‌های ورودی با وضعیت (در صورت لزوم)
    if state and state['parts']:
        logger.info(f"وضعیت آپلود قبلی بارگذاری شد: {len(state['parts'])} بخش.")
    else:
        # ایجاد entries جدید
        state['parts'] = [{'path': p, 'sent': False, 'retries': 0} for p in file_parts]
        if state_file:
            save_json(state_file, state)

    total = len(state['parts'])
    for idx, part_info in enumerate(state['parts']):
        if part_info['sent']:
            logger.info(f"بخش {idx+1}/{total} قبلاً ارسال شده، رد می‌شود.")
            continue

        file_path = part_info['path']
        if not os.path.exists(file_path):
            logger.warning(f"فایل بخش وجود ندارد: {file_path}. علامت‌گذاری به عنوان ارسال‌شده.")
            part_info['sent'] = True
            if state_file:
                save_json(state_file, state)
            continue

        caption = f"📦 بخش {idx+1} از {total}"
        success = False
        while part_info['retries'] < max_retries:
            try:
                logger.debug(f"تلاش ارسال بخش {idx+1} (تلاش {part_info['retries']+1})")
                ok = send_func(chat_id, file_path, caption)
                if ok:
                    logger.info(f"بخش {idx+1} با موفقیت ارسال شد.")
                    success = True
                    break
                else:
                    logger.warning(f"ارسال بخش {idx+1} با شکست روبرو شد (بازگشت False).")
            except Exception as e:
                logger.error(f"خطا در ارسال بخش {idx+1}: {e}")

            part_info['retries'] += 1
            if state_file:
                save_json(state_file, state)
            if part_info['retries'] < max_retries:
                time.sleep(3)  # مکث قبل از تلاش مجدد

        if success:
            part_info['sent'] = True
            # تلاش برای حذف فایل
            safe_remove(file_path)
            if state_file:
                save_json(state_file, state)
        else:
            logger.critical(f"بخش {idx+1} پس از {max_retries} تلاش ارسال نشد. آپلود ناقص ماند.")
            return False

    logger.info("همه بخش‌ها با موفقیت ارسال شدند.")
    # پاک کردن فایل وضعیت
    if state_file and os.path.exists(state_file):
        try:
            os.remove(state_file)
        except Exception:
            pass
    return True

# ═══════════════════════════════════════════════════════════
# حذف ایمن فایل
# ═══════════════════════════════════════════════════════════
def safe_remove(file_path: str) -> None:
    """حذف فایل در صورت وجود، بدون ایجاد خطا."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        get_logger('utils.safe_remove').warning(f"حذف فایل {file_path} ناموفق: {e}")
