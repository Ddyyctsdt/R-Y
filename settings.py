"""
settings.py - پیکربندی و ثابت‌های اصلی ربات یوتیوب Bale (نسخه ۳)
توکن، متدهای جستجو/دانلود/اطلاعات، تنظیمات UI، زنجیره‌های fallback و ...
فقط شامل تعریف متغیرها (بدون منطق پیچیده).
"""

import os
import sys

# ──────────────────────────────────────
# توکن ربات و API بیس
# ──────────────────────────────────────
BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BALE_BOT_TOKEN در متغیرهای محیطی یافت نشد.")
    sys.exit(1)

API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}"

# ──────────────────────────────────────
# زمان‌بندی و محدودیت‌های شبکه
# ──────────────────────────────────────
REQUEST_TIMEOUT = 30                # ثانیه - زمان انتظار برای درخواست‌های معمولی
LONG_POLL_TIMEOUT = 50             # ثانیه - زمان انتظار long‑polling

# ──────────────────────────────────────
# محدودیت‌های اندازه فایل
# ──────────────────────────────────────
MAX_SEND_SIZE = 20 * 1024 * 1024   # 20 مگابایت – حداکثر اندازه ارسال مستقیم در بله
ZIP_PART_SIZE = 20 * 1024 * 1024   # 20 مگابایت – اندازه هر بخش در حالت ZIP
MAX_VIDEO_DURATION = 7200          # ثانیه - حداکثر مدت ویدئو (۲ ساعت)
MAX_DOWNLOAD_RETRIES = 3           # تعداد تلاش مجدد برای هر بخش در آپلود ناموفق
MAX_CHUNK_SIZE = 19 * 1024 * 1024  # 19 مگابایت – اندازه هدف برای قطعات قابل پخش (اسپلیت حجمی)

# ──────────────────────────────────────
# مسیرها و دایرکتوری‌ها
# ──────────────────────────────────────
DATA_DIR = "data"
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")
METHOD_CONFIG_FILE = os.path.join(DATA_DIR, "method_config.json")
UPLOAD_STATE_FILE = os.path.join(DATA_DIR, "upload_state.json")
LOG_FILE = "bot.log"
DOWNLOADS_DIR = "downloads"
DEBUG_DIR = "debug"

# ──────────────────────────────────────
# User-Agent و FFmpeg
# ──────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
FFMPEG_PATH = "ffmpeg"   # فرض بر این است که در PATH سیستم موجود باشد

# ──────────────────────────────────────
# ادمین
# ──────────────────────────────────────
DEFAULT_ADMIN_CHAT_ID = 46829437   # در صورت نبود admin.json استفاده می‌شود

# ──────────────────────────────────────
# تعریف متدهای جستجو (فقط Scrapetube)
# ──────────────────────────────────────
SEARCH_METHODS = {
    "scrapetube": {
        "name": "Scrapetube",
        "emoji": "🔎",
        "description": "اسکرپر سبک (فقط شناسه)",
        "requires_key": False,
        "max_results": 10,
        "enabled": True
    }
}

# ──────────────────────────────────────
# تعریف متدهای دانلود
# ──────────────────────────────────────
DOWNLOAD_METHODS = {
    "hubytconvert": {
        "name": "hub.ytconvert.org",
        "emoji": "📥",
        "description": "API امن و تست‌شده (پروکسی واسط)",
        "requires_key": False,
        "max_quality": "1080p",
        "enabled": True
    },
    "cobalt": {
        "name": "Cobalt.tools",
        "emoji": "📥",
        "description": "دانلودر چندپلتفرمه (یوتیوب، تیک‌تاک و ...)",
        "requires_key": False,
        "max_quality": "1080p",
        "enabled": True
    },
    "allmedia": {
        "name": "AllMedia Downloader",
        "emoji": "📥",
        "description": "API رایگان چندپلتفرمه",
        "requires_key": False,
        "max_quality": "1080p",
        "enabled": True
    }
}

# ──────────────────────────────────────
# تعریف متدهای اطلاعات ویدئو (فقط oEmbed)
# ──────────────────────────────────────
INFO_METHODS = {
    "oembed": {
        "name": "YouTube oEmbed",
        "emoji": "ℹ️",
        "description": "اطلاعات پایه (عنوان، آپلودر، تامنیل)",
        "requires_key": False,
        "max_results": 1,
        "enabled": True
    }
}

# ──────────────────────────────────────
# متدهای تکمیل اطلاعات (Enrichment) برای حالت browser
# ──────────────────────────────────────
ENRICHMENT_METHODS = {
    "dom_search_page": {
        "name": "DOM Search Page",
        "description": "استخراج مستقیم از صفحه جستجو",
        "enabled": True
    },
    "oembed_enrich": {
        "name": "oEmbed Enrichment",
        "description": "اطلاعات سریع و سبک (عنوان، تامنیل)",
        "enabled": True
    },
    "dom_watch_page": {
        "name": "Watch Page DOM",
        "description": "باز کردن صفحه ویدیو (عمیق - دیدئو، لایک، توضیحات)",
        "enabled": True
    },
    "json_ld": {
        "name": "JSON-LD Parser",
        "description": "استخراج structured data از صفحه",
        "enabled": True
    }
}

# ──────────────────────────────────────
# زنجیره‌های پیش‌فرض (ترتیب اولویت)
# ──────────────────────────────────────
DEFAULT_SEARCH_CHAIN = ["scrapetube"]          # در حالت API فقط این
DEFAULT_INFO_CHAIN = ["oembed"]
DEFAULT_DOWNLOAD_CHAIN = ["hubytconvert", "cobalt", "allmedia"]

# ──────────────────────────────────────
# تنظیمات ظاهری و کاربری UI
# ──────────────────────────────────────
UI_SETTINGS = {
    "show_method_in_output": True,        # نمایش نام متد موفق در خروجی
    "show_thumbnails": True,              # ارسال تصویر بند‌انگشتی
    "show_download_button": True,         # دکمه دانلود در نتایج
    "show_next_method_button": True,      # دکمه «متد بعدی»
    "result_page_size": 5,               # تعداد نتایج در هر صفحه (پیش‌فرض)
    "emoji_enabled": True,
    "download_quality": "720p",          # کیفیت پیش‌فرض دانلود
    "search_mode": "browser",            # "browser" یا "api" – حالت جستجو
}

# ──────────────────────────────────────
# تنظیمات پیش‌فرض نشست (ذخیره در method_config.json)
# ──────────────────────────────────────
DEFAULT_SESSION_SETTINGS = {
    "search_chain": DEFAULT_SEARCH_CHAIN[:],
    "info_chain": DEFAULT_INFO_CHAIN[:],
    "download_chain": DEFAULT_DOWNLOAD_CHAIN[:],
    "enabled_search_methods": list(SEARCH_METHODS.keys()),
    "enabled_download_methods": list(DOWNLOAD_METHODS.keys()),
    "upload_mode": "playable_chunks",
    "chunk_duration_seconds": 60,            # فقط در صورت نیاز به اسپلیت زمانی
    "result_page_size": 5,
    "download_quality": "720p",
    "search_mode": "browser",               # "browser" یا "api"
    "enabled_enrichment_methods": list(ENRICHMENT_METHODS.keys())
}

# ──────────────────────────────────────
# حالت‌های آپلود
# ──────────────────────────────────────
UPLOAD_MODES = {
    "zip": {
        "description": "فشرده‌سازی کل ویدئو به ZIP و تقسیم به بخش‌های 20 مگابایتی"
    },
    "playable_chunks": {
        "description": "تقسیم ویدئو به قطعات قابل پخش با حجم حداکثر 19 مگابایت"
    }
}

# ──────────────────────────────────────
# کتابخانه‌های مورد نیاز (جهت اطلاع)
# ──────────────────────────────────────
REQUIRED_LIBS = [
    "requests",
    "scrapetube",
    "beautifulsoup4",
    "playwright",   # برای جستجوی browser‑based
    # cobalt و allmedia با HTTP خام فراخوانی می‌شوند
]

# ──────────────────────────────────────
# حالت دیباگ
# ──────────────────────────────────────
DEBUG_MODE = os.environ.get("DEBUG", "0") == "1"
