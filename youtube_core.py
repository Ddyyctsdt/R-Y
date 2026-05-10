"""
youtube_core.py - هسته اصلی عملیات یوتیوب (نسخه ۳)
شامل جستجوی مرورگری با Playwright، جستجوی API با scrapetube،
غنی‌سازی اطلاعات با چندین روش، دانلود با سه API پروکسی و موتور fallback.
"""

import os
import time
import re
import math
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from urllib.parse import quote_plus

# کتابخانه‌های داخلی پروژه
import settings
from utils import get_logger, download_file as utils_download_file, extract_video_id

# تلاش برای import کتابخانه‌های خارجی (در صورت عدم وجود، متدها None برمی‌گردانند)
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # مدیریت در متدهای مربوطه

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

_log = get_logger("youtube_core")

# ═══════════════════════════════════════════════════════════
# موتور fallback
# ═══════════════════════════════════════════════════════════
def run_with_fallback(
    chain: List[str],
    operation_func: Callable[[str, dict], Any],
    start_method: Optional[str] = None,
    **kwargs
) -> Tuple[Any, Optional[str]]:
    """
    متدهای یک زنجیره را به ترتیب اجرا می‌کند؛
    اولین موفقیت (نتیجه غیر None) را برمی‌گرداند.
    """
    if not chain:
        return None, None

    start_idx = 0
    if start_method:
        try:
            start_idx = chain.index(start_method)
        except ValueError:
            _log.warning(f"start_method '{start_method}' در زنجیره وجود ندارد. از ابتدا شروع می‌شود.")

    for method in chain[start_idx:]:
        _log.info(f"اجرای متد: {method}")
        try:
            result = operation_func(method, kwargs)
            if result is not None:
                if isinstance(result, list) and len(result) == 0:
                    _log.debug(f"متد {method} نتیجه خالی برگرداند، ادامه...")
                    continue
                _log.info(f"موفقیت با متد: {method}")
                return result, method
        except Exception as e:
            _log.error(f"خطای غیرمنتظره در متد {method}: {e}", exc_info=True)
    return None, None


# ═══════════════════════════════════════════════════════════
# توابع کمکی
# ═══════════════════════════════════════════════════════════
def _extract_video_id_from_url(url: str) -> Optional[str]:
    """استخراج شناسه ۱۱ نویسه‌ای از URLهای یوتیوب."""
    return extract_video_id(url)

def _find_downloaded_file(video_id: str, save_dir: str, ext: str = ".mp4") -> Optional[str]:
    """جستجوی فایل دانلودشده با پیشوند video_id."""
    if not os.path.isdir(save_dir):
        return None
    for fname in os.listdir(save_dir):
        if fname.startswith(video_id) and fname.endswith(ext):
            return os.path.join(save_dir, fname)
    return None


# ═══════════════════════════════════════════════════════════
# جستجوی مرورگری با Playwright
# ═══════════════════════════════════════════════════════════
def _search_browser(query: str, limit: int = 10) -> Optional[List[Dict]]:
    """
    جستجوی یوتیوب با مرورگر headless (Playwright).
    اطلاعات پایه را از صفحه نتایج استخراج می‌کند.
    """
    if sync_playwright is None:
        _log.warning("Playwright نصب نیست. جستجوی مرورگری غیرفعال است.")
        return None

    _log.info(f"شروع جستجوی مرورگری: {query}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = browser.new_context(
                user_agent=settings.USER_AGENT,
                viewport={"width": 390, "height": 844}  # شبیه‌سازی موبایل برای DOM ساده‌تر
            )
            page = context.new_page()
            try:
                # پیمایش به صفحه جستجو
                search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                # صبر برای بارگذاری نتایج
                page.wait_for_selector("ytd-video-renderer", timeout=15000)
                page.wait_for_timeout(2000)  # اطمینان از رندر کامل

                # استخراج اطلاعات از DOM با جاوااسکریپت
                results = page.evaluate("""(limit) => {
                    const items = document.querySelectorAll('ytd-video-renderer');
                    const videos = [];
                    for (const item of items) {
                        if (videos.length >= limit) break;
                        try {
                            const titleEl = item.querySelector('#video-title');
                            const title = titleEl ? (titleEl.getAttribute('aria-label') || titleEl.textContent.trim()) : '';
                            const href = titleEl ? titleEl.getAttribute('href') : '';
                            const videoId = href ? new URL(href, location.origin).searchParams.get('v') : '';

                            const thumbImg = item.querySelector('#img, yt-image img, img.yt-core-image');
                            const thumbnail = thumbImg ? (thumbImg.src || thumbImg.getAttribute('src')) : '';

                            const timeStatus = item.querySelector('ytd-thumbnail-overlay-time-status-renderer #text');
                            const duration = timeStatus ? timeStatus.textContent.trim() : '';

                            const channelEl = item.querySelector('ytd-channel-name a');
                            const uploader = channelEl ? channelEl.textContent.trim() : '';

                            if (videoId) {
                                videos.push({
                                    video_id: videoId,
                                    title: title,
                                    duration: duration,
                                    thumbnail_url: thumbnail,
                                    uploader: uploader
                                });
                            }
                        } catch(e) {}
                    }
                    return videos;
                }""", limit)

                _log.info(f"جستجوی مرورگری {len(results)} نتیجه برگرداند.")
                # افزودن فیلدهای خالی برای غنی‌سازی بعدی
                for r in results:
                    r.setdefault("view_count", None)
                    r.setdefault("like_count", None)
                    r.setdefault("description", None)
                return results
            finally:
                page.close()
                context.close()
                browser.close()
    except Exception as e:
        _log.error(f"search_browser failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# جستجوی API با scrapetube
# ═══════════════════════════════════════════════════════════
def _search_scrapetube(query: str, limit: int = 10) -> Optional[List[Dict]]:
    """جستجو با scrapetube (فقط شناسه ویدئوها)."""
    try:
        import scrapetube
    except ImportError:
        _log.warning("scrapetube library نصب نیست.")
        return None
    try:
        videos = scrapetube.get_search(query, limit=limit)
        results = []
        for v in videos:
            vid = v.get("videoId", "")
            results.append({
                "video_id": vid,
                "title": None,
                "duration": None,
                "thumbnail_url": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
                "uploader": None,
                "view_count": None,
                "like_count": None,
                "description": None
            })
        return results
    except Exception as e:
        _log.error(f"scrapetube search failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# متدهای غنی‌سازی اطلاعات ویدئو
# ═══════════════════════════════════════════════════════════
def _enrich_oembed(video_id: str) -> Optional[Dict]:
    """دریافت اطلاعات پایه از oEmbed."""
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        resp = requests.get(url, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title"),
            "uploader": data.get("author_name"),
            "thumbnail_url": data.get("thumbnail_url"),
        }
    except Exception as e:
        _log.error(f"oembed enrichment failed: {e}")
        return None

def _enrich_json_ld(video_id: str) -> Optional[Dict]:
    """استخراج structured data از صفحه ویدیو (بدون مرورگر)."""
    if BeautifulSoup is None:
        return None
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        resp = requests.get(url, headers={"User-Agent": settings.USER_AGENT}, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        script_tag = soup.find("script", type="application/ld+json")
        if not script_tag:
            return None
        data = json.loads(script_tag.string)
        # استخراج فیلدها
        result = {}
        if "name" in data:
            result["title"] = data["name"]
        if "description" in data:
            result["description"] = data["description"]
        if "thumbnailUrl" in data:
            result["thumbnail_url"] = data["thumbnailUrl"]
        if "uploadDate" in data:
            result["upload_date"] = data["uploadDate"]
        if "author" in data and "name" in data["author"]:
            result["uploader"] = data["author"]["name"]
        # آمار تعامل
        if "interactionStatistic" in data:
            for stat in data["interactionStatistic"]:
                if stat.get("interactionType") == "http://schema.org/WatchAction":
                    result.setdefault("view_count", stat.get("userInteractionCount"))
                elif stat.get("interactionType") == "http://schema.org/LikeAction":
                    result.setdefault("like_count", stat.get("userInteractionCount"))
        # تبدیل مدت زمان iso8601 به ثانیه
        if "duration" in data:
            dur = data["duration"]  # e.g. "PT1H2M10S"
            try:
                match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur)
                if match:
                    hours = int(match.group(1) or 0)
                    minutes = int(match.group(2) or 0)
                    seconds = int(match.group(3) or 0)
                    result["duration"] = hours * 3600 + minutes * 60 + seconds
            except Exception:
                pass
        return result if result else None
    except Exception as e:
        _log.error(f"JSON-LD enrichment failed: {e}")
        return None

def _enrich_dom_watch_page(video_id: str) -> Optional[Dict]:
    """بازکردن صفحه ویدیو با Playwright و استخراج اطلاعات (JSON-LD یا ytInitialPlayerResponse)."""
    if sync_playwright is None:
        return None
    _log.info(f"DOM watch page enrichment for {video_id}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = browser.new_context(user_agent=settings.USER_AGENT)
            page = context.new_page()
            try:
                page.goto(f"https://www.youtube.com/watch?v={video_id}", wait_until="domcontentloaded", timeout=30000)
                # تلاش برای دریافت داده‌های ساخت‌یافته
                try:
                    # ابتدا سعی کنیم JSON-LD را با جاوااسکریپت استخراج کنیم
                    ld_json = page.evaluate("""() => {
                        const el = document.querySelector('script[type="application/ld+json"]');
                        return el ? el.textContent : null;
                    }""")
                    if ld_json:
                        data = json.loads(ld_json)
                        # پردازش مشابه _enrich_json_ld
                        result = {}
                        if "name" in data:
                            result["title"] = data["name"]
                        if "description" in data:
                            result["description"] = data["description"]
                        if "thumbnailUrl" in data:
                            result["thumbnail_url"] = data["thumbnailUrl"]
                        if "uploadDate" in data:
                            result["upload_date"] = data["uploadDate"]
                        if "author" in data and "name" in data["author"]:
                            result["uploader"] = data["author"]["name"]
                        if "interactionStatistic" in data:
                            for stat in data["interactionStatistic"]:
                                if stat.get("interactionType") == "http://schema.org/WatchAction":
                                    result.setdefault("view_count", stat.get("userInteractionCount"))
                                elif stat.get("interactionType") == "http://schema.org/LikeAction":
                                    result.setdefault("like_count", stat.get("userInteractionCount"))
                        if "duration" in data:
                            dur = data["duration"]
                            try:
                                match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur)
                                if match:
                                    hours = int(match.group(1) or 0)
                                    minutes = int(match.group(2) or 0)
                                    seconds = int(match.group(3) or 0)
                                    result["duration"] = hours * 3600 + minutes * 60 + seconds
                            except Exception:
                                pass
                        if result:
                            return result
                except Exception:
                    pass

                # fallback: استخراج از ytInitialPlayerResponse
                try:
                    details = page.evaluate("""() => {
                        if (window.ytInitialPlayerResponse && window.ytInitialPlayerResponse.videoDetails) {
                            return window.ytInitialPlayerResponse.videoDetails;
                        }
                        return null;
                    }""")
                    if details:
                        result = {
                            "title": details.get("title"),
                            "duration": int(details.get("lengthSeconds", 0)) or None,
                            "uploader": details.get("author"),
                            "view_count": int(details.get("viewCount", 0)) or None,
                            "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                        }
                        return result
                except Exception:
                    pass
                return None
            finally:
                page.close()
                context.close()
                browser.close()
    except Exception as e:
        _log.error(f"DOM watch page enrichment failed: {e}")
        return None


def enrich_video_info(video_id: str, methods: Optional[List[str]] = None) -> Dict:
    """
    غنی‌سازی اطلاعات یک ویدئو با استفاده از روش‌های مختلف.
    خروجی: دیکشنری با کلیدهای video_id, title, duration, view_count, like_count,
            thumbnail_url, uploader, description, upload_date.
    """
    final_info = {"video_id": video_id}
    if methods is None:
        # استفاده از همه متدهای فعال تعریف‌شده در settings
        methods = list(settings.ENRICHMENT_METHODS.keys())
    # فیلدهایی که هر متد می‌تواند پر کند
    # ترتیب اولویت: oembed سریع است، json_ld کامل‌تر، dom_watch_page عمیق‌تر
    for method in methods:
        if method == "dom_search_page":
            continue  # فقط در صفحه جستجو کاربرد دارد
        enrich_func = {
            "oembed_enrich": _enrich_oembed,
            "json_ld": _enrich_json_ld,
            "dom_watch_page": _enrich_dom_watch_page,
        }.get(method)
        if not enrich_func:
            continue
        try:
            data = enrich_func(video_id)
            if data:
                # ادغام با حفظ داده‌های قبلی (متدهای بعدی در صورت وجود، بازنویسی نمی‌کنند مگر اینکه فیلد جدید خالی باشد)
                for key, value in data.items():
                    if value is not None:
                        final_info[key] = value
        except Exception as e:
            _log.error(f"{method} enrichment failed: {e}")
    return final_info


# ═══════════════════════════════════════════════════════════
# دانلود تصویر بند‌انگشتی
# ═══════════════════════════════════════════════════════════
def download_thumbnail(video_id: str, save_dir: str) -> Tuple[Optional[str], str]:
    """
    دانلود با کیفیت‌های maxresdefault, sddefault, hqdefault, mqdefault.
    بازگشت (مسیر فایل, "direct").
    """
    os.makedirs(save_dir, exist_ok=True)
    qualities = ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"]
    for quality in qualities:
        thumb_url = f"https://img.youtube.com/vi/{video_id}/{quality}"
        try:
            head = requests.head(thumb_url, timeout=5)
            if head.status_code == 200:
                fname = f"{video_id}_thumb.jpg"
                path = utils_download_file(thumb_url, save_dir, fname, timeout=30)
                if path:
                    return path, "direct"
        except Exception:
            continue
    return None, "direct"


# ═══════════════════════════════════════════════════════════
# متدهای دانلود
# ═══════════════════════════════════════════════════════════
def _download_hubytconvert(video_id: str, save_dir: str, quality: str = "720p") -> Optional[str]:
    """دانلود از hub.ytconvert.org (مطمئن‌ترین)."""
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
            _log.error("hubytconvert: statusUrl دریافت نشد.")
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
                return utils_download_file(dl_url, save_dir, f"{video_id}.mp4", timeout=180)
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
    """دانلود از cobalt.tools."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": settings.USER_AGENT
    }
    try:
        r = requests.post(
            "https://api.cobalt.tools/api/json",
            json={
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "videoQuality": quality.replace("p", ""),
                "filenameStyle": "basic"
            },
            headers=headers,
            timeout=settings.REQUEST_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status in ("tunnel", "redirect"):
            dl_url = data.get("url")
            if not dl_url:
                _log.error("cobalt: no download URL")
                return None
            return utils_download_file(dl_url, save_dir, f"{video_id}.mp4", timeout=180)
        elif status == "picker":
            picker = data.get("picker", [])
            for opt in picker:
                if opt.get("type") == "video":
                    dl_url = opt.get("url")
                    if dl_url:
                        return utils_download_file(dl_url, save_dir, f"{video_id}.mp4", timeout=180)
            _log.error("cobalt picker but no video option")
            return None
        else:
            _log.error(f"cobalt status: {status}, error: {data.get('error', '')}")
            return None
    except Exception as e:
        _log.error(f"cobalt download failed: {e}")
        return None


def _download_allmedia(video_id: str, save_dir: str, quality: str = "720p") -> Optional[str]:
    """دانلود از AllMedia Downloader API."""
    # تلاش با دو روش مختلف
    methods_to_try = [
        # روش GET
        lambda: requests.get(
            f"https://api.allmedia.app/dl?url=https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": settings.USER_AGENT},
            timeout=30
        ),
        # روش POST
        lambda: requests.post(
            "https://api.allmedia.app/api/json",
            json={"url": f"https://www.youtube.com/watch?v={video_id}", "quality": quality},
            headers={"User-Agent": settings.USER_AGENT, "Content-Type": "application/json"},
            timeout=30
        ),
    ]
    dl_url = None
    for try_func in methods_to_try:
        try:
            resp = try_func()
            resp.raise_for_status()
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            # استخراج لینک دانلود
            for key in ("download_url", "url", "link"):
                if key in data:
                    dl_url = data[key]
                    break
            if dl_url:
                break
        except Exception:
            continue

    if not dl_url:
        _log.error("allmedia: no download URL found")
        return None
    return utils_download_file(dl_url, save_dir, f"{video_id}.mp4", timeout=180)


# ═══════════════════════════════════════════════════════════
# توابع عمومی (Public API)
# ═══════════════════════════════════════════════════════════
def search_youtube(
    query: str,
    limit: int = 10,
    mode: str = "browser",
    enrich: bool = True
) -> Tuple[List[Dict], Optional[str]]:
    """
    جستجوی یوتیوب. حالت‌ها:
    - browser: استفاده از Playwright (پیش‌فرض)
    - api: استفاده از scrapetube + oembed
    """
    if mode == "browser":
        results = _search_browser(query, limit)
        if results:
            return results, "browser"
        else:
            # fallback به api در صورت شکست
            _log.warning("Browser search failed, falling back to API.")
            return search_youtube(query, limit, mode="api", enrich=enrich)
    elif mode == "api":
        # زنجیره فقط scrapetube
        chain = settings.DEFAULT_SEARCH_CHAIN
        search_map = {
            "scrapetube": _search_scrapetube
        }
        def op(method: str, kw: dict) -> Optional[List[Dict]]:
            func = search_map.get(method)
            if not func:
                return None
            return func(query=kw["query"], limit=kw["limit"])
        results, method_name = run_with_fallback(chain, op, query=query, limit=limit)
        if results and enrich:
            # غنی‌سازی هر نتیجه با oembed برای دریافت عنوان
            for r in results:
                if not r["title"]:
                    try:
                        oembed_data = _enrich_oembed(r["video_id"])
                        if oembed_data:
                            r["title"] = oembed_data.get("title", r["title"])
                            r["uploader"] = oembed_data.get("uploader", r["uploader"])
                            if oembed_data.get("thumbnail_url"):
                                r["thumbnail_url"] = oembed_data["thumbnail_url"]
                    except Exception:
                        pass
        return results, method_name
    else:
        _log.error(f"search mode ناشناخته: {mode}")
        return [], None

def get_video_info(
    video_id: str,
    chain: Optional[List[str]] = None,
    start_method: Optional[str] = None
) -> Tuple[Dict, Optional[str]]:
    """
    دریافت اطلاعات کامل ویدئو با غنی‌سازی.
    در صورت شکست، فقط oembed را امتحان می‌کند.
    """
    # تلاش برای غنی‌سازی کامل
    info = enrich_video_info(video_id)
    if info.get("title"):
        return info, "enrichment"
    # fallback به oembed خالی
    oembed_data = _enrich_oembed(video_id)
    if oembed_data:
        oembed_data["video_id"] = video_id
        return oembed_data, "oembed"
    return {}, None

def download_video(
    video_id: str,
    save_dir: str,
    chain: Optional[List[str]] = None,
    start_method: Optional[str] = None,
    quality: str = "720p"
) -> Tuple[Optional[str], Optional[str]]:
    """دانلود ویدئو با fallback روی متدهای دانلود."""
    os.makedirs(save_dir, exist_ok=True)
    if chain is None:
        chain = settings.DEFAULT_DOWNLOAD_CHAIN
    download_map = {
        "hubytconvert": lambda vid, dir: _download_hubytconvert(vid, dir, quality),
        "cobalt": lambda vid, dir: _download_cobalt(vid, dir, quality),
        "allmedia": lambda vid, dir: _download_allmedia(vid, dir, quality),
    }
    def op(method: str, kw: dict) -> Optional[str]:
        func = download_map.get(method)
        if not func:
            return None
        return func(video_id=kw["video_id"], save_dir=kw["save_dir"])
    return run_with_fallback(chain, op, start_method, video_id=video_id, save_dir=save_dir)
