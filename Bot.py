"""
bot.py - YouTube Bale Bot v5.0
Advanced multi-method search, info, download, channel support, pagination, cookies,
keyframe-aware video splitting, quality profiles, VIP auto-registration, admin panel,
and silent channel handling.
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

MAX_SEND_SIZE = 20 * 1024 * 1024
QUOTA_BYTES = 3 * 1024 ** 3
QUOTA_SECONDS = 6 * 3600
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
SEARCH_TIMEOUT = 90
WATCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 600
QUOTA_THRESHOLD = 500 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
VIP_CODES = ["XK7G", "P2MQ", "Z9WN", "R4TJ", "Y6VL"]
VIP_HOURS = 6

BITRATE_TABLE = {
    "360p": 0.5e6, "480p": 1e6, "720p": 2e6, "1080p": 4e6, "4K": 12e6
}

QUALITY_PROFILES = {
    "360p": {"height": 360, "vcodec": "h264", "max_bitrate": "800k"},
    "480p": {"height": 480, "vcodec": "h264", "max_bitrate": "1.2M"},
    "720p": {"height": 720, "vcodec": "h264", "max_bitrate": "2.5M"},
    "1080p": {"height": 1080, "vcodec": "h264", "max_bitrate": "5M"},
    "4K": {"height": 2160, "vcodec": "vp9", "max_bitrate": "12M"},
}

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("youtube_bot")

# ---------- Thread-safe globals ----------
state_lock = threading.Lock()
users_lock = threading.Lock()
vip_lock = threading.Lock()

SEARCH_STATE: Dict[int, Dict[str, Any]] = {}
USER_STATE: Dict[int, str] = {}
PENDING_DOWNLOADS: Dict[int, str] = {}

# ---------- Helper Functions ----------
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
        if url.startswith("//"):
            url = "https:" + url
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

def split_file_binary(file_path: str, original_filename: Optional[str] = None) -> List[str]:
    """Binary split for zip mode – produces non-playable parts."""
    out_dir = os.path.dirname(file_path) or "."
    if original_filename is None:
        original_filename = os.path.basename(file_path)
    pattern = f"{original_filename}.part{{:03d}}"
    part_paths = []
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

def split_video_by_keyframes(video_path: str, chat_id: int = 0) -> List[str]:
    """
    Keyframe-aware split: produces chunks ≤ 18 MB (with safety margin).
    Reports progress to user if chat_id provided.
    Falls back to sending whole file if keyframe extraction fails.
    """
    TARGET_SIZE = 17 * 1024 * 1024  # 17 MB target, ~18 MB actual
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        if chat_id:
            send_message(chat_id, "⚠️ امکان تقسیم خودکار ویدیو وجود نداشت. فایل کامل ارسال می‌شود.")
        return [video_path]

    # Extract keyframe packets: only lines with ",K" flag
    try:
        cmd = [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,size,flags",
            "-of", "csv=p=0", video_path
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except Exception as e:
        logger.error(f"ffprobe keyframe extraction failed: {e}")
        if chat_id:
            send_message(chat_id, "⚠️ امکان تقسیم خودکار ویدیو وجود نداشت. فایل کامل ارسال می‌شود.")
        return [video_path]

    keyframes = []  # list of (pts_time, size) only keyframes
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split(",")
        if len(parts) >= 3 and parts[2].strip() == "K":
            try:
                pts_time = float(parts[0])
                size = int(parts[1])
                keyframes.append((pts_time, size))
            except ValueError:
                continue

    if not keyframes:
        if chat_id:
            send_message(chat_id, "⚠️ امکان تقسیم خودکار ویدیو وجود نداشت. فایل کامل ارسال می‌شود.")
        return [video_path]

    # Compute cumulative sizes up to each keyframe
    cum_sizes = []
    total = 0
    for pts, sz in keyframes:
        total += sz
        cum_sizes.append((pts, total))

    # Group keyframes into chunks ≤ TARGET_SIZE
    cut_points = []
    current_start = 0.0
    current_size = 0
    prev_pts = 0.0
    for i, (pts, cum_size) in enumerate(cum_sizes):
        chunk_size = cum_size - current_size
        if chunk_size > TARGET_SIZE and i > 0:
            # Cut at previous keyframe (the one before current)
            cut_pts = prev_pts
            cut_points.append(cut_pts)
            current_size = cum_sizes[i-1][1]  # cumulative size at previous keyframe
            current_start = cut_pts
        prev_pts = pts
    # Add last segment (from last cut to end)
    if cut_points:
        last_cut = cut_points[-1]
        if last_cut < keyframes[-1][0]:
            # we have a final segment
            pass
    else:
        # No cuts made, file fits in one chunk (already handled earlier if total size <= TARGET_SIZE)
        # but we still split to be safe
        pass

    # If no cuts needed, return whole file
    if not cut_points:
        return [video_path]

    # Add end point
    cut_points.append(keyframes[-1][0] + 0.5)  # a little beyond

    # Build chunk ranges
    chunks = []
    prev_cut = 0.0
    for cp in cut_points:
        if cp > prev_cut:
            chunks.append((prev_cut, cp))
            prev_cut = cp

    total_chunks = len(chunks)
    parts = []
    out_dir = os.path.dirname(video_path)

    for idx, (start, end) in enumerate(chunks, 1):
        if chat_id:
            send_message(chat_id, f"✂️ در حال برش قطعه {idx} از {total_chunks}...")
        out_path = os.path.join(out_dir, f"chunk_{idx:03d}.mp4")
        cmd = [
            ffmpeg, "-y", "-ss", str(start), "-to", str(end),
            "-i", video_path, "-c", "copy", "-movflags", "+faststart",
            out_path
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            if os.path.getsize(out_path) <= MAX_SEND_SIZE:
                parts.append(out_path)
            else:
                logger.warning(f"Chunk {idx} exceeds MAX_SEND_SIZE despite keyframe alignment")
                # Fallback for that chunk: binary split (but still add)
                sub_parts = split_file_binary(out_path, original_filename=os.path.basename(out_path))
                parts.extend(sub_parts)
                safe_remove(out_path)
        except Exception as e:
            logger.error(f"ffmpeg cut failed for chunk {idx}: {e}")
            if chat_id:
                send_message(chat_id, f"⚠️ برش قطعه {idx} با خطا مواجه شد.")

    return parts if parts else [video_path]

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

# ---------- Cookie Handling ----------
COOKIE_FILE = "cookies.txt"

def parse_netscape_cookies(filepath: str) -> List[Dict]:
    cookies = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, domain_flag, path, secure, expires, name, value = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "expires": int(expires) if expires.isdigit() else -1,
                    "httpOnly": False,
                    "secure": secure == "TRUE",
                    "sameSite": "Lax"
                })
    except Exception as e:
        logger.error(f"Failed to parse cookies: {e}")
    return cookies

def add_cookies_to_context(context, cookie_file=COOKIE_FILE):
    if os.path.exists(cookie_file):
        cookies = parse_netscape_cookies(cookie_file)
        if cookies:
            context.add_cookies(cookies)
            logger.info(f"Added {len(cookies)} cookies to Playwright context")

# ---------- Bale API functions ----------
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

def sendPhoto(chat_id: int, photo_path: str, caption: str = "", reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    if not os.path.exists(photo_path):
        return None
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": (os.path.basename(photo_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            if reply_markup:
                data["reply_markup"] = reply_markup
            r = requests.post(f"{API_BASE}/sendPhoto", files=files, data=data, timeout=REQUEST_TIMEOUT*4)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"sendPhoto error: {e}")
        return None

def sendVideo(chat_id: int, video_path: str, caption: str = "", reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    if not os.path.exists(video_path):
        return None
    try:
        with open(video_path, "rb") as f:
            files = {"video": (os.path.basename(video_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            if reply_markup:
                data["reply_markup"] = reply_markup
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
    data[code] = {"used_by": chat_id, "used_at": now}
    save_vip_data(data)

    # Auto-register user if not already in users.json
    users_data = load_users()
    if str(chat_id) not in users_data["users"]:
        users_data["users"][str(chat_id)] = {
            "quota_used_bytes": 0, "quota_reset_time": 0.0,
            "quality": "720p", "send_mode": "playable",
            "preview_mode": "thumbnail", "video_mode": "document",
            "photo_mode": "showable", "result_count": 10,
            "search_mode": "relevance", "search_method": "scrapetube",
            "info_method": "playwright", "download_method": "auto",
            "target_channel": 0
        }
        save_users(users_data)
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
                "quota_used_bytes": 0, "quota_reset_time": 0.0,
                "quality": "720p", "send_mode": "playable",
                "preview_mode": "thumbnail", "video_mode": "document",
                "photo_mode": "showable", "result_count": 10,
                "search_mode": "relevance", "search_method": "scrapetube",
                "info_method": "playwright", "download_method": "auto",
                "target_channel": 0
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
            u.setdefault("search_method", "scrapetube")
            u.setdefault("info_method", "playwright")
            u.setdefault("download_method", "auto")
            u.setdefault("target_channel", 0)
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
        "search_mode": "relevance", "search_method": "scrapetube",
        "info_method": "playwright", "download_method": "auto",
        "target_channel": 0
    })

def update_user_setting(chat_id: int, key: str, value: Any) -> None:
    data = load_users()
    uid = str(chat_id)
    if uid in data["users"]:
        data["users"][uid][key] = value
        save_users(data)

# ---------- Search Methods ----------
def _search_scrapetube(query: str, limit: int) -> List[Dict[str, str]]:
    try:
        videos = scrapetube.get_search(query, limit=limit)
        results = []
        for v in videos:
            vid = v["videoId"]
            title = v.get("title", {}).get("runs", [{}])[0].get("text", "")
            results.append({"video_id": vid, "title": title})
        return results
    except Exception as e:
        logger.error(f"scrapetube search error: {e}")
        return []

def _search_ytdlp(query: str, limit: int) -> List[Dict[str, str]]:
    try:
        cmd = [
            "yt-dlp",
            "--remote-components", "ejs:github",
            "--js-runtimes", "deno",
            "--flat-playlist", "--print", "%(id)s|%(title)s|%(duration)s",
            f"ytsearch{limit}:{query}"
        ]
        if os.path.exists(COOKIE_FILE):
            cmd.insert(1, "--cookies")
            cmd.insert(2, COOKIE_FILE)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SEARCH_TIMEOUT)
        results = []
        for line in proc.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) >= 2:
                vid = parts[0]
                title = parts[1]
                results.append({"video_id": vid, "title": title})
                if len(results) >= limit:
                    break
        return results
    except Exception as e:
        logger.error(f"yt-dlp search error: {e}")
        return []

def _search_innertube(query: str, limit: int) -> List[Dict[str, str]]:
    return []

# ---------- Info Methods ----------
def _info_playwright(video_id: str) -> Optional[Dict[str, Any]]:
    return scrape_watch_page(video_id)

def _info_ytdlp(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        cmd = ["yt-dlp", "--dump-json", f"https://www.youtube.com/watch?v={video_id}"]
        if os.path.exists(COOKIE_FILE):
            cmd.insert(1, "--cookies")
            cmd.insert(2, COOKIE_FILE)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=WATCH_TIMEOUT)
        data = json.loads(proc.stdout)
        return {
            "video_id": video_id,
            "title": data.get("title"),
            "uploader": data.get("uploader") or data.get("channel"),
            "views": str(data.get("view_count", "")),
            "duration": str(data.get("duration", "")),
            "description": data.get("description", ""),
            "screenshot_path": None
        }
    except Exception as e:
        logger.error(f"yt-dlp info error: {e}")
        return None

def _info_oembed(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"https://youtube.com/oembed?url=https://youtube.com/watch?v={video_id}&format=json"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "video_id": video_id,
                "title": data.get("title"),
                "uploader": data.get("author_name"),
                "views": "",
                "duration": "",
                "description": "",
                "screenshot_path": None
            }
    except Exception:
        pass
    return None

# ---------- Playwright scraping ----------
def scrape_watch_page(video_id: str) -> Optional[Dict[str, Any]]:
    pw = browser = context = page = None
    screenshot_path = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 720, "height": 1280})
        add_cookies_to_context(context)
        page = context.new_page()
        page.set_viewport_size({"width": 720, "height": 1280})
        page.goto(f"https://www.youtube.com/watch?v={video_id}",
                  wait_until="domcontentloaded", timeout=WATCH_TIMEOUT * 1000)
        for _ in range(3):
            page.wait_for_timeout(5000)
            if page.query_selector("h1 yt-formatted-string"):
                break

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

        try:
            screenshot_path = f"downloads/{uuid.uuid4().hex[:8]}_screenshot.jpg"
            page.screenshot(path=screenshot_path, full_page=True, quality=85)
        except Exception:
            pass

        return {
            "video_id": video_id,
            "title": data.get("title"),
            "uploader": data.get("uploader"),
            "views": data.get("views"),
            "duration": data.get("duration"),
            "description": data.get("description", ""),
            "screenshot_path": screenshot_path
        }
    except Exception as e:
        logger.error(f"scrape_watch_page error: {e}")
        return None
    finally:
        if page: page.close()
        if context: context.close()
        if browser: browser.close()
        if pw: pw.stop()

# ---------- Channel helpers ----------
def get_channel_info_exact(channel_id: str) -> Optional[Dict[str, Any]]:
    pw = browser = page = None
    try:
        if channel_id.startswith("@"):
            url = f"https://www.youtube.com/{channel_id}/about"
        else:
            url = f"https://www.youtube.com/channel/{channel_id}/about"
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        add_cookies_to_context(context)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT*1000)
        try:
            page.wait_for_selector("#channel-header", timeout=10000)
        except PlaywrightTimeout:
            pass
        name = page.evaluate("document.querySelector('#channel-header yt-formatted-string')?.textContent?.trim() || ''")
        subs = page.evaluate("document.querySelector('#subscriber-count')?.textContent?.trim() || ''")
        videos_count = page.evaluate("""() => {
            const tabs = document.querySelectorAll('yt-tab-shape, tp-yt-paper-tab');
            const vtab = [...tabs].find(el => el.textContent.includes('Videos'));
            return vtab ? vtab.textContent.trim() : '';
        }""")
        avatar = page.evaluate("document.querySelector('#img')?.getAttribute('src') || ''")
        return {"name": name, "subscribers": subs, "videos_count": videos_count, "avatar": avatar, "channel_id": channel_id}
    except Exception as e:
        logger.error(f"get_channel_info_exact error: {e}")
        return None
    finally:
        if page: page.close()
        if browser: browser.close()
        if pw: pw.stop()

def get_channel_videos_playwright(channel_id: str, limit: int = 50) -> List[Dict[str, str]]:
    pw = browser = page = None
    try:
        if channel_id.startswith("@"):
            url = f"https://www.youtube.com/{channel_id}/videos"
        else:
            url = f"https://www.youtube.com/channel/{channel_id}/videos"
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        add_cookies_to_context(context)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT*1000)
        try:
            page.wait_for_selector("ytd-rich-grid-media, ytd-video-renderer", timeout=10000)
        except PlaywrightTimeout:
            pass
        prev = 0
        for _ in range(10):
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            time.sleep(2)
            current = len(page.query_selector_all("ytd-rich-grid-media, ytd-video-renderer"))
            if current >= limit or current == prev:
                break
            prev = current
        items = page.evaluate("""
        (limit) => {
            const els = document.querySelectorAll('ytd-rich-grid-media, ytd-video-renderer');
            const results = [];
            for (const el of els) {
                if (results.length >= limit) break;
                const titleEl = el.querySelector('#video-title');
                const title = titleEl ? titleEl.textContent.trim() : '';
                const href = titleEl ? titleEl.closest('a')?.getAttribute('href') : '';
                const vid = href ? href.split('?v=')[1]?.split('&')[0] : '';
                if (vid && vid.length === 11) {
                    results.push({video_id: vid, title: title});
                }
            }
            return results;
        }
        """, limit)
        return items[:limit]
    except Exception as e:
        logger.error(f"get_channel_videos_playwright error: {e}")
        return []
    finally:
        if page: page.close()
        if browser: browser.close()
        if pw: pw.stop()

# ---------- Download ----------
def _download_ytdlp(video_id: str, quality: str, save_dir: str) -> Optional[str]:
    video_path = os.path.join(save_dir, f"{video_id}.mp4")
    try:
        profile = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["720p"])
        height = profile["height"]
        vcodec = profile["vcodec"]
        fmt = (
            f"bestvideo[height<={height}][vcodec*={vcodec}][ext=mp4]"
            f"+bestaudio[ext=m4a]/best[height<={height}]"
        )
        max_bitrate = profile["max_bitrate"]
        # Convert bitrate to numeric for bufsize
        bitrate_str = max_bitrate.replace("M", "e6").replace("k", "e3")
        try:
            bitrate_val = float(bitrate_str)
        except ValueError:
            bitrate_val = 2.5e6
        bufsize = int(bitrate_val * 2)

        cmd = [
            "yt-dlp",
            "--remote-components", "ejs:github",
            "--js-runtimes", "deno",
            "-f", fmt,
            "-o", video_path,
            "--postprocessor-args", f"ffmpeg:-b:v {max_bitrate} -maxrate {max_bitrate} -bufsize {bufsize}",
            "--no-playlist",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        if os.path.exists(COOKIE_FILE):
            cmd.insert(1, "--cookies")
            cmd.insert(2, COOKIE_FILE)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        if os.path.exists(video_path):
            return video_path
    except subprocess.CalledProcessError as e:
        logger.error(f"yt-dlp failed: {e.stderr}")
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
    return None

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

def download_video(video_id: str, quality: str, save_dir: str, method: str = "auto") -> Optional[str]:
    if method == "auto":
        path = _download_ytdlp(video_id, quality, save_dir)
        if path: return path
        path = _download_hubytconvert(video_id, quality, save_dir)
        if path: return path
        return _download_cobalt(video_id, quality, save_dir)
    elif method == "ytdlp":
        return _download_ytdlp(video_id, quality, save_dir)
    elif method == "hubytconvert":
        return _download_hubytconvert(video_id, quality, save_dir)
    elif method == "cobalt":
        return _download_cobalt(video_id, quality, save_dir)
    else:
        return _download_ytdlp(video_id, quality, save_dir)

# ---------- Send parts ----------
def send_video_parts(parts: List[str], chat_id: int, video_mode: str = "document",
                     send_mode: str = "playable") -> bool:
    total = len(parts)
    slow_mode = False
    slow_counter = 0
    for i, part in enumerate(parts, 1):
        part_start = time.time()
        send_message(chat_id, f"📤 در حال ارسال پارت {i}/{total}...")
        success = False
        for attempt in range(1, 4):
            if time.time() - part_start > 300:
                send_message(chat_id, f"⛔ ارسال پارت {i} به‌دلیل اتمام وقت متوقف شد.")
                return False
            try:
                if video_mode == "showable" and send_mode == "playable":
                    caption = "" if i == 1 else f"ادامه ویدیو... (بخش {i})"
                    res = sendVideo(chat_id, part, caption=caption)
                else:
                    caption = "ادامه ویدیو..." if i > 1 else ""
                    res = send_document(chat_id, part, caption=caption)
                if res and res.get("ok"):
                    success = True
                    break
            except Exception:
                pass
            if attempt < 3:
                send_message(chat_id, f"🔄 تلاش {attempt+1} برای پارت {i} در ۱۰ ثانیه...")
                time.sleep(10)
            else:
                send_message(chat_id, f"⛔ ارسال پارت {i} ناموفق ماند. لطفاً دوباره تلاش کنید.")
        if not success:
            return False
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
    return True

# ---------- Format helpers ----------
def format_count(raw: str) -> str:
    if not raw:
        return "?"
    clean = raw.strip()
    if clean and clean[-1] in 'KM':
        return clean
    num_str = re.sub(r"[^\d.]", "", raw)
    if not num_str:
        return raw.strip() or "?"
    try:
        num = float(num_str)
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
        formatted = str(int(num))
    return formatted

def seconds_to_display(sec: int) -> str:
    if not isinstance(sec, (int, float)):
        if isinstance(sec, str) and sec.isdigit():
            sec = int(sec)
        else:
            return str(sec)
    sec = int(sec)
    if sec >= 3600:
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h}:{m:02d}:{s:02d}"
    else:
        m = sec // 60
        s = sec % 60
        return f"{m}:{s:02d}"

# ---------- Info display ----------
def handle_info(chat_id: int, video_id: str, index: int) -> None:
    settings = get_user_settings(chat_id)
    info_method = settings.get("info_method", "playwright")
    if info_method == "playwright":
        info = _info_playwright(video_id)
    elif info_method == "ytdlp":
        info = _info_ytdlp(video_id)
    elif info_method == "oembed":
        info = _info_oembed(video_id)
    else:
        info = _info_playwright(video_id)
    if not info or not info.get("title"):
        send_message(chat_id, "⛔ دریافت اطلاعات ویدیو ناموفق بود.")
        return

    preview_mode = settings.get("preview_mode", "thumbnail")
    photo_mode = settings.get("photo_mode", "showable")

    title = info.get("title", "?")
    uploader = info.get("uploader", "?")
    views = format_count(info.get("views", "?"))
    duration_raw = info.get("duration", "?")
    if isinstance(duration_raw, str) and duration_raw.isdigit():
        duration_raw = int(duration_raw)
    duration_display = seconds_to_display(duration_raw) if isinstance(duration_raw, (int, float)) else str(duration_raw)
    desc = info.get("description", "")

    caption = (
        f"📹 {title}\n"
        f"👤 {uploader}\n"
        f"👁 {views} views\n"
        f"⏱ {duration_display}\n"
    )
    if desc:
        if len(desc) <= 200:
            caption += f"📝 {desc}\n"
        else:
            caption += "📄 توضیحات کامل در فایل بالا.\n"
    else:
        caption += "📝 —\n"
    caption += f"🔗 https://youtube.com/watch?v={video_id}\n📥 /dl_{index}"

    if desc and len(desc) > 200:
        desc_path = f"downloads/{chat_id}/{video_id}_desc.txt"
        os.makedirs(os.path.dirname(desc_path), exist_ok=True)
        with open(desc_path, "w", encoding="utf-8") as f:
            f.write(desc)
        send_document(chat_id, desc_path, caption="📄 توضیحات کامل")
        safe_remove(desc_path)

    screenshot_path = info.get("screenshot_path")
    if preview_mode == "screenshot" and screenshot_path and os.path.exists(screenshot_path):
        if photo_mode == "document":
            send_document(chat_id, screenshot_path, caption=caption)
        else:
            sendPhoto(chat_id, screenshot_path, caption=caption)
        safe_remove(screenshot_path)
    else:
        thumb = download_thumbnail(video_id, f"downloads/{chat_id}")
        if thumb:
            if photo_mode == "document":
                send_document(chat_id, thumb, caption=caption)
            else:
                sendPhoto(chat_id, thumb, caption=caption)
            safe_remove(thumb)
        else:
            send_message(chat_id, caption)

# ---------- Download handler ----------
def handle_download(chat_id: int, video_id: str, index: int, confirmed: bool = False) -> None:
    allowed, msg = check_quota_before(chat_id, QUOTA_THRESHOLD)
    if not allowed:
        send_message(chat_id, msg)  # error to user's PV
        return

    settings = get_user_settings(chat_id)
    quality = settings.get("quality", "720p")
    send_mode = settings.get("send_mode", "playable")
    video_mode = settings.get("video_mode", "document")
    download_method = settings.get("download_method", "auto")
    target_channel = settings.get("target_channel", 0)

    destination = int(target_channel) if target_channel else chat_id
    report_chat_id = chat_id  # errors always go to user

    job_id = uuid.uuid4().hex[:8]
    save_dir = f"downloads/{chat_id}/{job_id}"
    os.makedirs(save_dir, exist_ok=True)

    start_time = time.time()
    try:
        send_message(destination, "📥 دانلود ویدیو آغاز شد...")  # progress to channel/group if set
        video_path = download_video(video_id, quality, save_dir, method=download_method)
        if not video_path or not os.path.exists(video_path):
            send_message(report_chat_id, "❌ دانلود ویدیو ناموفق بود.")
            return

        file_size = os.path.getsize(video_path)
        if file_size == 0:
            send_message(report_chat_id, "❌ فایل دانلود شده خراب است (حجم صفر).")
            safe_remove(video_path)
            return

        add_quota_usage(chat_id, file_size)

        if send_mode == "playable":
            parts = split_video_by_keyframes(video_path, chat_id=destination)
        else:
            zip_base = os.path.splitext(video_path)[0]
            zip_path = f"{zip_base}.zip"
            shutil.make_archive(zip_base, 'zip', os.path.dirname(video_path), os.path.basename(video_path))
            if not os.path.exists(zip_path) or os.path.getsize(zip_path) == 0:
                send_message(report_chat_id, "❌ خطا در فشرده‌سازی فایل.")
                safe_remove(video_path)
                return
            parts = split_file_binary(zip_path, original_filename=os.path.basename(zip_path))
            safe_remove(zip_path)

        if not parts:
            send_message(report_chat_id, "⛔ خطا در آماده‌سازی فایل برای ارسال.")
            return

        if send_video_parts(parts, destination, video_mode, send_mode):
            send_message(destination, "✅ ویدیو با موفقیت ارسال شد.")
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
            send_message(destination, report)
        else:
            send_message(report_chat_id, "⚠️ ارسال ویدیو با مشکل مواجه شد.")
    except Exception as e:
        logger.exception("download flow error")
        send_message(report_chat_id, f"⛔ خطایی رخ داد: {str(e)[:200]}")
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

# ---------- Paginated display ----------
def display_results_page(chat_id: int) -> None:
    state = SEARCH_STATE.get(chat_id)
    if not state or not state.get("all_video_ids"):
        send_message(chat_id, "⚠️ هیچ نتیجه‌ای موجود نیست.")
        return
    settings = get_user_settings(chat_id)
    count = settings.get("result_count", 10)
    offset = state["offset"]
    all_ids = state["all_video_ids"]
    batch = all_ids[offset:offset+count]
    if not batch:
        send_message(chat_id, "✅ تمام نتایج نمایش داده شد.")
        return

    photo_mode = settings.get("photo_mode", "showable")

    for local_idx, vid in enumerate(batch, 1):
        global_idx = offset + local_idx
        caption = f"🔗 https://youtube.com/watch?v={vid}\n📥 /dl_{global_idx}\nℹ️ /info_{global_idx}"
        thumb = download_thumbnail(vid, f"downloads/{chat_id}")
        if thumb:
            if photo_mode == "document":
                send_document(chat_id, thumb, caption=caption)
            else:
                sendPhoto(chat_id, thumb, caption=caption)
            safe_remove(thumb)
        else:
            send_message(chat_id, caption)

    if offset + count < len(all_ids):
        keyboard = {"inline_keyboard": [[{"text": "🔄 نتایج بیشتر", "callback_data": "more_results"}]]}
        send_message(chat_id, "برای نتایج بیشتر دکمه زیر را بزنید:", reply_markup=keyboard)

# ---------- Search / Channel Command Handler ----------
def handle_search_command(chat_id: int, query: str) -> None:
    settings = get_user_settings(chat_id)
    search_method = settings.get("search_method", "scrapetube")
    limit = settings.get("result_count", 10) * 3
    if search_method == "scrapetube":
        results = _search_scrapetube(query, limit)
    elif search_method == "ytdlp":
        results = _search_ytdlp(query, limit)
    elif search_method == "innertube":
        results = _search_innertube(query, limit)
    else:
        results = _search_scrapetube(query, limit)

    if not results:
        send_message(chat_id, "❌ نتیجه‌ای یافت نشد.")
        return

    all_video_ids = [r["video_id"] for r in results]
    with state_lock:
        SEARCH_STATE[chat_id] = {
            "query": query,
            "search_type": "normal",
            "offset": 0,
            "all_video_ids": all_video_ids
        }
    display_results_page(chat_id)

def handle_more_results(chat_id: int) -> None:
    state = SEARCH_STATE.get(chat_id)
    if not state:
        send_message(chat_id, "⚠️ جستجوی فعالی وجود ندارد.")
        return
    new_offset = state["offset"] + get_user_settings(chat_id).get("result_count", 10)
    if new_offset >= len(state["all_video_ids"]):
        send_message(chat_id, "✅ تمام نتایج قبلاً نمایش داده شده است.")
        return
    state["offset"] = new_offset
    display_results_page(chat_id)

# ---------- Channel search flows ----------
def handle_channel_exact(chat_id: int, channel_id: str) -> None:
    info = get_channel_info_exact(channel_id)
    if not info:
        send_message(chat_id, "❌ دریافت اطلاعات کانال ناموفق بود.")
        return
    caption = (
        f"📺 {info.get('name', '')}\n"
        f"👥 {info.get('subscribers', '')}\n"
        f"🎬 {info.get('videos_count', '')}\n"
        f"🔗 https://youtube.com/{'@' if channel_id.startswith('@') else 'channel/'}{channel_id}"
    )
    avatar = info.get("avatar")
    if avatar:
        thumb_path = download_file(avatar, f"downloads/{chat_id}", f"{channel_id}_avatar.jpg")
        if thumb_path:
            sendPhoto(chat_id, thumb_path, caption=caption)
            safe_remove(thumb_path)
        else:
            send_message(chat_id, caption)
    else:
        send_message(chat_id, caption)
    keyboard = {"inline_keyboard": [[{"text": "📋 ویدیوها", "callback_data": f"channel_videos|{channel_id}"}]]}
    send_message(chat_id, "می‌خواهید ویدیوهای این کانال را ببینید؟", reply_markup=keyboard)

def handle_channel_videos(chat_id: int, channel_id: str) -> None:
    send_message(chat_id, "⏳ در حال دریافت ویدیوهای کانال...")
    limit = 100
    results = get_channel_videos_playwright(channel_id, limit)
    if not results:
        send_message(chat_id, "❌ ویدیویی یافت نشد.")
        return
    all_video_ids = [r["video_id"] for r in results]
    with state_lock:
        SEARCH_STATE[chat_id] = {
            "query": channel_id,
            "search_type": "channel_videos",
            "offset": 0,
            "all_video_ids": all_video_ids
        }
    display_results_page(chat_id)

# ---------- Link confirmation ----------
def handle_link_confirm(chat_id: int, video_id: str) -> None:
    info = _info_playwright(video_id) or _info_oembed(video_id)
    if not info:
        info = {"title": get_video_title(video_id), "duration": 0}
    title = info.get("title", "?")
    quality = get_user_settings(chat_id).get("quality", "720p")
    bitrate = BITRATE_TABLE.get(quality, 2e6)

    duration_raw = info.get("duration", 0)
    if isinstance(duration_raw, str) and duration_raw.isdigit():
        duration_raw = int(duration_raw)
    duration_display = seconds_to_display(duration_raw) if isinstance(duration_raw, (int, float)) else "?"

    if isinstance(duration_raw, (int, float)):
        estimated_size = (duration_raw * bitrate) / 8
    else:
        estimated_size = 10 * 1024 * 1024
    size_mb = estimated_size / (1024 * 1024)

    caption = (
        f"📹 {title}\n"
        f"🔗 https://youtube.com/watch?v={video_id}\n"
        f"📏 کیفیت: {quality}\n"
        f"⏱ زمان: {duration_display}\n"
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

# ---------- Command processor ----------
def process_message(chat_id: int, text: str) -> None:
    # Silently ignore messages from channels except specific commands
    if chat_id < 0:
        if text.startswith("/set_channel") or text.startswith("/unset_channel"):
            pass
        else:
            return

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
    if state == "awaiting_channel_exact" and not text.startswith("/"):
        handle_channel_exact(chat_id, text)
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
            state = SEARCH_STATE.get(chat_id)
            if state and 1 <= idx <= len(state["all_video_ids"]):
                vid = state["all_video_ids"][idx-1]
                threading.Thread(target=handle_info, args=(chat_id, vid, idx), daemon=True).start()
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نامعتبر. مثال: /info_1")
    elif text.startswith("/dl_"):
        m = re.match(r"/dl_(\d+)", text)
        if m:
            idx = int(m.group(1))
            state = SEARCH_STATE.get(chat_id)
            if state and 1 <= idx <= len(state["all_video_ids"]):
                vid = state["all_video_ids"][idx-1]
                threading.Thread(target=handle_download, args=(chat_id, vid, idx), kwargs={"confirmed": True}, daemon=True).start()
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نامعتبر. مثال: /dl_1")
    elif text == "/more":
        handle_more_results(chat_id)
    elif text == "/settings":
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif text == "/help":
        help_text = (
            "📖 راهنمای ربات:\n"
            "/search - جستجوی ویدیو\n"
            "/info_1 - اطلاعات ویدیوی شماره ۱\n"
            "/dl_1 - دانلود ویدیوی شماره ۱\n"
            "/more - نتایج بیشتر\n"
            "/settings - تنظیمات\n"
            "📥 با دکمه «دانلود با لینک» لینک مستقیم بفرستید.\n"
            "📺 با دکمه «جستجوی کانال» کانال‌ها را بگردید.\n"
            "📌 ادمین: /panel, /setadmin, /set_channel, /unset_channel"
        )
        send_message(chat_id, help_text)
    elif text == "/panel" and is_admin(chat_id):
        data = load_users()
        users = data.get("users", {})
        if not users:
            send_message(chat_id, "هیچ کاربر VIP یافت نشد.")
        else:
            lines = ["📊 پنل مدیریت:", "کاربران VIP:"]
            for uid, u in users.items():
                if int(uid) == data["admin_id"]:
                    continue
                used_mb = u.get("quota_used_bytes", 0) / (1024 * 1024)
                reset_ts = u.get("quota_reset_time", 0)
                if reset_ts:
                    from datetime import datetime
                    reset_str = datetime.fromtimestamp(reset_ts).strftime("%H:%M")
                else:
                    reset_str = "—"
                lines.append(f"👤 {uid} | مصرف: {used_mb:.1f}MB | ریست: {reset_str}")
            send_message(chat_id, "\n".join(lines))
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
    elif text.startswith("/set_channel") and is_admin(chat_id):
        parts = text.split()
        if len(parts) == 2:
            try:
                channel_id = int(parts[1])
                update_user_setting(chat_id, "target_channel", channel_id)
                send_message(chat_id, f"✅ ارسال خودکار به کانال {channel_id} فعال شد.")
                send_message(channel_id, "✅ ربات برای آپلود خودکار روی این کانال تنظیم شد.")
            except ValueError:
                send_message(chat_id, "❌ آیدی کانال نامعتبر.")
        else:
            send_message(chat_id, "❌ فرمت: /set_channel <channel_id>")
    elif text == "/unset_channel" and is_admin(chat_id):
        update_user_setting(chat_id, "target_channel", 0)
        send_message(chat_id, "✅ ارسال خودکار به کانال غیرفعال شد. فایل‌ها به چت خصوصی شما ارسال می‌شوند.")
    else:
        send_message(chat_id, "⚠️ فرمان نامعتبر. از منوی زیر استفاده کنید:", reply_markup=main_menu())

# ---------- Menus ----------
def main_menu() -> Dict:
    return {"inline_keyboard": [
        [{"text": "🔍 جستجو", "callback_data": "search"},
         {"text": "📥 دانلود با لینک", "callback_data": "download_link"}],
        [{"text": "📺 جستجوی کانال", "callback_data": "search_channel"}],
        [{"text": "ℹ️ راهنما", "callback_data": "help"},
         {"text": "⚙️ تنظیمات", "callback_data": "settings"}]
    ]}

def settings_menu(s: Dict) -> Dict:
    preview_text = "تصویر کوچک" if s.get("preview_mode") == "thumbnail" else "اسکرین‌شات"
    video_text = "Showable" if s.get("video_mode") == "showable" else "Document"
    photo_text = "Showable" if s.get("photo_mode") == "showable" else "Document"
    search_mode_text = {"relevance": "مرتبط‌ترین", "newest": "جدیدترین", "popular": "محبوب‌ترین"}.get(s.get("search_mode"), "?")
    dl_method = s.get("download_method", "auto")
    search_method = s.get("search_method", "scrapetube")
    info_method = s.get("info_method", "playwright")
    return {"inline_keyboard": [
        [{"text": f"🎬 کیفیت: {s.get('quality')}", "callback_data": "set_quality"}],
        [{"text": f"📦 حالت ارسال: {'فیلم (قابل پخش)' if s.get('send_mode')=='playable' else 'زیپ (فشرده)'}", "callback_data": "set_mode"}],
        [{"text": f"🖼️ پیش‌نمایش: {preview_text}", "callback_data": "set_preview"}],
        [{"text": f"🎥 حالت ویدیو: {video_text}", "callback_data": "set_video_mode"}],
        [{"text": f"🖼️ حالت عکس: {photo_text}", "callback_data": "set_photo_mode"}],
        [{"text": f"🔢 نتایج: {s.get('result_count')}", "callback_data": "set_page_size"}],
        [{"text": f"🔍 نوع جستجو: {search_mode_text}", "callback_data": "set_search_mode"}],
        [{"text": f"📥 روش دانلود: {dl_method}", "callback_data": "set_dl_method"}],
        [{"text": f"🔍 روش جستجو: {search_method}", "callback_data": "set_search_method"}],
        [{"text": f"ℹ️ روش اطلاعات: {info_method}", "callback_data": "set_info_method"}],
        [{"text": "🔙 بازگشت", "callback_data": "main_menu"}]
    ]}

def quality_keyboard(current: str) -> Dict:
    qualities = ["360p", "480p", "720p", "1080p", "4K"]
    btns = [[{"text": f"{'✅' if q==current else '○'} {q}", "callback_data": f"quality_{q}"}] for q in qualities]
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
    btns = [[{"text": f"{'✅' if s==current else '○'} {s}", "callback_data": f"pagesize_{s}"}] for s in sizes]
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

def search_mode_keyboard(current: str) -> Dict:
    modes = [("relevance", "مرتبط‌ترین"), ("newest", "جدیدترین"), ("popular", "محبوب‌ترین")]
    btns = [[{"text": f"{'✅' if val==current else '○'} {label}", "callback_data": f"search_{val}"}] for val, label in modes]
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

def dl_method_keyboard(current: str) -> Dict:
    methods = ["auto", "ytdlp", "hubytconvert", "cobalt"]
    btns = [[{"text": f"{'✅' if m==current else '○'} {m}", "callback_data": f"dlmethod_{m}"}] for m in methods]
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

def search_method_keyboard(current: str) -> Dict:
    methods = ["scrapetube", "ytdlp", "innertube"]
    btns = [[{"text": f"{'✅' if m==current else '○'} {m}", "callback_data": f"searchmethod_{m}"}] for m in methods]
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

def info_method_keyboard(current: str) -> Dict:
    methods = ["playwright", "ytdlp", "oembed"]
    btns = [[{"text": f"{'✅' if m==current else '○'} {m}", "callback_data": f"infomethod_{m}"}] for m in methods]
    btns.append([{"text": "🔙 بازگشت", "callback_data": "settings"}])
    return {"inline_keyboard": btns}

# ---------- Callback Handler ----------
def process_callback(chat_id: int, data: str, message_id: int, callback_query_id: str) -> None:
    # Silently ignore callbacks from channels
    if chat_id < 0:
        return

    toast_text = ""
    if data == "search":
        with state_lock:
            USER_STATE[chat_id] = "awaiting_query"
        edit_message_text(chat_id, message_id, "🔍 عبارت جستجو را تایپ کنید:")
    elif data == "download_link":
        with state_lock:
            USER_STATE[chat_id] = "awaiting_url"
        edit_message_text(chat_id, message_id, "📥 لینک ویدیوی یوتیوب را بفرستید:")
    elif data == "search_channel":
        with state_lock:
            USER_STATE[chat_id] = "awaiting_channel_exact"
        edit_message_text(chat_id, message_id, "📌 آیدی کانال (مثلاً @Google یا UCxxx) را بفرستید:")
    elif data == "more_results":
        handle_more_results(chat_id)
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
        toast_text = f"کیفیت روی {q} تنظیم شد."
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_mode":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "📦 حالت ارسال:", reply_markup=mode_keyboard(s["send_mode"]))
    elif data.startswith("mode_"):
        m = data.split("_", 1)[1]
        update_user_setting(chat_id, "send_mode", m)
        mode_str = "فیلم (قابل پخش)" if m == "playable" else "زیپ (فشرده)"
        toast_text = f"حالت ارسال: {mode_str}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
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
    elif data == "set_dl_method":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "📥 روش دانلود:", reply_markup=dl_method_keyboard(s["download_method"]))
    elif data.startswith("dlmethod_"):
        method = data.split("_", 1)[1]
        update_user_setting(chat_id, "download_method", method)
        toast_text = f"روش دانلود: {method}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_search_method":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🔍 روش جستجو:", reply_markup=search_method_keyboard(s["search_method"]))
    elif data.startswith("searchmethod_"):
        method = data.split("_", 1)[1]
        update_user_setting(chat_id, "search_method", method)
        toast_text = f"روش جستجو: {method}"
        edit_message_text(chat_id, message_id, f"✅ {toast_text}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s))
    elif data == "set_info_method":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "ℹ️ روش اطلاعات:", reply_markup=info_method_keyboard(s["info_method"]))
    elif data.startswith("infomethod_"):
        method = data.split("_", 1)[1]
        update_user_setting(chat_id, "info_method", method)
        toast_text = f"روش اطلاعات: {method}"
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
    logger.info("YouTube Bale Bot v5.0 started")
    os.makedirs("downloads", exist_ok=True)
    offset = 0
    while True:
        try:
            resp = get_updates(offset, LONG_POLL_TIMEOUT)
            if not resp or not resp.get("ok"):
                time.sleep(2)
                continue
            for upd in resp.get("result", []):
                if "my_chat_member" in upd:
                    chat = upd["my_chat_member"]["chat"]
                    new_status = upd["my_chat_member"]["new_chat_member"]["status"]
                    if chat.get("type") == "channel" and new_status == "administrator":
                        channel_name = chat.get("title", "?")
                        channel_id = chat["id"]
                        admin_id = load_users().get("admin_id", ADMIN_CHAT_ID)
                        msg = f"🔔 ربات به کانال «{channel_name}» با آیدی {channel_id} اضافه شد.\nبرای فعال‌سازی ارسال خودکار دانلودها به این کانال، دستور زیر را بفرست:\n/set_channel {channel_id}"
                        send_message(admin_id, msg)

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
