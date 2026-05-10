"""
settings.py – پیکربندی اصلی پروژه YouTube Bale Bot
تعریف تمام ثابت‌ها، مسیرها، متدها، زنجیره‌های fallback و تنظیمات رابط کاربری
"""

import os
import sys

# ──────────────── توکن و API بله ────────────────
BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
if not BOT_TOKEN:
    print("خطا: توکن ربات تنظیم نشده است. متغیر محیطی BALE_BOT_TOKEN را مقداردهی کنید.")
    sys.exit(1)

API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}"

# ──────────────── ثابت‌های شبکه ────────────────
REQUEST_TIMEOUT = 30          # ثانیه، تایم‌اوت درخواست‌های HTTP
LONG_POLL_TIMEOUT = 50        # ثانیه، تایم‌اوت long polling

# ── timeout برای عملیات (ثانیه) ──
SEARCH_TIMEOUT = 25            # حداکثر زمان برای جستجوی مرورگر (Playwright)
INFO_TIMEOUT = 20              # حداکثر زمان برای دریافت اطلاعات ویدیو
CHANNEL_VIDEOS_LIMIT = 50      # حداکثر تعداد ویدیوهای استخراج‌شده از کانال
BROWSER_IDLE_TIMEOUT = 600     # ۱۰ دقیقه - بستن خودکار مرورگرهای باز
MAX_RELATED_DEPTH = 3          # حداکثر عمق برای ویدیوهای مشابه (پیشنهادی)

# ──────────────── محدودیت‌های فایل ────────────────
MAX_SEND_SIZE = 20 * 1024 * 1024          # ۲۰ مگابایت (حداکثر اندازه برای ارسال مستقیم، در غیر این صورت تکه‌تکه می‌شود)
MAX_VIDEO_DURATION = 7200                 # حداکثر مدت ویدیو به ثانیه (۲ ساعت)
ZIP_PART_SIZE = 20 * 1024 * 1024          # اندازه هر تکه هنگام تقسیم فایل (بایت)

# ──────────────── مسیرها ────────────────
DATA_DIR = "data"
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")                       # فایل صف وظایف
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")                       # فایل شناسه ادمین
METHOD_CONFIG_FILE = os.path.join(DATA_DIR, "method_config.json")       # فایل تنظیمات متدها (اولویت‌ها)
LOG_FILE = "log.txt"                                                    # فایل لاگ
DOWNLOADS_DIR = "downloads"                                             # پوشه دانلودهای موقت
DEBUG_DIR = "debug"                                                     # پوشه ذخیرهٔ پاسخ‌های API در حالت دیباگ

# ──────────────── User‑Agent و FFmpeg ────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
FFMPEG_PATH = "ffmpeg"   # فرض بر این است که ffmpeg در PATH سیستم موجود است

# ──────────────── شناسه ادمین پیش‌فرض ────────────────
DEFAULT_ADMIN_CHAT_ID = 123456789   # در صورت نبودن فایل admin.json، این شناسه استفاده می‌شود

# ──────────────── تعریف متدهای جستجو ────────────────
SEARCH_METHODS = {
    "piped": {
        "name": "Piped API",
        "emoji": "🔍",
        "description": "جستجو از طریق Piped (پایدار و رایگان)",
        "requires_key": False,
        "max_results": 20,
    },
    "newpipe": {
        "name": "NewPipe Extractor",
        "emoji": "🔍",
        "description": "کتابخانهٔ NewPipe (اندروید کلاینت)",
        "requires_key": False,
        "max_results": 20,
    },
    "innertube2": {
        "name": "InnerTube API v2",
        "emoji": "🔍",
        "description": "API خصوصی یوتیوب (MohammadKobirShah)",
        "requires_key": False,
        "max_results": 20,
    },
    "scrapetube": {
        "name": "Scrapetube",
        "emoji": "🔍",
        "description": "اسکرپر سبک (فقط شناسه برمی‌گرداند)",
        "requires_key": False,
        "max_results": 10,
    },
    "youtube_search_python": {
        "name": "YouTube Search Python",
        "emoji": "🔍",
        "description": "جستجوی پایتونی (نیاز به پراکسی دارد)",
        "requires_key": False,
        "max_results": 10,
    },
    "py_yt_search": {
        "name": "py-yt-search",
        "emoji": "🔍",
        "description": "کتابخانه AshokShau (جستجو + اطلاعات)",
        "requires_key": False,
        "max_results": 10,
    },
    "duckduckgo": {
        "name": "DuckDuckGo + HTML",
        "emoji": "🔍",
        "description": "جستجوی غیرمستقیم از طریق DuckDuckGo",
        "requires_key": False,
        "max_results": 10,
    },
    "html_parse": {
        "name": "HTML Parse (ytInitialData)",
        "emoji": "🔍",
        "description": "پارس مستقیم صفحهٔ نتایج یوتیوب",
        "requires_key": False,
        "max_results": 10,
    },
}

# ──────────────── تعریف متدهای دریافت اطلاعات ────────────────
INFO_METHODS = {
    "piped": {
        "name": "Piped API",
        "emoji": "📋",
        "description": "اطلاعات کامل ویدیو از Piped",
        "requires_key": False,
    },
    "newpipe": {
        "name": "NewPipe Extractor",
        "emoji": "📋",
        "description": "استخراج اطلاعات با NewPipe",
        "requires_key": False,
    },
    "innertube2": {
        "name": "InnerTube API v2",
        "emoji": "📋",
        "description": "اطلاعات از API خصوصی یوتیوب",
        "requires_key": False,
    },
    "oembed": {
        "name": "oEmbed",
        "emoji": "📋",
        "description": "داده‌های محدود از oEmbed یوتیوب",
        "requires_key": False,
    },
    "yt_dlp": {
        "name": "yt-dlp",
        "emoji": "📋",
        "description": "استخراج کامل با yt-dlp",
        "requires_key": False,
    },
    "py_yt_search": {
        "name": "py-yt-search",
        "emoji": "📋",
        "description": "اطلاعات از کتابخانه AshokShau",
        "requires_key": False,
    },
}

# ──────────────── تعریف متدهای دانلود ویدیو ────────────────
DOWNLOAD_METHODS = {
    "cobalt": {
        "name": "Cobalt.tools",
        "emoji": "📥",
        "description": "دانلودر قدرتمند و رایگان (pybalt)",
        "requires_key": False,
        "max_quality": "1080p",
    },
    "yt_dlp_pot": {
        "name": "yt-dlp + PO Token",
        "emoji": "📥",
        "description": "yt-dlp با توکن Proof of Origin",
        "requires_key": False,
        "max_quality": "4K",
    },
    "newpipe": {
        "name": "NewPipe Extractor",
        "emoji": "📥",
        "description": "استخراج لینک استریم با NewPipe",
        "requires_key": False,
        "max_quality": "1080p",
    },
    "innertube2_stream": {
        "name": "InnerTube Stream",
        "emoji": "📥",
        "description": "لینک مستقیم از InnerTube API",
        "requires_key": False,
        "max_quality": "720p",
    },
    "piped_stream": {
        "name": "Piped Stream",
        "emoji": "📥",
        "description": "لینک استریم از Piped",
        "requires_key": False,
        "max_quality": "1080p",
    },
    "pytube": {
        "name": "PyTube (Fork)",
        "emoji": "📥",
        "description": "آخرین شانس - معمولاً خراب است",
        "requires_key": False,
        "max_quality": "720p",
    },
}

# ──────────────── زنجیره‌های پیش‌فرض (Fallback Chains) ────────────────
DEFAULT_SEARCH_CHAIN = [
    "piped",
    "newpipe",
    "innertube2",
    "scrapetube",
    "youtube_search_python",
    "py_yt_search",
    "duckduckgo",
    "html_parse",
]

DEFAULT_INFO_CHAIN = [
    "piped",
    "newpipe",
    "innertube2",
    "oembed",
    "yt_dlp",
    "py_yt_search",
]

DEFAULT_DOWNLOAD_CHAIN = [
    "cobalt",
    "yt_dlp_pot",
    "newpipe",
    "innertube2_stream",
    "piped_stream",
    "pytube",
]

# ──────────────── تنظیمات رابط کاربری (UI) ────────────────
UI_SETTINGS = {
    "show_method_in_output": True,       # نمایش نام متد در خروجی
    "show_thumbnails": True,             # ارسال تامنیل همراه نتایج
    "show_download_button": True,        # دکمهٔ دانلود زیر هر ویدیو
    "show_next_method_button": True,     # دکمهٔ «🔁 متد بعدی»
    "result_page_size": 5,               # تعداد نتایج در هر صفحه
    "emoji_enabled": True,               # استفاده از ایموجی
}

# ──────────────── تنظیمات دیباگ ────────────────
DEBUG_MODE = os.getenv("DEBUG", "0") == "1"   # فعال‌سازی لاگ بیشتر و ذخیره پاسخ‌ها
SAVE_RESPONSES = DEBUG_MODE                   # در حالت دیباگ، پاسخ‌های API ذخیره شوند

# ──────────────── کتابخانه‌های مورد نیاز (برای بررسی نصب و import پویا) ────────────────
REQUIRED_LIBS = [
    "requests",
    "yt_dlp",
    "scrapetube",
    "pybalt",              # Cobalt.tools wrapper
    "beautifulsoup4",      # برای پارس HTML (در صورت نیاز)
    # سایر کتابخانه‌ها (innertube, newpipe, ...) اختیاری و در زمان اجرا import می‌شوند
]

# ──────────────── تنظیمات پیش‌فرض session (در method_config.json ذخیره می‌شود) ────────────────
DEFAULT_SESSION_SETTINGS = {
    "search_chain": DEFAULT_SEARCH_CHAIN[:],
    "info_chain": DEFAULT_INFO_CHAIN[:],
    "download_chain": DEFAULT_DOWNLOAD_CHAIN[:],
    "result_page_size": 5,
    "show_thumbnails": True,
}

# ──────────────── تنظیمات پروکسی و PO Token ────────────────
WARP_PROXY_URL = "http://127.0.0.1:8080"        # آدرس پروکسی HTTP که توسط کانتینر wgcf-proxy ایجاد می‌شود
PO_TOKEN_SERVER_URL = "http://127.0.0.1:4416"    # آدرس سرور تولید PO Token (bgutil-ytdlp-pot-provider)
