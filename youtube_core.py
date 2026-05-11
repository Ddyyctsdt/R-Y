"""
youtube_core.py – هستهٔ عملیات یوتیوب (نسخهٔ ۳)
جستجوی سریع با scrapetube، دریافت اطلاعات کامل از صفحه تماشا (Playwright)،
دانلود ویدیو، عملیات کانال و متدهای fallback
"""

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import settings
from utils import get_logger, download_file

_log = get_logger("youtube_core")


# ──────────────────────────── ابزارهای کمکی ────────────────────────────

def _extract_video_id_from_url(url: str) -> Optional[str]:
    pattern = r'(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&/#]|$)'
    parsed = requests.utils.urlparse(url)
    if parsed.netloc in ('youtu.be', 'www.youtu.be'):
        vid = parsed.path.lstrip('/')
        if re.match(r'^[0-9A-Za-z_-]{11}$', vid):
            return vid
    match = re.search(pattern, url)
    return match.group(1) if match else None


def run_with_fallback(
    chain: List[str],
    operation_func: Callable[[str, Dict[str, Any]], Any],
    start_method: Optional[str] = None,
    **kwargs
) -> Tuple[Any, Optional[str]]:
    """
    اجرای یک عملیات روی زنجیره‌ای از متدها تا اولین موفقیت.
    خروجی: (نتیجه, نام متد موفق) یا (None, None)
    """
    if start_method:
        try:
            idx = chain.index(start_method)
            chain = chain[idx:]
        except ValueError:
            _log.warning(f"start_method={start_method} در زنجیره نیست، از اول شروع می‌شود.")

    for method in chain:
        _log.info(f"تلاش با متد: {method}")
        try:
            result = operation_func(method, kwargs)
            if result is not None:
                _log.info(f"متد {method} موفق بود.")
                return result, method
        except Exception as e:
            _log.warning(f"متد {method} شکست خورد: {e}")
    _log.error("تمام متدهای زنجیره ناموفق بودند.")
    return None, None


# ──────────────────────────── جستجوی سریع (فقط scrapetube) ─────────────────

def _search_scrapetube(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    """جستجو با کتابخانه scrapetube (فقط شناسه برمی‌گرداند)"""
    try:
        import scrapetube
        videos = scrapetube.get_search(query, limit=limit)
        results = []
        for v in videos:
            vid = v.get("videoId") if isinstance(v, dict) else getattr(v, "videoId", None)
            if vid:
                results.append({
                    "video_id": vid,
                    "title": None,
                    "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "duration": None,
                    "uploader": None,
                    "views": None,
                    "uploaded": None,
                })
        return results if results else None
    except Exception:
        return None


# ──────────────────────────── دریافت اطلاعات تکمیلی ──────────────────────

def _enrich_oembed(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"https://www.youtube.com/oembed?url=https://youtube.com/watch?v={video_id}&format=json"
        resp = requests.get(url, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title"),
            "author": data.get("author_name"),
            "thumbnail": data.get("thumbnail_url"),
        }
    except Exception:
        return None


def scrape_watch_page(video_id: str) -> Optional[Dict[str, Any]]:
    """
    باز کردن صفحه تماشای یوتیوب با Playwright (نمونه جدید) و استخراج کامل داده‌ها.
    """
    _log.info(f"scrape_watch_page: {video_id}")
    pw = None
    browser = None
    context = None
    page = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=settings.USER_AGENT,
            viewport={"width": 390, "height": 844}
        )
        page = context.new_page()
        page.goto(f"https://www.youtube.com/watch?v={video_id}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("h1 yt-formatted-string", timeout=15000)

        # کلیک روی "Show more" برای دریافت توضیحات کامل
        try:
            expand_btn = page.query_selector("#expand, #description-inline-expander button")
            if expand_btn:
                expand_btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        js_code = """
        () => {
            const titleEl = document.querySelector('h1 yt-formatted-string');
            const title = titleEl ? titleEl.textContent.trim() : '';
            const uploaderEl = document.querySelector('#owner yt-formatted-string a, ytd-channel-name a');
            const uploader = uploaderEl ? uploaderEl.textContent.trim() : '';
            const viewsEl = document.querySelector('#info .view-count, #count .view-count');
            const views = viewsEl ? viewsEl.textContent.trim() : '';
            const duration = (() => {
                try {
                    const playerResponse = JSON.parse(document.querySelector('ytd-watch-flexy').getAttribute('ytd-watch-flexy'));
                    return playerResponse?.args?.raw_player_response?.videoDetails?.lengthSeconds || '';
                } catch(e) {}
                return '';
            })();
            const descEl = document.querySelector('#description-inline-expander yt-formatted-string, #description yt-formatted-string');
            const description = descEl ? descEl.textContent.trim() : '';
            const likesEl = document.querySelector('#top-level-buttons-computed ytd-toggle-button-renderer:first-child #text, #segmented-like-button yt-button-shape button div[aria-label]');
            const likes = likesEl ? likesEl.textContent.trim() : '';
            const thumbUrl = document.querySelector('link[rel="shortcut icon"]')?.href || '';
            return {
                title, uploader, views, duration, description, likes, thumbUrl
            };
        }
        """
        data = page.evaluate(js_code)
        return {
            "video_id": video_id,
            "title": data.get("title") or None,
            "duration": data.get("duration") or None,
            "view_count": data.get("views") or None,
            "like_count": data.get("likes") or None,
            "thumbnail_url": data.get("thumbUrl") or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            "uploader": data.get("uploader") or None,
            "description": data.get("description") or "",   # ← دیگر None نیست
        }
    except Exception as e:
        _log.warning(f"scrape_watch_page برای {video_id} شکست خورد: {e}")
        return None
    finally:
        if page:
            page.close()
        if context:
            context.close()
        if browser:
            browser.close()
        if pw:
            pw.stop()


def enrich_video_info(video_id: str) -> Dict[str, Any]:
    """تلاش برای دریافت اطلاعات کامل یک ویدیو با اولویت Playwright"""
    info = scrape_watch_page(video_id)
    if info and info.get("title"):
        return info
    # fallback به oembed
    oembed = _enrich_oembed(video_id)
    if oembed:
        return {
            "video_id": video_id,
            "title": oembed.get("title"),
            "author": oembed.get("author"),
            "thumbnail_url": oembed.get("thumbnail"),
            "duration": None,
            "view_count": None,
            "like_count": None,
            "uploader": oembed.get("author"),
            "description": "",             # ← اضافه شد
        }
    return {}


# ──────────────────────────── توابع عمومی جستجو و اطلاعات ────────────────

def search_youtube(query: str, limit: int = 10, mode: str = "api") -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    جستجوی سریع ویدیوها فقط با scrapetube.
    mode برای سازگاری باقی مانده ولی فقط حالت api پشتیبانی می‌شود.
    """
    results = _search_scrapetube(query, limit)
    if results:
        return results, "scrapetube"
    return [], None


def get_video_info(video_id: str) -> Tuple[Dict[str, Any], str]:
    """دریافت اطلاعات کامل ویدیو از صفحه تماشا (Playwright)"""
    info = enrich_video_info(video_id)
    # تطبیق با ساختار قدیمی
    result = {
        "title": info.get("title"),
        "author": info.get("author"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail_url") or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        "description": info.get("description") or "",   # ← تضمین رشته بودن
    }
    # اگر خیلی خالی بود، fallback دیگه
    if not result.get("title"):
        oembed = _enrich_oembed(video_id)
        if oembed:
            result["title"] = oembed.get("title")
    return result, "playwright_scrape"


# ──────────────────────────── دانلود ویدیو ──────────────────────────────

def _download_hubytconvert(video_id: str, save_dir: str, quality: str = "720p") -> Optional[str]:
    """
    دانلود از طریق hub.ytconvert.org (روش اصلی).
    🟢 اصلاح‌شده: از درخواست HTTP مستقیم استفاده می‌کند.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": settings.USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://media.ytmp3.gg/",
        "Origin": "https://media.ytmp3.gg",
        "Content-Type": "application/json"
    }
    try:
        s = requests.Session()
        s.get("https://media.ytmp3.gg/", headers={"User-Agent": settings.USER_AGENT}, timeout=10)
    except Exception:
        pass

    try:
        resp = s.post(
            "https://hub.ytconvert.org/api/download",
            json={
                "url": url,
                "os": "linux",
                "output": {"type": "video", "format": "mp4", "quality": quality}
            },
            headers=headers,
            timeout=settings.REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        status_url = data.get("statusUrl")
        if not status_url:
            _log.error("hubytconvert: statusUrl not returned")
            return None
    except Exception as e:
        _log.error(f"hubytconvert init failed: {e}")
        return None

    for attempt in range(1, 91):
        try:
            status_resp = s.get(status_url, headers=headers, timeout=10)
            status_resp.raise_for_status()
            status_data = status_resp.json()
            if status_data.get("status") == "completed":
                dl_url = status_data.get("downloadUrl")
                if not dl_url:
                    _log.error("hubytconvert: completed but no downloadUrl")
                    return None
                return download_file(dl_url, save_dir, f"{video_id}.mp4", timeout=180)
            elif status_data.get("status") == "error":
                _log.error(f"hubytconvert error: {status_data.get('message', 'unknown')}")
                return None
            time.sleep(2)
        except Exception as e:
            _log.error(f"hubytconvert polling error: {e}")
            return None
    _log.error("hubytconvert timeout")
    return None


def _download_cobalt(video_id: str, save_dir: str, quality: str = "1080p") -> Optional[str]:
    """دانلود از طریق cobalt.tools"""
    try:
        url = "https://api.cobalt.tools/api/json"
        payload = {
            "url": f"https://youtube.com/watch?v={video_id}",
            "vCodec": "h264",
            "aFormat": "mp3",
        }
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT*2)
        resp.raise_for_status()
        data = resp.json()
        stream_url = data.get("url") or data.get("streamUrl")
        if not stream_url:
            return None
        return download_file(stream_url, save_dir, filename=f"{video_id}.mp4", timeout=120)
    except Exception:
        return None


def _download_allmedia(video_id: str, save_dir: str, quality: str = "720p") -> Optional[str]:
    """
    AllMedia API فعلاً در دسترس نیست – به متد بعدی می‌رود.
    🟢 اصلاح‌شده: بلافاصله None برمی‌گرداند تا زنجیره ادامه یابد.
    """
    _log.warning("allmedia: API unavailable, skipping.")
    return None


def download_video(video_id: str, save_dir: str, quality: str = "1080p") -> Tuple[Optional[str], str]:
    """دانلود ویدیو با چندین روش پشتیبان"""
    os.makedirs(save_dir, exist_ok=True)
    chain = ["hubytconvert", "cobalt", "allmedia"]
    def _dop(method: str, kwargs: dict) -> Optional[str]:
        if method == "hubytconvert":
            return _download_hubytconvert(video_id, save_dir, quality)
        elif method == "cobalt":
            return _download_cobalt(video_id, save_dir, quality)
        elif method == "allmedia":
            return _download_allmedia(video_id, save_dir, quality)
        return None
    path, method = run_with_fallback(chain, _dop)
    return path, method


def download_thumbnail(video_id: str, save_dir: str) -> Tuple[Optional[str], str]:
    """دانلود تامنیل از یوتیوب (چهار کیفیت)"""
    base = "https://img.youtube.com/vi/{}/{}"
    variants = ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"]
    for var in variants:
        url = base.format(video_id, var)
        try:
            head = requests.head(url, timeout=5)
            if head.status_code == 200:
                path = download_file(url, save_dir, filename=f"{video_id}.jpg", timeout=20)
                if path:
                    return path, "direct"
        except Exception:
            continue
    return None, "direct"


# ════════════════════ عملیات کانال ════════════════════

def get_channel_videos(channel_id: str, sort_by: str = "newest", max_results: int = 50) -> Optional[List[Dict[str, Any]]]:
    """
    دریافت لیست ویدیوهای یک کانال یوتیوب.
    """
    sort_suffix = {"newest": "", "oldest": "?sort=da", "popular": "?sort=p"}.get(sort_by, "")
    if channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}/videos{sort_suffix}"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/videos{sort_suffix}"

    pw = None
    browser = None
    page = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(settings.SEARCH_TIMEOUT * 1000)
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector('ytd-rich-grid-media, ytd-video-renderer', timeout=15000)
        except PlaywrightTimeout:
            page.wait_for_timeout(5000)

        collected = []
        no_new_count = 0
        while len(collected) < max_results and no_new_count < 2:
            previous_count = len(page.query_selector_all('ytd-rich-grid-media, ytd-video-renderer'))
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            time.sleep(2)
            current_count = len(page.query_selector_all('ytd-rich-grid-media, ytd-video-renderer'))
            if current_count <= previous_count:
                no_new_count += 1
            else:
                no_new_count = 0

        js_code = """
        () => {
            const items = document.querySelectorAll('ytd-rich-grid-media, ytd-video-renderer');
            const results = [];
            for (const item of items) {
                const titleEl = item.querySelector('#video-title');
                const title = titleEl ? titleEl.textContent.trim() : '';
                const linkEl = titleEl ? titleEl.closest('a') : null;
                const href = linkEl ? linkEl.getAttribute('href') : '';
                const videoId = href.split('?v=')[1]?.split('&')[0] || '';
                const thumbEl = item.querySelector('img.yt-core-image');
                const thumb = thumbEl ? thumbEl.getAttribute('src') : '';
                const durationEl = item.querySelector('ytd-thumbnail-overlay-time-status-renderer span');
                const duration = durationEl ? durationEl.textContent.trim() : '';
                const channelEl = item.querySelector('ytd-channel-name a');
                const channel = channelEl ? channelEl.textContent.trim() : '';
                const meta = item.querySelector('#metadata-line');
                const metaSpans = meta ? meta.querySelectorAll('span') : [];
                const views = metaSpans.length >= 1 ? metaSpans[0].textContent.trim() : '';
                const uploaded = metaSpans.length >= 2 ? metaSpans[1].textContent.trim() : '';
                results.push({ videoId, title, thumbnail, duration, channel, views, uploaded });
            }
            return results;
        }
        """
        dom = page.evaluate(js_code)
        for r in dom:
            vid = r.get("videoId")
            if not vid or len(vid) != 11:
                continue
            collected.append({
                "video_id": vid,
                "title": r.get("title"),
                "thumbnail_url": r.get("thumbnail"),
                "duration": r.get("duration"),
                "uploader": r.get("channel"),
                "views": r.get("views"),
                "uploaded": r.get("uploaded"),
            })
            if len(collected) >= max_results:
                break
        return collected if collected else None
    except Exception as e:
        _log.error(f"خطا در get_channel_videos: {e}")
        return None
    finally:
        if page:
            page.close()
        if browser:
            browser.close()
        if pw:
            pw.stop()


def get_channel_info(channel_id: str) -> Optional[Dict[str, Any]]:
    """
    دریافت اطلاعات پایهٔ کانال (نام، تعداد دنبال‌کننده، توضیحات).
    """
    if channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}/about"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/about"

    pw = None
    browser = None
    page = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=settings.REQUEST_TIMEOUT*1000)
        html = page.content()
        match = re.search(r'var\s+ytInitialData\s*=\s*(\{.+?\});', html, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            header = data.get("header", {}).get("c4TabbedHeaderRenderer", {})
            metadata = data.get("metadata", {}).get("channelMetadataRenderer", {})
            name = metadata.get("title") or header.get("title")
            subs = header.get("subscriberCountText", {}).get("simpleText", "")
            thumbnail = header.get("avatar", {}).get("thumbnails", [{}])[0].get("url")
            desc = metadata.get("description", "")
            return {
                "name": name,
                "subscriber_count": subs,
                "thumbnail": thumbnail,
                "description": desc,
            }
        else:
            name = page.title().replace(" - YouTube", "").strip()
            return {
                "name": name,
                "subscriber_count": None,
                "thumbnail": None,
                "description": None,
            }
    except Exception as e:
        _log.error(f"خطا در get_channel_info: {e}")
        return None
    finally:
        if page:
            page.close()
        if browser:
            browser.close()
        if pw:
            pw.stop()
