"""
bot.py - YouTube Bale Bot v4.2
Single-file robust bot with quota, Playwright scraping, reliable upload, and link download.
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

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("youtube_bot")

# ---------- Thread-safe globals ----------
state_lock = threading.Lock()
users_lock = threading.Lock()

SEARCH_RESULTS: Dict[int, Dict[int, str]] = {}
USER_STATE: Dict[int, str] = {}

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
    """Split video into playable chunks ≤ max_size_bytes using ffmpeg segment or fallback with size check."""
    if not os.path.exists(video_path):
        return []
    total_size = os.path.getsize(video_path)
    if total_size <= max_size_bytes:
        dest = os.path.join(os.path.dirname(video_path), os.path.basename(video_path))
        shutil.copy2(video_path, dest)
        return [dest]

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.error("ffmpeg not found")
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
        logger.info("segment_size failed, falling back to manual method")

    # Method 2: manual segmentation with guaranteed size limit
    try:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return []
        dur_result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True
        )
        duration = float(dur_result.stdout.strip())
        if duration <= 0:
            return []
        avg_bitrate = (total_size * 8) / duration
        # initial chunk_time with safety factor 0.9
        chunk_time = (max_size_bytes * 8 * 0.9) / avg_bitrate
        if chunk_time <= 0:
            return []

        parts = []
        start = 0.0
        chunk_idx = 1
        while start < duration:
            end = min(start + chunk_time, duration)
            out_path = os.path.join(out_dir, f"chunk_{chunk_idx:03d}.mp4")
            # try to create chunk under size limit, adjusting duration if needed
            for retry in range(5):
                cmd = [
                    ffmpeg, "-y", "-ss", str(start), "-to", str(end),
                    "-i", video_path, "-c", "copy",
                    "-movflags", "+faststart",
                    out_path
                ]
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                chunk_size = os.path.getsize(out_path)
                if chunk_size <= max_size_bytes:
                    parts.append(out_path)
                    break
                # chunk too large, reduce end point
                safe_remove(out_path)
                end = start + (end - start) * 0.9
                if end - start < 1.0:  # minimum 1 second
                    logger.error("Cannot create chunk <= max_size_bytes")
                    return []   # abandon
            else:
                # all retries failed
                logger.error("Chunk creation failed after 5 retries")
                return []
            start = end
            chunk_idx += 1
        return parts
    except Exception as e:
        logger.error(f"Manual split failed: {e}")
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
            r = requests.post(f"{API_BASE}/sendDocument", files=files, data=data, timeout=REQUEST_TIMEOUT*2)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"sendDocument error: {e}")
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
                "send_mode": "playable"
            }
        for u in data["users"].values():
            u.setdefault("quota_used_bytes", 0)
            u.setdefault("quota_reset_time", 0.0)
            u.setdefault("quality", "720p")
            u.setdefault("send_mode", "playable")
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
        "send_mode": "playable"
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
    return data["users"].get(str(chat_id), {"quality": "720p", "send_mode": "playable"})

def update_user_setting(chat_id: int, key: str, value: Any) -> None:
    data = load_users()
    uid = str(chat_id)
    if uid in data["users"]:
        data["users"][uid][key] = value
        save_users(data)

# ---------- Search ----------
def search_youtube(query: str, limit: int = 10) -> List[str]:
    try:
        videos = scrapetube.get_search(query, limit=limit)
        return [v["videoId"] for v in videos]
    except Exception as e:
        logger.error(f"scrapetube error: {e}")
        return []

# ---------- Playwright scraping ----------
def scrape_watch_page(video_id: str) -> Optional[Dict[str, Any]]:
    pw = browser = context = page = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 390, "height": 844})
        page = context.new_page()
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
            let title = '', uploader = '', views = '', likes = '', duration = '', description = '';
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
                    const likeButton = primaryInfo.videoActions?.menuRenderer?.topLevelButtons?.[0]?.toggleButtonRenderer;
                    const likeText = likeButton?.defaultText?.simpleText || likeButton?.toggledText?.simpleText;
                    if (likeText) likes = likeText;
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
            if (!likes) {
                const el = document.querySelector('#top-level-buttons-computed ytd-toggle-button-renderer:first-child #text, #segmented-like-button yt-button-shape button div[aria-label]');
                likes = el ? el.textContent.trim() : '';
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
            return { title, uploader, views, likes, duration, description };
        }
        """)
        dur = data.get("duration")
        if dur and isinstance(dur, str) and dur.isdigit():
            data["duration"] = int(dur)
        return {
            "video_id": video_id,
            "title": data.get("title"),
            "uploader": data.get("uploader"),
            "views": data.get("views"),
            "likes": data.get("likes"),
            "duration": data.get("duration"),
            "description": data.get("description", "")
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

    thumb = download_thumbnail(video_id, f"downloads/{chat_id}")
    if thumb:
        send_document(chat_id, thumb, caption=info.get("title", ""))
        safe_remove(thumb)

    title = info.get("title", "?")
    uploader = info.get("uploader", "?")
    views = format_count(info.get("views", "?"))
    likes = format_count(info.get("likes", "?"))
    duration_raw = info.get("duration", "?")
    duration_display = seconds_to_display(duration_raw) if isinstance(duration_raw, (int, float)) else str(duration_raw)
    desc = info.get("description", "")

    msg = f"📹 {title}\n👤 {uploader}\n👁 {views} | ❤️ {likes} | ⏱ {duration_display}\n"
    if desc and len(desc) <= 200:
        msg += f"📝 {desc}\n"
    elif desc:
        desc_path = f"downloads/{chat_id}/{video_id}_desc.txt"
        os.makedirs(os.path.dirname(desc_path), exist_ok=True)
        with open(desc_path, "w", encoding="utf-8") as f:
            f.write(desc)
        send_document(chat_id, desc_path, caption="📄 توضیحات کامل")
        safe_remove(desc_path)
        msg += "📄 توضیحات کامل در فایل بالا.\n"
    msg += f"🔗 https://youtube.com/watch?v={video_id}\n📥 /dl_{index}"
    send_message(chat_id, msg)

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

def send_video_parts(parts: List[str], chat_id: int) -> bool:
    """Upload parts with 15s delay, 10s retry, 900s total timeout, and dynamic oversize handling."""
    total = len(parts)
    start_time = time.time()
    i = 0
    while i < len(parts):
        part = parts[i]

        # Global timeout
        if time.time() - start_time > 900:
            send_message(chat_id, "⏰ زمان ارسال به پایان رسید.")
            return False

        # If part too large, split further
        if os.path.getsize(part) > MAX_SEND_SIZE:
            logger.info(f"Part {i+1} oversized, binary splitting")
            base, ext = os.path.splitext(part)
            sub_parts = split_file_binary(part, base, ext)
            safe_remove(part)
            # Replace this part with the sub-parts in place
            parts.pop(i)
            for j, sp in enumerate(sub_parts):
                parts.insert(i + j, sp)
            total = len(parts)
            continue   # stay at same i to process the first new sub-part

        # Send the part
        send_message(chat_id, f"📤 در حال ارسال پارت {i+1}/{total}...")
        success = False
        for attempt in range(1, 4):
            try:
                res = send_document(chat_id, part, caption=f"بخش {i+1}/{total}")
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
        time.sleep(15)
        i += 1   # move to next part only after successful send
    return True

def handle_download(chat_id: int, video_id: str, index: int) -> None:
    allowed, msg = check_quota_before(chat_id, QUOTA_THRESHOLD)
    if not allowed:
        send_message(chat_id, msg)
        return

    settings = get_user_settings(chat_id)
    quality = settings.get("quality", "720p")
    send_mode = settings.get("send_mode", "playable")

    job_id = uuid.uuid4().hex[:8]
    save_dir = f"downloads/{chat_id}/{job_id}"
    os.makedirs(save_dir, exist_ok=True)

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

        if send_video_parts(parts, chat_id):
            send_message(chat_id, "✅ ویدیو با موفقیت ارسال شد.")
        else:
            send_message(chat_id, "⚠️ ارسال ویدیو با مشکل مواجه شد.")
    except Exception as e:
        logger.exception("download flow error")
        send_message(chat_id, f"⛔ خطایی رخ داد: {str(e)[:200]}")
    finally:
        shutil.rmtree(save_dir, ignore_errors=True)

# ---------- Menus ----------
def main_menu() -> Dict:
    return {"inline_keyboard": [
        [{"text": "🔍 جستجو", "callback_data": "search"},
         {"text": "📥 دانلود با لینک", "callback_data": "download_link"}],
        [{"text": "ℹ️ راهنما", "callback_data": "help"},
         {"text": "⚙️ تنظیمات", "callback_data": "settings"}]
    ]}

def settings_menu(quality: str, mode: str) -> Dict:
    mode_text = "فیلم (قابل پخش)" if mode == "playable" else "زیپ (فشرده)"
    return {"inline_keyboard": [
        [{"text": f"🎬 کیفیت: {quality}", "callback_data": "set_quality"}],
        [{"text": f"📦 حالت ارسال: {mode_text}", "callback_data": "set_mode"}],
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

# ---------- Message Handler ----------
def handle_search_command(chat_id: int, query: str) -> None:
    send_message(chat_id, f"🔎 در حال جستجوی «{query}»...")
    try:
        video_ids = search_youtube(query, limit=10)
    except Exception as e:
        logger.exception("search failed")
        send_message(chat_id, "⛔ خطا در جستجو.")
        return
    if not video_ids:
        send_message(chat_id, "❌ نتیجه‌ای یافت نشد.")
        return
    with state_lock:
        SEARCH_RESULTS[chat_id] = {i: vid for i, vid in enumerate(video_ids, 1)}
    for idx, vid in SEARCH_RESULTS[chat_id].items():
        thumb = download_thumbnail(vid, f"downloads/{chat_id}")
        if thumb:
            send_document(chat_id, thumb)
            safe_remove(thumb)
        send_message(chat_id, f"🔗 https://youtube.com/watch?v={vid}\n📥 /dl_{idx}\nℹ️ /info_{idx}")

def handle_link_download(chat_id: int, text: str) -> None:
    video_id = extract_video_id(text)
    if not video_id:
        send_message(chat_id, "❌ لینک یوتیوب نامعتبر است.")
        return
    threading.Thread(target=handle_download, args=(chat_id, video_id, 0), daemon=True).start()

def process_message(chat_id: int, text: str) -> None:
    if not is_admin(chat_id) and str(chat_id) not in load_users().get("users", {}):
        send_message(chat_id, "⛔ دسترسی ندارید.")
        return

    with state_lock:
        state = USER_STATE.pop(chat_id, None)

    if state == "awaiting_query" and not text.startswith("/"):
        handle_search_command(chat_id, text)
        return
    if state == "awaiting_url" and not text.startswith("/"):
        handle_link_download(chat_id, text)
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
                threading.Thread(target=handle_download, args=(chat_id, vid, idx), daemon=True).start()
            else:
                send_message(chat_id, "❌ کد نامعتبر. ابتدا جستجو کنید.")
        else:
            send_message(chat_id, "❌ فرمت نامعتبر. مثال: /dl_1")
    elif text == "/settings":
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s["quality"], s["send_mode"]))
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
    else:
        send_message(chat_id, "⚠️ فرمان نامعتبر. از منوی زیر استفاده کنید:", reply_markup=main_menu())

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
        edit_message_text(chat_id, message_id, "⚙️ تنظیمات:",
                          reply_markup=settings_menu(s["quality"], s["send_mode"]))
    elif data == "set_quality":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "🎬 کیفیت دانلود:",
                          reply_markup=quality_keyboard(s["quality"]))
    elif data.startswith("quality_"):
        q = data.split("_", 1)[1]
        update_user_setting(chat_id, "quality", q)
        edit_message_text(chat_id, message_id, f"✅ کیفیت روی {q} تنظیم شد.")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s["quality"], s["send_mode"]))
        toast_text = f"کیفیت روی {q} تنظیم شد."
    elif data == "set_mode":
        s = get_user_settings(chat_id)
        edit_message_text(chat_id, message_id, "📦 حالت ارسال:",
                          reply_markup=mode_keyboard(s["send_mode"]))
    elif data.startswith("mode_"):
        m = data.split("_", 1)[1]
        update_user_setting(chat_id, "send_mode", m)
        mode_str = "فیلم (قابل پخش)" if m == "playable" else "زیپ (فشرده)"
        edit_message_text(chat_id, message_id, f"✅ حالت ارسال: {mode_str}")
        s = get_user_settings(chat_id)
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_menu(s["quality"], s["send_mode"]))
        toast_text = f"حالت ارسال: {mode_str}"
    elif data == "main_menu":
        edit_message_text(chat_id, message_id, "🏠 منوی اصلی:", reply_markup=main_menu())
    # Always send one callback answer with the collected toast text
    answer_callback_query(callback_query_id, toast_text)

# ---------- Main ----------
def main():
    logger.info("YouTube Bale Bot v4.2 started")
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
