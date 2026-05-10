"""
youtube_core.py – هستهٔ عملیات یوتیوب (نسخهٔ ۳)
جستجوی مرورگر، دریافت اطلاعات، دانلود ویدیو، پیمایش کانال و متدهای fallback
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

# ──────────────────────────── مدیریت مرورگر ────────────────────────────
_browser = None
_playwright = None

def _get_browser():
    """راه‌اندازی یا بازیابی نمونهٔ مشترک مرورگر (Chromium)"""
    global _browser, _playwright
    if _browser is None or not _browser.is_connected():
        if _playwright is None:
            _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    return _browser

def _close_browser():
    global _browser, _playwright
    if _browser:
        _browser.close()
        _browser = None
    if _playwright:
        _playwright.stop()
        _playwright = None

# ──────────────────────────── ابزارهای کمکی ────────────────────────────

def _parse_innertube_renderer(data: dict, limit: int = 10) -> List[Dict[str, Any]]:
    """تبدیل rendererهای innertube/ytInitialData به لیست استاندارد"""
    results = []
    contents = []
    try:
        primary = (data.get("contents", {})
                       .get("twoColumnSearchResultsRenderer", {})
                       .get("primaryContents", {})
                       .get("sectionListRenderer", {})
                       .get("contents", []))
        for section in primary:
            items = section.get("itemSectionRenderer", {}).get("contents", [])
            contents.extend(items)
        if not contents:
            rich = (data.get("contents", {})
                        .get("twoColumnSearchResultsRenderer", {})
                        .get("primaryContents", {})
                        .get("richGridRenderer", {})
                        .get("contents", []))
            for item in rich:
                video = item.get("richItemRenderer", {}).get("content", {}).get("videoRenderer")
                if video:
                    contents.append({"videoRenderer": video})

        for item in contents:
            if len(results) >= limit:
                break
            video = item.get("videoRenderer")
            if not video:
                continue
            vid = video.get("videoId")
            if not vid:
                continue
            title = "".join(run.get("text", "") for run in video.get("title", {}).get("runs", []))
            thumbs = video.get("thumbnail", {}).get("thumbnails", [])
            thumb_url = thumbs[0]["url"] if thumbs else None
            duration = video.get("lengthText", {}).get("simpleText", "")
            uploader = video.get("ownerText", {}).get("runs", [{"text": ""}])[0].get("text", "")
            views = video.get("viewCountText", {}).get("simpleText") or video.get("shortViewCountText", {}).get("simpleText", "")
            uploaded = video.get("publishedTimeText", {}).get("simpleText", "")

            results.append({
                "video_id": vid,
                "title": title or None,
                "thumbnail_url": thumb_url,
                "duration": duration,
                "uploader": uploader or None,
                "views": views or None,
                "uploaded": uploaded or None,
            })
    except Exception as e:
        _log.warning(f"خطا در تجزیه renderer: {e}")
    return results


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


# ──────────────────────────── جستجوی مرورگر (بهبودیافته) ─────────────────────

def _search_browser(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    """
    جستجوی یوتیوب با Playwright و استخراج داده‌ها ابتدا از ytInitialData،
    سپس با DOM به‌عنوان پشتیبان.
    """
    browser = _get_browser()
    page = None
    try:
        page = browser.new_page()
        page.set_default_timeout(settings.SEARCH_TIMEOUT * 1000)

        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector('ytd-video-renderer', timeout=15000)
        except PlaywrightTimeout:
            _log.warning("زمان انتظار برای ytd-video-renderer تمام شد، ۵ ثانیه توقف اضافی...")
            page.wait_for_timeout(5000)

        html = page.content()
        match = re.search(r'var\s+ytInitialData\s*=\s*(\{.+?\});', html, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                results = _parse_innertube_renderer(data, limit)
                if results:
                    return results
            except Exception as e:
                _log.warning(f"پارسه ytInitialData ناموفق: {e}")

        # روش DOM (پشتیبان)
        js_code = """
        () => {
            const items = document.querySelectorAll('ytd-video-renderer, ytd-rich-item-renderer');
            const results = [];
            for (const item of items) {
                const titleEl = item.querySelector('#video-title, .ytd-video-renderer #video-title');
                const linkEl = titleEl ? titleEl.querySelector('a') : null;
                const href = linkEl ? linkEl.getAttribute('href') : '';
                const videoId = href.split('?v=')[1]?.split('&')[0] || '';
                const title = titleEl ? titleEl.textContent.trim() : '';
                const thumbEl = item.querySelector('img.yt-core-image, #img');
                const thumb = thumbEl ? thumbEl.getAttribute('src') : '';
                const durationEl = item.querySelector('ytd-thumbnail-overlay-time-status-renderer span, .ytd-thumbnail-overlay-time-status-renderer');
                const duration = durationEl ? durationEl.textContent.trim() : '';
                const channelEl = item.querySelector('ytd-channel-name a, .ytd-channel-name a');
                const channel = channelEl ? channelEl.textContent.trim() : '';
                const metaLine = item.querySelector('#metadata-line, .inline-metadata');
                const metaSpans = metaLine ? metaLine.querySelectorAll('span') : [];
                const views = metaSpans.length >= 1 ? metaSpans[0].textContent.trim() : '';
                const uploaded = metaSpans.length >= 2 ? metaSpans[1].textContent.trim() : '';
                results.push({videoId, title, thumbnail, duration, channel, views, uploaded});
            }
            return results;
        }
        """
        dom_results = page.evaluate(js_code)
        formatted = []
        for r in dom_results:
            vid = r.get("videoId")
            if not vid or len(vid) != 11:
                continue
            formatted.append({
                "video_id": vid,
                "title": r.get("title"),
                "thumbnail_url": r.get("thumbnail"),
                "duration": r.get("duration"),
                "uploader": r.get("channel"),
                "views": r.get("views"),
                "uploaded": r.get("uploaded"),
            })
            if len(formatted) >= limit:
                break
        return formatted if formatted else None
    except Exception as e:
        _log.warning(f"خطا در جستجوی مرورگر: {e}")
        return None
    finally:
        if page:
            page.close()


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


def _enrich_json_ld(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        headers = {"User-Agent": settings.USER_AGENT}
        resp = requests.get(url, timeout=settings.REQUEST_TIMEOUT, headers=headers)
        resp.raise_for_status()
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', resp.text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(1))
        for item in data if isinstance(data, list) else [data]:
            if item.get("@type") == "VideoObject":
                return {
                    "title": item.get("name"),
                    "duration": item.get("duration"),
                    "views": item.get("interactionStatistic"),
                    "thumbnail": item.get("thumbnailUrl"),
                    "author": item.get("author", {}).get("name") if isinstance(item.get("author"), dict) else None,
                }
        return None
    except Exception:
        return None


def _enrich_dom_watch_page(video_id: str) -> Optional[Dict[str, Any]]:
    """استخراج اطلاعات از صفحه تماشا با Playwright"""
    browser = _get_browser()
    page = None
    try:
        page = browser.new_page()
        page.goto(f"https://www.youtube.com/watch?v={video_id}", wait_until="domcontentloaded", timeout=settings.REQUEST_TIMEOUT*1000)
        page.wait_for_selector("h1 yt-formatted-string", timeout=10000)
        js = """
        () => {
            const title = document.querySelector('h1 yt-formatted-string')?.textContent || '';
            const owner = document.querySelector('#owner yt-formatted-string a')?.textContent || '';
            const views = document.querySelector('#count .view-count')?.textContent || '';
            const date = document.querySelector('#info-strings yt-formatted-string')?.textContent || '';
            return {title, owner, views, date};
        }
        """
        data = page.evaluate(js)
        return {
            "title": data.get("title"),
            "author": data.get("owner"),
            "views": data.get("views"),
            "uploaded": data.get("date"),
        }
    except Exception as e:
        _log.warning(f"enrich_dom_watch_page: {e}")
        return None
    finally:
        if page:
            page.close()


def enrich_video_info(video_id: str) -> Dict[str, Any]:
    """تلاش برای پر کردن اطلاعات یک ویدیو با چند روش"""
    info = {}
    oembed = _enrich_oembed(video_id)
    if oembed:
        info.update(oembed)
    jld = _enrich_json_ld(video_id)
    if jld:
        info.update({k: v for k, v in jld.items() if v is not None})
    dom = _enrich_dom_watch_page(video_id)
    if dom:
        info.update({k: v for k, v in dom.items() if v is not None})
    return info


# ──────────────────────────── توابع عمومی جستجو و اطلاعات ────────────────

def search_youtube(query: str, limit: int = 10, mode: str = "browser") -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    جستجوی ویدیو. mode: 'browser' یا 'api' (scrapetube یا سایر)
    """
    if mode == "api":
        results = _search_scrapetube(query, limit)
        return (results if results else [], "scrapetube")
    # browser
    chain = ["browser", "scrapetube"]
    def _op(method: str, kwargs: dict) -> Optional[List[Dict]]:
        q = kwargs["query"]
        lim = kwargs["limit"]
        if method == "browser":
            return _search_browser(q, lim)
        elif method == "scrapetube":
            return _search_scrapetube(q, lim)
        return None
    results, method = run_with_fallback(chain, _op, query=query, limit=limit)
    return (results if results else [], method if results else None)


def get_video_info(video_id: str) -> Tuple[Dict[str, Any], str]:
    """دریافت اطلاعات کامل ویدیو (با enrich)"""
    info = {
        "title": None,
        "author": None,
        "duration": None,
        "view_count": None,
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        "description": None,
    }
    enriched = enrich_video_info(video_id)
    info.update(enriched)
    if not info.get("title"):
        oembed = _enrich_oembed(video_id)
        if oembed:
            info["title"] = oembed.get("title")
    method = "enrich"
    return info, method


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


# ════════════════════ عملیات کانال (جدید) ════════════════════

def get_channel_videos(channel_id: str, sort_by: str = "newest", max_results: int = 50) -> Optional[List[Dict[str, Any]]]:
    """
    دریافت لیست ویدیوهای یک کانال یوتیوب.
    🟢 اصلاح‌شده: استخراج فقط از DOM انجام می‌شود تا ویدیوهای بارگذاری‌شده با اسکرول پوشش داده شوند.
    """
    sort_suffix = {"newest": "", "oldest": "?sort=da", "popular": "?sort=p"}.get(sort_by, "")
    if channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}/videos{sort_suffix}"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/videos{sort_suffix}"

    browser = _get_browser()
    page = None
    try:
        page = browser.new_page()
        page.set_default_timeout(settings.SEARCH_TIMEOUT * 1000)
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector('ytd-rich-grid-media, ytd-video-renderer', timeout=15000)
        except PlaywrightTimeout:
            page.wait_for_timeout(5000)

        # اسکرول برای بارگذاری بیشتر
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

        # استخراج DOM (روش اصلی)
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


def get_channel_info(channel_id: str) -> Optional[Dict[str, Any]]:
    """
    دریافت اطلاعات پایهٔ کانال (نام، تعداد دنبال‌کننده، توضیحات).
    """
    if channel_id.startswith("@"):
        url = f"https://www.youtube.com/{channel_id}/about"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/about"

    browser = _get_browser()
    page = None
    try:
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
