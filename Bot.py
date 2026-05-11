"""
bot.py - YouTube Bale Bot v4.3
Complete rewrite with VIP, screenshot, video/photo modes, search modes, and more.
"""

import os, re, sys, time, json, math, uuid, shutil, threading, subprocess, traceback, logging
from typing import Any, Dict, List, Optional, Tuple
import requests, scrapetube
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import concurrent.futures

# ---------- Constants ----------
BOT_TOKEN = os.getenv("BALE_BOT_TOKEN")
if not BOT_TOKEN:
    print("Error: BALE_BOT_TOKEN not set")
    sys.exit(1)
API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))
DEBUG = os.getenv("DEBUG", "0") == "1"

MAX_SEND_SIZE = 20 * 1024 * 1024       # 20 MB
QUOTA_BYTES = 3 * 1024 ** 3            # 3 GB
QUOTA_SECONDS = 6 * 3600              # 6 hours
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
SEARCH_TIMEOUT = 90
WATCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180
QUOTA_THRESHOLD = 500 * 1024 * 1024    # 500 MB – minimum remaining quota to allow download
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
VIP_CODES = ["XK7G", "P2MQ", "Z9WN", "R4TJ", "Y6VL"]
VIP_HOURS = 6

BITRATE_TABLE = {
    "360p": 0.5e6,
    "480p": 1e6,
    "720p": 2e6,
    "1080p": 4e6,
    "4K": 12e6
}

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("youtube_bot")

# ---------- Thread-safe globals ----------
state_lock = threading.Lock()
users_lock = threading.Lock()
vip_lock = threading.Lock()

SEARCH_RESULTS: Dict[int, Dict[int, str]] = {}
USER_STATE: Dict[int, str] = {}
PENDING_DOWNLOADS: Dict[int, str] = {}     # chat_id -> video_id for confirmation

# ---------- Helpers ----------
def load_json(path: str, default: Optional[Dict] = None) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def save_json(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def safe_remove(file_path: str) -> None:
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass

def extract_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    parsed = requests.utils.urlparse(url)
    if parsed.netloc in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/")
        if re.match(r"^[0-9A-Za-z_-]{11}$", vid):
            return vid
    m = re.search(r"(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&/#]|$)", url)
    return m.group(1) if m else None

def download_file(url: str, save_dir: str, filename: Optional[str] = None, timeout: int = 120) -> Optional[str]:
    try:
        os.makedirs(save_dir, exist_ok=True)
        if not filename:
            filename = os.path.basename(requests.utils.urlparse(url).path) or "file"
        base, ext = os.path.splitext(filename)
        dest = os.path.join(save_dir, filename)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(save_dir, f"{base}_{counter}{ext}")
            counter += 1
        with requests.get(url, stream=True, timeout=timeout, headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return dest
    except Exception as e:
        logger.error(f"download_file error: {e}")
        return None

def split_file_binary(file_path: str, prefix: str, ext: str) -> List[str]:
    part_paths = []
    out_dir = os.path.dirname(file_path) or "."
    if ext == ".zip":
        pattern = f"{prefix}.zip.{{:03d}}"
    else:
        pattern = f"{prefix}.part{{:03d}}{ext}"
    with open(file_path, "rb") as f:
        part_num = 1
        while True:
            chunk = f.read(MAX_SEND_SIZE)
            if not chunk:
                break
            p = os.path.join(out_dir, pattern.format(part_num))
            with open(p, "wb") as pf:
                pf.write(chunk)
            part_paths.append(p)
            part_num += 1
    return part_paths

def split_video_by_size(video_path: str, max_size_bytes: int = MAX_SEND_SIZE) -> List[str]:
    if not os.path.exists(video_path):
        return []
    total_size = os.path.getsize(video_path)
    if total_size <= max_size_bytes:
        dest = os.path.join(os.path.dirname(video_path), os.path.basename(video_path))
        shutil.copy2(video_path, dest)
        return [dest]

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        logger.error("ffmpeg/ffprobe not found")
        return []

    out_dir = os.path.dirname(video_path)
    # Method 1: segment_size (newer ffmpeg)
    try:
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_size", str(max_size_bytes),
            "-reset_timestamps", "1",
            os.path.join(out_dir, "chunk_%03d.mp4")
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        parts = sorted([
            os.path.join(out_dir, f) for f in os.listdir(out_dir)
            if re.match(r"chunk_\d{3}\.mp4$", f)
        ])
        if parts and all(os.path.getsize(p) <= max_size_bytes for p in parts):
            return parts
    except subprocess.CalledProcessError:
        logger.info("segment_size failed, falling back to -fs method")

    # Method 2: -fs based segmentation (guarantees max size per chunk)
    try:
        dur_result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True
        )
        duration = float(dur_result.stdout.strip())
        if duration <= 0:
            return []
        start = 0.0
        idx = 1
        parts = []
        while start < duration:
            out_path = os.path.join(out_dir, f"chunk_{idx:03d}.mp4")
            cmd = [
                ffmpeg, "-y", "-ss", str(start), "-i", video_path,
                "-fs", str(max_size_bytes),
                "-c", "copy", "-movflags", "+faststart",
                out_path
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            # get actual duration of output chunk
            chunk_dur_cmd = [
                ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out_path
            ]
            try:
                chunk_dur_output = subprocess.run(chunk_dur_cmd, capture_output=True, text=True, check=True)
                chunk_duration = float(chunk_dur_output.stdout.strip())
                if chunk_duration <= 0:
                    logger.error("Chunk duration <= 0, aborting")
                    return []
            except Exception:
                logger.error("Failed to get chunk duration")
                return []
            parts.append(out_path)
            start += chunk_duration
            idx += 1
        return parts
    except Exception as e:
        logger.error(f"Manual split with -fs failed: {e}")
        return []

def download_thumbnail(video_id: str, save_dir: str) -> Optional[str]:
    base = "https://img.youtube.com/vi/{}/{}"
    for variant in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"):
        url = base.format(video_id, variant)
        try:
            if requests.head(url, timeout=5).status_code == 200:
                return download_file(url, save_dir, f"{video_id}.jpg", timeout=20)
        except Exception:
            continue
    return None

# ---------- Bale API ----------
def send_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"sendMessage error: {e}")
        return None

def send_document(chat_id: int, file_path: str, caption: str = "") -> Optional[Dict]:
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            r = requests.post(f"{API_BASE}/sendDocument", files=files, data=data, timeout=REQUEST_TIMEOUT*4)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"sendDocument error: {e}")
        return None

def sendPhoto(chat_id: int, photo_path: str, caption: str = "") -> Optional[Dict]:
    if not os.path.exists(photo_path):
        return None
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": (os.path.basename(photo_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            r = requests.post(f"{API_BASE}/sendPhoto", files=files, data=data, timeout=REQUEST_TIMEOUT*4)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"sendPhoto error: {e}")
        return None

def sendVideo(chat_id: int, video_path: str, caption: str = "") -> Optional[Dict]:
    if not os.path.exists(video_path):
        return None
    try:
        with open(video_path, "rb") as f:
            files = {"video": (os.path.basename(video_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            r = requests.post(f"{API_BASE}/sendVideo", files=files, data=data, timeout=REQUEST_TIMEOUT*4)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"sendVideo error: {e}")
        return None

def edit_message_text(chat_id: int, message_id: int, text: str,
                      reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API_BASE}/editMessageText", json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"editMessageText error: {e}")
        return None

def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(f"{API_BASE}/answerCallbackQuery",
                      json={"callback_query_id": callback_query_id, "text": text},
                      timeout=REQUEST_TIMEOUT)
    except Exception:
        pass

def get_updates(offset: int, timeout: int) -> Optional[Dict]:
    try:
        r = requests.post(f"{API_BASE}/getUpdates",
                          json={"offset": offset, "timeout": timeout},
                          timeout=timeout + 10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {"ok": True, "result": []}

# ---------- VIP Management ----------
VIP_FILE = "vip_codes.json"

def load_vip_data() -> Dict:
    with vip_lock:
        return load_json(VIP_FILE, {})

def save_vip_data(data: Dict) -> None:
    with vip_lock:
        save_json(VIP_FILE, data)

def is_vip(chat_id: int) -> bool:
    """Check if user has valid VIP access (any code record)."""
    if is_admin(chat_id):
        return True
    data = load_vip_data()
    now = time.time()
    for code, info in data.items():
        if info.get("used_by") == chat_id and now - info.get("used_at", 0) < VIP_HOURS * 3600:
            return True
    return False

def check_vip_code(code: str, chat_id: int) -> Tuple[bool, str]:
    if code not in VIP_CODES:
        return False, "❌ کد VIP نامعتبر است."
    data = load_vip_data()
    now = time.time()
    if code in data:
        used_by = data[code]["used_by"]
        used_at = data[code]["used_at"]
        if used_by != chat_id and (now - used_at) < VIP_HOURS * 3600:
            return False, "❌ این کد در حال حاضر توسط کاربر دیگری استفاده می‌شود."
    # Assign or reassign
    data[code] = {"used_by": chat_id, "used_at": now}
    save_vip_data(data)
    return True, "✅ کد VIP با موفقیت فعال شد."

# ---------- User & Quota Management ----------
USERS_FILE = "users.json"

def load_users() -> Dict:
    with users_lock:
        data = load_json(USERS_FILE, {"admin_id": ADMIN_CHAT_ID, "users": {}})
        data.setdefault("admin_id", ADMIN_CHAT_ID)
        data.setdefault("users", {})
        adm_uid = str(data["admin_id"])
        if adm_uid not in data["users"]:
            data["users"][adm_uid] = {
                "quota_used_bytes": 0,
                "quota_reset_time": 0.0,
                "quality": "720p",
                "send_mode": "playable",
                "preview_mode": "thumbnail",
                "video_mode": "document",
                "photo_mode": "showable",
                "result_count": 10,
                "search_mode": "relevance"
            }
        for u in data["users"].values():
            u.setdefault("quota_used_bytes", 0)
            u.setdefault("quota_reset_time", 0.0)
            u.setdefault("quality", "720p")
            u.setdefault("send_mode", "playable")
            u.setdefault("preview_mode", "thumbnail")
            u.setdefault("video_mode", "document")
            u.setdefault("photo_mode", "showable")
            u.setdefault("result_count", 10)
            u.setdefault("search_mode", "relevance")
    return data

def save_users(data: Dict) -> None:
    with users_lock:
        save_json(USERS_FILE, data)

def is_admin(chat_id: int) -> bool:
    return chat_id == load_users().get("admin_id")

def check_quota_before(chat_id: int, threshold_bytes: int) -> Tuple[bool, str]:
    if is_admin(chat_id):
        return True, ""
    data = load_users()
    uid = str(chat_id)
    if uid not in data["users"]:
        return False, "⛔ دسترسی ندارید."
    user = data["users"][uid]
    now = time.time()
    if now >= user["quota_reset_time"]:
        user["quota_used_bytes"] = 0
        user["quota_reset_time"] = now + QUOTA_SECONDS
    if user["quota_used_bytes"] + threshold_bytes > QUOTA_BYTES:
        from datetime import datetime
        reset_str = datetime.fromtimestamp(user["quota_reset_time"]).strftime("%H:%M")
        return False, f"⛔ حجم مصرفی شما پر شده است. تا ساعت {reset_str} صبر کنید."
    return True, ""

def add_quota_usage(chat_id: int, bytes_used: int) -> None:
    if is_admin(chat_id):
        return
    data = load_users()
    uid = str(chat_id)
    if uid not in data["users"]:
        return
    user = data["users"][uid]
    now = time.time()
    if now >= user["quota_reset_time"]:
        user["quota_used_bytes"] = 0
        user["quota_reset_time"] = now + QUOTA_SECONDS
    user["quota_used_bytes"] += bytes_used
    save_users(data)

def add_user(target_id: int) -> Tuple[bool, str]:
    data = load_users()
    if target_id == data["admin_id"]:
        return False, "⚠️ ادمین نیازی به افزودن ندارد."
    uid = str(target_id)
    if uid in data["users"]:
        return False, "⚠️ کاربر قبلاً اضافه شده است."
    data["users"][uid] = {
        "quota_used_bytes": 0,
        "quota_reset_time": 0.0,
        "quality": "720p",
        "send_mode": "playable",
        "preview_mode": "thumbnail",
        "video_mode": "document",
        "photo_mode": "showable",
        "result_count": 10,
        "search_mode": "relevance"
    }
    save_users(data)
    return True, f"✅ کاربر {target_id} اضافه شد."

def set_admin(caller_id: int, new_admin: int) -> Tuple[bool, str]:
    if not is_admin(caller_id):
        return False, "⛔ فقط ادمین می‌تواند ادمین جدید تعیین کند."
    data = load_users()
    data["admin_id"] = new_admin
    save_users(data)
    return True, f"✅ ادمین به {new_admin} تغییر یافت."

def get_user_settings(chat_id: int) -> Dict:
    data = load_users()
    return data["users"].get(str(chat_id), {
        "quality": "720p", "send_mode": "playable",
        "preview_mode": "thumbnail", "video_mode": "document",
        "photo_mode": "showable", "result_count": 10,
        "search_mode": "relevance"
    })

def update_user_setting(chat_id: int, key: str, value: Any) -> None:
    data = load_users()
    uid = str(chat_id)
    if uid in data["users"]:
        data["users"][uid][key] = value
        save_users(data)

# ---------- Search ----------
def search_youtube(query: str, limit: int = 10) -> List[Dict[str, str]]:
    """Standard search via scrapetube, returns list of dicts with id and maybe title."""
    try:
        videos = scrapetube.get_search(query, limit=limit)
        results = []
        for v in videos:
            vid = v["videoId"]
            title = v.get("title", {}).get("runs", [{}])[0].get("text", "")
            results.append({"video_id": vid, "title": title})
        return results
    except Exception as e:
        logger.error(f"scrapetube error: {e}")
        return []

def _search_newest_playwright(query: str, limit: int) -> List[Dict[str, str]]:
    """Search by newest first using Playwright."""
    return _search_with_sp(query, limit, "CAMS")

def _search_popular_playwright(query: str, limit: int) -> List[Dict[str, str]]:
    """Search by popular using Playwright."""
    return _search_with_sp(query, limit, "CAMSAhAB")

def _search_with_sp(query: str, limit: int, sp: str) -> List[Dict[str, str]]:
    pw = browser = page = None
    try:
        encoded = requests.utils.quote(query)
        url = f"https://www.youtube.com/results?search_query={encoded}&sp={sp}"
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT * 1000)
        page.wait_for_selector("ytd-video-renderer", timeout=15000)
        for _ in range(5):
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            time.sleep(1)
        items = page.evaluate("""
        (limit) => {
            const results = [];
            const els = document.querySelectorAll('ytd-video-renderer');
            for (const el of els) {
                if (results.length >= limit) break;
                const titleEl = el.querySelector('#video-title');
                const title = titleEl ? titleEl.textContent.trim() : '';
                const href = titleEl ? titleEl.closest('a')?.getAttribute('href') : '';
                const vid = href ? href.split('?v=')[1]?.split('&')[0] : '';
                const thumbEl = el.querySelector('img.yt-core-image');
                const thumb = thumbEl ? thumbEl.getAttribute('src') : '';
                if (vid) results.push({ video_id: vid, title, thumbnail: thumb });
            }
            return results;
        }
        """, limit)
        return items[:limit]
    except Exception as e:
        logger.error(f"_search_with_sp failed: {e}")
        return []
    finally:
        if page: page.close()
        if browser: browser.close()
        if pw: pw.stop()

# ---------- Playwright scraping ----------
def scrape_watch_page(video_id: str) -> Optional[Dict[str, Any]]:
    """Multi-layer extraction, returns dict with screenshot_path, no likes."""
    pw = browser = context = page = None
    screenshot_path = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 720, "height": 1280})
        page = context.new_page()
        page.set_viewport_size({"width": 720, "height": 1280})
        page.goto(f"https://www.youtube.com/watch?v={video_id}",
                  wait_until="domcontentloaded", timeout=WATCH_TIMEOUT * 1000)
        page.wait_for_selector("h1 yt-formatted-string", timeout=15000)

        try:
            expand = page.query_selector("#expand, #description-inline-expander button")
            if expand:
                expand.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        data = page.evaluate("""
        () => {
            let title = '', uploader = '', views = '', duration = '', description = '';
            const player = window.ytInitialPlayerResponse;
            if (player && player.videoDetails) {
                title = player.videoDetails.title || '';
                uploader = player.videoDetails.author || '';
                duration = player.videoDetails.lengthSeconds || '';
            }
            const ytData = window.ytInitialData;
            if (ytData) {
                const contents = ytData.contents?.twoColumnWatchNextResults?.results?.results?.contents;
                const primaryInfo = contents?.find(c => c.videoPrimaryInfoRenderer)?.videoPrimaryInfoRenderer;
                const secondaryInfo = contents?.find(c => c.videoSecondaryInfoRenderer)?.videoSecondaryInfoRenderer;
                if (primaryInfo) {
                    const viewCount = primaryInfo.viewCount?.videoViewCountRenderer?.viewCount?.simpleText;
                    if (viewCount) views = viewCount;
                }
                if (!uploader && secondaryInfo) {
                    const owner = secondaryInfo.owner?.videoOwnerRenderer?.title?.runs?.[0]?.text;
                    if (owner) uploader = owner;
                }
            }
            if (!title) {
                const el = document.querySelector('h1 yt-formatted-string');
                title = el ? el.textContent.trim() : '';
            }
            if (!uploader) {
                const el = document.querySelector('#owner yt-formatted-string a, ytd-channel-name a');
                uploader = el ? el.textContent.trim() : '';
            }
            if (!views) {
                const el = document.querySelector('#info .view-count, #count .view-count');
                views = el ? el.textContent.trim() : '';
            }
            if (!duration) {
                const meta = document.querySelector('meta[itemprop="duration"]');
                if (meta) {
                    const iso = meta.getAttribute('content');
                    const match = iso.match(/PT(?:(\\d+)H)?(?:(\\d+)M)?(?:(\\d+)S)?/);
                    if (match) {
                        const h = parseInt(match[1]||0), m = parseInt(match[2]||0), s = parseInt(match[3]||0);
                        duration = (h*3600 + m*60 + s).toString();
                    }
                }
            }
            const descEl = document.querySelector('#description-inline-expander yt-formatted-string, #description yt-formatted-string');
            description = descEl ? descEl.textContent.trim() : '';
            return { title, uploader, views, duration, description };
        }
        """)
        dur = data.get("duration")
        if dur and isinstance(dur, str) and dur.isdigit():
            data["duration"] = int(dur)

        # Screenshot
        try:
            screenshot_path = f"downloads/{uuid.uuid4().hex[:8]}_screenshot.jpg"
            page.screenshot(path=screenshot_path, full_page=True, quality=85)
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")

        return {
            "video_id": video_id,
            "title": data.get("title"),
            "uploader": data.get("uploader"),
            "views": data.get("views"),
            "duration": data.get("duration"),
            "description": data.get("description", ""),
            "screenshot_path": screenshot_path
        }
    except (PlaywrightTimeout, Exception) as e:
        logger.error(f"scrape_watch_page error for {video_id}: {e}")
        return None
    finally:
        if page: page.close()
        if context: context.close()
        if browser: browser.close()
        if pw: pw.stop()

def format_count(raw: str) -> str:
    if not raw:
        return "?"
    num_str = re.sub(r"[^\d]", "", raw)
    if not num_str:
        return raw.strip() or "?"
    try:
        num = int(num_str)
    except ValueError:
        return raw.strip() or "?"
    if num >= 1_000_000:
        formatted = f"{num/1_000_000:.1f}M"
        if formatted.endswith(".0M"):
            formatted = formatted[:-2] + "M"
    elif num >= 1_000:
        formatted = f"{num/1_000:.1f}K"
        if formatted.endswith(".0K"):
            formatted = formatted[:-2] + "K"
    else:
        formatted = str(num)
    return formatted

def seconds_to_display(sec: int) -> str:
    if not isinstance(sec, (int, float)):
        return str(sec)
    sec = int(sec)
    if sec < 60:
        return f"{sec} ثانیه"
    mins = sec // 60
    secs = sec % 60
    if secs == 0:
        return f"{mins} دقیقه"
    return f"{mins} دقیقه {secs} ثانیه"

# ---------- Info Handler ----------
def handle_info(chat_id: int, video_id: str, index: int) -> None:
    send_message(chat_id, "⏳ در حال دریافت اطلاعات ویدیو...")
    info = scrape_watch_page(video_id)
    if not info or not info.get("title"):
        send_message(chat_id, "⛔ دریافت اطلاعات ویدیو ناموفق بود.")
        return

    settings = get_user_settings(chat_id)
    preview_mode = settings.get("preview_mode", "thumbnail")
    photo_mode = settings.get("photo_mode", "showable")

    title = info.get("title", "?")
    uploader = info.get("uploader", "?")
    views = format_count(info.get("views", "?"))
    duration_raw = info.get("duration", "?")
    duration_display = seconds_to_display(duration_raw) if isinstance(duration_raw, (int, float)) else str(duration_raw)
    desc = info.get("description", "")

    caption = f"📹 {title}\n👤 {uploader}\n👁 {views} | ⏱ {duration_display}\n"
    if desc and len(desc) <= 200:
        caption += f"📝 {desc}\n"
    elif desc:
        caption += "📄 توضیحات کامل در فایل بالا.\n"
    caption += f"🔗 https://youtube.com/watch?v={video_id}\n📥 /dl_{index}"

    # Send description file if long
    if desc and len(desc) > 200:
        desc_path = f"downloads/{chat_id}/{video_id}_desc.txt"
        os.makedirs(os.path.dirname(desc_path), exist_ok=True)
        with open(desc_path, "w", encoding="utf-8") as f:
            f.write(desc)
        send_document(chat_id, desc_path, caption="📄 توضیحات کامل")
        safe_remove(desc_path)

    # Send preview
    screenshot_path = info.get("screenshot_path")
    if preview_mode == "screenshot" and screenshot_path and os.path.exists(screenshot_path):
        if photo_mode == "document":
            send_document(chat_id, screenshot_path, caption=caption)
        else:
            sendPhoto(chat_id, screenshot_path, caption=caption)
        safe_remove(screenshot_path)
    else:
        thumb_path = download_thumbnail(video_id, f"downloads/{chat_id}")
        if thumb_path:
            if photo_mode == "document":
                send_document(chat_id, thumb_path, caption=caption)
            else:
                sendPhoto(chat_id, thumb_path, caption=caption)
            safe_remove(thumb_path)
        else:
            send_message(chat_id, caption)   # fallback text only

# ---------- Download & Upload ----------
def _download_hubytconvert(video_id: str, quality: str, save_dir: str) -> Optional[str]:
    try:
        s = requests.Session()
        s.get("https://media.ytmp3.gg/", headers={"User-Agent": USER_AGENT}, timeout=10)
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://media.ytmp3.gg/",
            "Origin": "https://media.ytmp3.gg",
            "Content-Type": "application/json"
        }
        payload = {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "os": "linux",
            "output": {"type": "video", "format": "mp4", "quality": quality}
        }
        r = s.post("https://hub.ytconvert.org/api/download", json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        status_url = data.get("statusUrl")
        if not status_url:
            raise ValueError("No statusUrl")
        start = time.time()
        while time.time() - start < DOWNLOAD_TIMEOUT:
            time.sleep(2)
            sr = s.get(status_url, headers=headers, timeout=10)
            sr.raise_for_status()
            sdata = sr.json()
            if sdata.get("status") == "completed":
                dl_url = sdata.get("downloadUrl")
                if not dl_url:
                    raise ValueError("No downloadUrl")
                return download_file(dl_url, save_dir, f"{video_id}.mp4", timeout=180)
            elif sdata.get("status") == "error":
                logger.error(f"hubytconvert error: {sdata.get('message')}")
                break
        logger.error("hubytconvert timeout")
    except Exception as e:
        logger.warning(f"hubytconvert failed: {e}")
    return None

def _download_cobalt(video_id: str, quality: str, save_dir: str) -> Optional[str]:
    try:
        r = requests.post(
            "https://api.cobalt.tools/api/json",
            json={"url": f"https://youtube.com/watch?v={video_id}", "vCodec": "h264", "aFormat": "mp3"},
            timeout=REQUEST_TIMEOUT * 2
        )
        r.raise_for_status()
        stream_url = r.json().get("url") or r.json().get("streamUrl")
        if stream_url:
            return download_file(stream_url, save_dir, f"{video_id}.mp4", timeout=180)
    except Exception as e:
        logger.warning(f"cobalt failed: {e}")
    return None

def download_video(video_id: str, quality: str, save_dir: str) -> Optional[str]:
    path = _download_hubytconvert(video_id, quality, save_dir)
    if path:
        return path
    return _download_cobalt(video_id, quality, save_dir)

def send_video_parts(parts: List[str], chat_id: int, video_mode: str = "document",
                     send_mode: str = "playable") -> bool:
    total = len(parts)
    i = 0
    slow_mode = False
    slow_counter = 0
    while i < len(parts):
        part = parts[i]
        part_start = time.time()

        # Oversize check
        if os.path.getsize(part) > MAX_SEND_SIZE:
            logger.info(f"Part {i+1} oversized, splitting")
            base, ext = os.path.splitext(part)
            sub_parts = split_file_binary(part, base, ext)
            safe_remove(part)
            parts.pop(i)
            for j, sp in enumerate(sub_parts):
                parts.insert(i + j, sp)
            total = len(parts)
            continue

        send_message(chat_id, f"📤 در حال ارسال پارت {i+1}/{total}...")
        success = False
        for attempt in range(1, 4):
            if time.time() - part_start > 300:
                send_message(chat_id, f"⛔ ارسال پارت {i+1} به‌دلیل اتمام وقت متوقف شد.")
                return False
            try:
                if video_mode == "showable" and send_mode == "playable" and i == 0:
                    res = sendVideo(chat_id, part, caption="")
                else:
                    caption = "ادامه ویدیو..." if i > 0 else ""
                    res = send_document(chat_id, part, caption=caption)
                if res and res.get("ok"):
                    success = True
                    break
            except Exception:
                pass
            if attempt < 3:
                send_message(chat_id, f"🔄 تلاش {attempt+1} برای پارت {i+1} در ۱۰ ثانیه...")
                time.sleep(10)
            else:
                send_message(chat_id, f"⛔ ارسال پارت {i+1} ناموفق ماند. لطفاً دوباره تلاش کنید.")
        if not success:
            return False

        # Timing logic
        elapsed_part = time.time() - part_start
        if elapsed_part > 120:
            slow_mode = True
            slow_counter = 0
        else:
            if slow_mode:
                slow_counter += 1
                if slow_counter >= 3:
                    slow_mode = False
        wait = 10 if slow_mode else 1
        time.sleep(wait)
        i += 1
    return True

def handle_download(chat_id: int, video_id: str, index: int, confirmed: bool = False) -> None:
    if not confirmed:
        pass
    allowed, msg = check_quota_before(chat_id, QUOTA_THRESHOLD)
    if not allowed:
        send_message(chat_id, msg)
        return

    settings = get_user_settings(chat_id)
    quality = settings.get("quality", "720p")
    send_mode = settings.get("send_mode", "playable")
    video_mode = settings.get("video_mode", "document")

    job_id = uuid.uuid4().hex[:8]
    save_dir = f"downloads/{chat_id}/{job_id}"
    os.makedirs(save_dir, exist_ok=True)

    start_time = time.time()
    try:
        send_message(chat_id, "📥 دانلود ویدیو آغاز شد...")
        video_path = download_video(video_id, quality, save_dir)
        if not video_path:
            send_message(chat_id, "❌ دانلود ویدیو ناموفق بود.")
            return

        file_size = os.path.getsize(video_path)
        add_quota_usage(chat_id, file_size)

        if send_mode == "playable":
            parts = split_video_by_size(video_path, MAX_SEND_SIZE)
        else:
            zip_base = os.path.splitext(video_path)[0]
            zip_path = f"{zip_base}.zip"
            shutil.make_archive(zip_base, 'zip', os.path.dirname(video_path), os.path.basename(video_path))
            parts = split_file_binary(zip_path, os.path.splitext(os.path.basename(zip_path))[0], ".zip")
            safe_remove(zip_path)

        if not parts:
            send_message(chat_id, "⛔ خطا در آماده‌سازی فایل برای ارسال.")
            return

        if send_video_parts(parts, chat_id, video_mode, send_mode):
            send_message(chat_id, "✅ ویدیو با موفقیت ارسال شد.")
            end_time = time.time()
            elapsed = end_time - start_time
            title = get_video_title(video_id)
            total_size_mb = file_size / (1024 * 1024)
            report = (
                f"📊 گزارش نهایی:\n"
                f"🎬 {title}\n"
                f"🔗 https://youtube.com/watch?v={video_id}\n"
                f"📏 کیفیت: {quality}\n"
                f"💾 حجم کل: {total_size_mb:.1f} MB\n"
                f"📦 تعداد پارت‌ها: {len(parts)}\n"
                f"⏱ زمان کل: {elapsed:.1f} ثانیه"
            )
            send_message(chat_id, report)
        else:
            send_message(chat_id, "⚠️ ارسال ویدیو با مشکل مواجه شد.")
    except Exception as e:
        logger.exception("download flow error")
        send_message(chat_id, f"⛔ خطایی رخ داد: {str(e)[:200]}")
    finally:
        shutil.rmtree(save_dir, ignore_errors=True)

def get_video_title(video_id: str) -> str:
    try:
        url = f"https://youtube.com/oembed?url=https://youtube.com/watch?v={video_id}&format=json"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get("title", video_id)
    except Exception:
        pass
    return video_id

# ---------- Link Confirm ----------
def handle_link_confirm(chat_id: int, video_id: str) -> None:
    info = scrape_watch_page(video_id)
    if not info:
        info = {"title": get_video_title(video_id), "duration": 0}
    title = info.get("title", "?")
    quality = get_user_settings(chat_id).get("quality", "720p")
    bitrate = BITRATE_TABLE.get(quality, 2e6)
    duration = info.get("duration", 0)
    if isinstance(duration, (int, float)):
        estimated_size = (duration * bitrate) / 8
    else:
        estimated_size = 10 * 1024 * 1024
    size_mb = estimated_size / (1024 * 1024)

    caption = (
        f"📹 {title}\n"
        f"🔗 https://youtube.com/watch?v={video_id}\n"
        f"📏 کیفیت: {quality}\n"
        f"💾 حجم تخمینی: {size_mb:.1f} MB\n\n"
        "آیا دانلود انجام شود؟"
    )
    preview_mode = get_user_settings(chat_id).get("preview_mode", "thumbnail")
    screenshot_path = info.get("screenshot_path")
    if preview_mode == "screenshot" and screenshot_path and os.path.exists(screenshot_path):
        sendPhoto(chat_id, screenshot_path, caption=caption)
        safe_remove(screenshot_path)
    else:
        thumb = download_thumbnail(video_id, f"downloads/{chat_id}")
        if thumb:
            sendPhoto(chat_id, thumb, caption=caption)
            safe_remove(thumb)
        else:
            send_message(chat_id, caption)

    PENDING_DOWNLOADS[chat_id] = video_id
    keyboard = {"inline_keyboard": [
        [{"text": "✅ تأیید دانلود", "callback_data": "confirm_dl"},
         {"text": "❌ لغو", "callback_data": "cancel_dl"}]
    ]}
    send_message(chat_id, "لطفاً انتخاب کنید:", reply_markup=keyboard)

# ---------- Search Command Handler ----------
def handle_search_command(chat_id: int, query: str) -> None:
    settings = get_user_settings(chat_id)
    search_mode = settings.get("search_mode", "relevance")
    limit = settings.get("result_count", 10)

    send_message(chat_id, f"🔎 در حال جستجوی «{query}»...")
    results = []
    if search_mode == "relevance":
        raw = search_youtube(query, limit)
        results = [{"video_id": r["video_id"], "title": r.get("title", ""), "thumbnail": f"https://img.youtube.com/vi/{r['video_id']}/hqdefault.jpg"} for r in raw]
    elif search_mode == "newest":
        results = _search_newest_playwright(query, limit)
    elif search_mode == "popular":
        results = _search_popular_playwright(query, limit)

    if not results:
        send_message(chat_id, "❌ نتیجه‌ای یافت نشد.")
        return

    with state_lock:
        SEARCH_RESULTS[chat_id] = {i: r["video_id"] for i, r in enumerate(results, 1)}

    photo_mode = settings.get("photo_mode", "showable")
    for idx, r in enumerate(results, 1):
        vid = r["video_id"]
        caption = f"🔗 https://youtube.com/watch?v={vid}\n📥 /dl_{idx}\nℹ️ /info_{idx}"
        thumb_path = download_thumbnail(vid, f"downloads/{chat_id}")
        if thumb_path:
            if photo_mode == "document":
                send_document(chat_id, thumb_path, caption=caption)
            else:
                sendPhoto(chat_id, thumb_path, caption=caption)
            safe_remove(thumb_path)
        else:
            send_message(chat_id, caption)

# ---------- Message Processor ----------
def process_message(chat_id: int, text: str) -> None:
    if not is_admin(chat_id) and not is_vip(chat_id):
        if text not in ("/start", "/help", "/vip") and not text.startswith("/vip "):
            send_message(chat_id, "⛔ لطفاً کد VIP را وارد کنید: /vip <code>")
            return

    if not is_admin(chat_id) and str(chat_id) not in load_users().get("users", {}):
        send_message(chat_id, "⛔ دسترسی ندارید.")
        return

    with state_lock:
        state = USER_STATE.pop(chat_id, None)

    if state == "awaiting_query" and not text.startswith("/"):
        handle_search_command(chat_id, text)
        return
    if state == "awaiting_url" and not text.startswith("/"):
        vid = extract_video_id(text)
        if vid:
            handle_link_confirm(chat_id, vid)
        else:
            send_message(chat_id, "❌ لینک یوتیوب نامعتبر است.")
        return

    if text == "/start":
        send_message(chat_id, "🤖 به ربات YouTube Bale خوش آمدید!", reply_markup=main_menu())
    elif text == "/search":
        with state_lock:
            USER_STATE[chat_id] = "awaiting_query"
        send_message(chat_id, "🔍 عبارت جستجو را تایپ کنید:")
    elif text.startswith("/info_"):
        m = re.match(r"/info_(\d+)", text)
        if m:
            idx = int(m.group(1))
            with state_lock:
                vid = (SEARCH_RESULTS.get(chat_id, {})).get(idx)
            if vid:
                threading.Thread(target=handle_info, args=(chat_id, vid, idx), daemon=True).start()
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نامعتبر. مثال: /info_1")
    elif text.startswith("/dl_"):
        m = re.match(r"/dl_(\d+)", text)
        if m:
            idx = int(m.group(1))
            with state_lock:
                vid = (SEARCH_RESULTS.get(chat_id, {})).get(idx)
            if vid:
                threading.Thread(target=handle_download, args=(chat_id, vid, idx), kwargs={"confirmed": True}, daemon=True).start()
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نامعتبر. مثال: /dl_1")
    elif text == "/settings":
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif text == "/help":
        help_text = (
            "📖 راهنمای ربات:\n"
            "/search - جستجوی ویدیو\n"
            "/info_1 - اطلاعات ویدیوی شماره ۱\n"
            "/dl_1 - دانلود ویدیوی شماره ۱\n"
            "/settings - تنظیمات\n"
            "📥 با دکمه «دانلود با لینک» لینک مستقیم بفرستید.\n"
            "📌 ادمین: /add_user شناسه"
        )
        send_message(chat_id, help_text)
    elif text.startswith("/add_user") and is_admin(chat_id):
        parts = text.split()
        if len(parts) == 2:
            try:
                uid = int(parts[1])
                ok, resp = add_user(uid)
                send_message(chat_id, resp)
            except ValueError:
                send_message(chat_id, "❌ شناسه نامعتبر.")
        else:
            send_message(chat_id, "❌ فرمت: /add_user 123456789")
    elif text.startswith("/setadmin") and is_admin(chat_id):
        parts = text.split()
        if len(parts) == 2:
            try:
                new_admin = int(parts[1])
                ok, resp = set_admin(chat_id, new_admin)
                send_message(chat_id, resp)
            except ValueError:
                send_message(chat_id, "❌ شناسه نامعتبر.")
        else:
            send_message(chat_id, "❌ فرمت: /setadmin 123456789")
    elif text.startswith("/vip"):
        parts = text.split()
        if len(parts) == 2:
            code = parts[1].upper()
            ok, msg = check_vip_code(code, chat_id)
            send_message(chat_id, msg)
        else:
            send_message(chat_id, "❌ فرمت: /vip <code>")
    else:
        send_message(chat_id, "⚠️ فرمان نامعتبر. از منوی زیر استفاده کنید:", reply_markup=main_menu())

# ---------- Menus ----------
def main_menu() -> Dict:
    return {"inline_keyboard": [
        [{"text": "🔍 جستجو", "callback_data": "search"},
         {"text": "📥 دانلود با لینک", "callback_data": "download_link"}],
        [{"text": "ℹ️ راهنما", "callback_data": "help"},
         {"text": "⚙️ تنظیمات", "callback_data": "settings"}]
    ]}

def settings_menu(s: Dict) -> Dict:
    preview_text = "تصویر کوچک" if s["preview_mode"] == "thumbnail" else "اسکرین‌شات"
    video_text = "Showable" if s["video_mode"] == "showable" else "Document"
    photo_text = "Showable" if s["photo_mode"] == "showable" else "Document"
    search_text = {"relevance": "مرتبط‌ترین", "newest": "جدیدترین", "popular": "محبوب‌ترین"}.get(s["search_mode"], "مرتبط‌ترین")
    return {"inline_keyboard": [
        [{"text": f"🎬 کیفیت: {s['quality']}", "callback_data": "set_quality"}],
        [{"text": f"📦 حالت ارسال: {'فیلم (قابل پخش)' if s['send_mode']=='playable' else 'زیپ (فشرده)'}", "callback_data": "set_mode"}],
        [{"text": f"🖼️ پیش‌نمایش: {preview_text}", "callback_data": "set_preview"}],
        [{"text": f"🎥 حالت ویدیو: {video_text}", "callback_data": "set_video_mode"}],
        [{"text": f"🖼️ حالت عکس: {photo_text}", "callback_data": "set_photo_mode"}],
        [{"text": f"🔢 نتایج: {s['result_count']}", "callback_data": "set_page_size"}],
        [{"text": f"🔍 نوع جستجو: {search_text}", "callback_data": "set_search_mode"}],
        [{"text": "🔙 بازگشت", "callback_data": "main_menu"}]
    ]}

def quality_keyboard(current: str) -> Dict:
    qualities = ["360p", "480p", "720p", "1080p", "4K"]
    btns = []
    for q in qualities:
        mark = "✅" if q == current else "○"
        btns.append([{"text": f"{mark} {q}", "callback_data": f"quality_{q}"}])
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

def mode_keyboard(current: str) -> Dict:
    return {"inline_keyboard": [
        [{"text": f"{'✅' if current=='playable' else '○'} فیلم (قابل پخش)", "callback_data": "mode_playable"}],
        [{"text": f"{'✅' if current=='zip' else '○'} زیپ (فشرده)", "callback_data": "mode_zip"}],
        [{"text": "🔙 بازگشت", "callback_data": "settings"}]
    ]}

def preview_keyboard(current: str) -> Dict:
    return {"inline_keyboard": [
        [{"text": f"{'✅' if current=='thumbnail' else '○'} تصویر کوچک", "callback_data": "preview_thumbnail"}],
        [{"text": f"{'✅' if current=='screenshot' else '○'} اسکرین‌شات", "callback_data": "preview_screenshot"}],
        [{"text": "🔙 بازگشت", "callback_data": "settings"}]
    ]}

def video_mode_keyboard(current: str) -> Dict:
    return {"inline_keyboard": [
        [{"text": f"{'✅' if current=='document' else '○'} Document", "callback_data": "video_document"}],
        [{"text": f"{'✅' if current=='showable' else '○'} Showable", "callback_data": "video_showable"}],
        [{"text": "🔙 بازگشت", "callback_data": "settings"}]
    ]}

def photo_mode_keyboard(current: str) -> Dict:
    return {"inline_keyboard": [
        [{"text": f"{'✅' if current=='document' else '○'} Document", "callback_data": "photo_document"}],
        [{"text": f"{'✅' if current=='showable' else '○'} Showable", "callback_data": "photo_showable"}],
        [{"text": "🔙 بازگشت", "callback_data": "settings"}]
    ]}

def page_size_keyboard(current: int) -> Dict:
    sizes = [5, 10, 15, 20, 30, 50]
    btns = []
    for s in sizes:
        mark = "✅" if s == current else "○"
        btns.append([{"text": f"{mark} {s}", "callback_data": f"pagesize_{s}"}])
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

def search_mode_keyboard(current: str) -> Dict:
    modes = [("relevance", "مرتبط‌ترین"), ("newest", "جدیدترین"), ("popular", "محبوب‌ترین")]
    btns = []
    for val, label in modes:
        mark = "✅" if val == current else "○"
        btns.append([{"text": f"{mark} {label}", "callback_data": f"search_{val}"}])
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

# ---------- Callback Handler ----------
def process_callback(chat_id: int, data: str, message_id: int, callback_query_id: str) -> None:
    toast_text = ""
    if data == "search":
        with state_lock:
            USER_STATE[chat_id] = "awaiting_query"
        edit_message_text(chat_id, message_id, "🔍 عبارت جستجو را تایپ کنید:")
    elif data == "download_link":
        with state_lock:
            USER_STATE[chat_id] = "awaiting_url"
        edit_message_text(chat_id, message_id, "📥 لینک ویدیوی یوتیوب را بفرستید:")
    elif data == "help":
        edit_message_text(chat_id, message_id, "📖 راهنما: /help")
        process_message(chat_id, "/help")
    elif data == "settings":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_quality":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🎬 کیفیت دانلود:", reply_markup=quality_keyboard(s["quality"]))
    elif data.startswith("quality_"):
        q = data.split("_", 1)[1]
        update_user_setting(chat_id, "quality", q)
        edit_message_text(chat_id, message_id, f"✅ کیفیت روی {q} تنظیم شد.")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
        toast_text = f"کیفیت روی {q} تنظیم شد."
    elif data == "set_mode":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "📦 حالت ارسال:", reply_markup=mode_keyboard(s["send_mode"]))
    elif data.startswith("mode_"):
        m = data.split("_", 1)[1]
        update_user_setting(chat_id, "send_mode", m)
        mode_str = "فیلم (قابل پخش)" if m == "playable" else "زیپ (فشرده)"
        edit_message_text(chat_id, message_id, f"✅ حالت ارسال: {mode_str}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
        toast_text = f"حالت ارسال: {mode_str}"
    elif data == "set_preview":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🖼️ نوع پیش‌نمایش:", reply_markup=preview_keyboard(s["preview_mode"]))
    elif data.startswith("preview_"):
        mode = data.split("_", 1)[1]
        update_user_setting(chat_id, "preview_mode", mode)
        toast_text = f"پیش‌نمایش: {'تصویر کوچک' if mode=='thumbnail' else 'اسکرین‌شات'}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_video_mode":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🎥 حالت ویدیو:", reply_markup=video_mode_keyboard(s["video_mode"]))
    elif data.startswith("video_"):
        mode = data.split("_", 1)[1]
        update_user_setting(chat_id, "video_mode", mode)
        toast_text = f"حالت ویدیو: {mode}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_photo_mode":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🖼️ حالت عکس:", reply_markup=photo_mode_keyboard(s["photo_mode"]))
    elif data.startswith("photo_"):
        mode = data.split("_", 1)[1]
        update_user_setting(chat_id, "photo_mode", mode)
        toast_text = f"حالت عکس: {mode}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_page_size":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🔢 تعداد نتایج:", reply_markup=page_size_keyboard(s["result_count"]))
    elif data.startswith("pagesize_"):
        size = int(data.split("_", 1)[1])
        update_user_setting(chat_id, "result_count", size)
        toast_text = f"تعداد نتایج: {size}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_search_mode":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🔍 نوع جستجو:", reply_markup=search_mode_keyboard(s["search_mode"]))
    elif data.startswith("search_"):
        mode = data.split("_", 1)[1]
        update_user_setting(chat_id, "search_mode", mode)
        mode_str = {"relevance": "مرتبط‌ترین", "newest": "جدیدترین", "popular": "محبوب‌ترین"}[mode]
        toast_text = f"نوع جستجو: {mode_str}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "confirm_dl":
        vid = PENDING_DOWNLOADS.pop(chat_id, None)
        if vid:
            threading.Thread(target=handle_download, args=(chat_id, vid, 0), kwargs={"confirmed": True}, daemon=True).start()
            edit_message_text(chat_id, message_id, "⏳ دانلود تأیید شد. در حال دانلود...")
        else:
            edit_message_text(chat_id, message_id, "⚠️ درخواست نامعتبر.")
    elif data == "cancel_dl":
        PENDING_DOWNLOADS.pop(chat_id, None)
        edit_message_text(chat_id, message_id, "❌ دانلود لغو شد.")
    elif data == "main_menu":
        edit_message_text(chat_id, message_id, "🏠 منوی اصلی:", reply_markup=main_menu())
    answer_callback_query(callback_query_id, toast_text)

# ---------- Main ----------
def main():
    logger.info("YouTube Bale Bot v4.3 started")
    os.makedirs("downloads", exist_ok=True)
    offset = 0
    while True:
        try:
            resp = get_updates(offset, LONG_POLL_TIMEOUT)
            if not resp or not resp.get("ok"):
                time.sleep(2)
                continue
            for upd in resp.get("result", []):
                if "message" in upd and "text" in upd["message"]:
                    msg = upd["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg["text"]
                    threading.Thread(target=process_message, args=(chat_id, text), daemon=True).start()
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    message_id = cq["message"]["message_id"]
                    data = cq.get("data", "")
                    cq_id = cq["id"]
                    threading.Thread(target=process_callback,
                                     args=(chat_id, data, message_id, cq_id), daemon=True).start()
                offset = upd["update_id"] + 1
        except Exception as e:
            logger.exception("Main loop error")
            time.sleep(5)

if __name__ == "__main__":
    main()
