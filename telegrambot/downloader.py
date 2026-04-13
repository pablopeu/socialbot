import http.cookiejar
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

import httpx
import instaloader
import yt_dlp

COOKIES_PATH = os.path.join(os.path.dirname(__file__), "cookies.txt")
THREADS_COOKIES_PATH = os.path.join(os.path.dirname(__file__), "threads_cookies.txt")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.instagram.com/",
}

YDL_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Mode": "navigate",
}

MIME_MAP = {
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp",
}

MAX_TG_BYTES = 50 * 1024 * 1024  # 50 MB Telegram bot limit


def is_instagram(url: str) -> bool:
    return "instagram.com" in url or "instagr.am" in url


def is_twitter(url: str) -> bool:
    return "twitter.com" in url or "x.com" in url


def is_facebook(url: str) -> bool:
    return "facebook.com" in url or "fb.watch" in url


def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url or "vm.tiktok.com" in url


def is_threads(url: str) -> bool:
    return "threads.net" in url or "threads.com" in url


def _is_direct(f: dict) -> bool:
    return (
        f.get("protocol", "") not in ("m3u8", "m3u8_native", "m3u8_local", "dash")
        and not (f.get("url") or "").endswith(".m3u8")
        and not (f.get("url") or "").endswith(".mpd")
    )


def _ig_shortcode_from_url(url: str) -> Optional[str]:
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _load_cookies_into_session(session, cookie_path: str):
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(cookie_path, ignore_discard=True, ignore_expires=True)
    session.cookies.update(jar)


def _ig_cdn_items(url: str) -> Optional[list]:
    """Use instaloader to get CDN URLs for an Instagram post."""
    shortcode = _ig_shortcode_from_url(url)
    if not shortcode:
        return None

    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )

    if os.path.exists(COOKIES_PATH):
        _load_cookies_into_session(L.context._session, COOKIES_PATH)

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception:
        return None

    items = []
    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if node.is_video:
                items.append({"type": "video", "cdn_url": node.video_url})
            else:
                items.append({"type": "image", "cdn_url": node.display_url})
    elif post.is_video:
        items.append({"type": "video", "cdn_url": post.video_url})
    else:
        items.append({"type": "image", "cdn_url": post.url})

    return items if items else None


def _download_cdn_url(cdn_url: str, item_type: str) -> Optional[dict]:
    """Download a CDN URL to a temp file and return {type, path, mime}."""
    suffix = ".mp4" if item_type == "video" else ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="ig_cdn_")
    os.close(fd)
    try:
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            with client.stream("GET", cdn_url, headers=BROWSER_HEADERS) as r:
                if r.status_code != 200:
                    os.unlink(tmp_path)
                    return None
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
        mime = "video/mp4" if item_type == "video" else "image/jpeg"
        return {"type": item_type, "path": tmp_path, "mime": mime}
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        return None


def _threads_scrape(url: str) -> Optional[list]:
    """Extract media from a public Threads post by scraping the page HTML."""
    fetch_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            r = client.get(url, headers=fetch_headers)
        if r.status_code != 200:
            logger.error(f"Threads page HTTP {r.status_code}")
            return None
        html = r.text.replace("\\u0026", "&").replace("\\u003C", "<").replace("\\u003E", ">")
    except Exception as e:
        logger.error(f"Threads fetch error: {e}")
        return None

    # Video: look for video_versions array
    m = re.search(r'"video_versions"\s*:\s*\[\s*\{[^]]*?"url"\s*:\s*"(https://[^"]+)"', html)
    if m:
        logger.info("Threads scrape: found video_versions")
        return [{"type": "video", "cdn_url": m.group(1)}]

    # Video: og:video meta tag
    m = re.search(r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        logger.info("Threads scrape: found og:video")
        return [{"type": "video", "cdn_url": m.group(1)}]

    # Image: og:image meta tag
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        logger.info("Threads scrape: found og:image")
        return [{"type": "image", "cdn_url": m.group(1)}]

    logger.error("Threads scrape: no media found in page HTML")
    return None


def download_media(url: str) -> list:
    """
    Download all media from a URL.
    Returns list of: {'type': 'video'|'image', 'path': str, 'mime': str}
    Caller is responsible for deleting the temp files.
    """
    tmp_dir = f"/tmp/bot_{uuid.uuid4().hex}"
    os.makedirs(tmp_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(playlist_index)03d_%(id)s.%(ext)s"),
        "format": "best[ext=mp4][filesize<50M]/best[filesize<50M]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HTTP_HEADERS,
    }

    # Pick the right cookies file for the platform
    if is_threads(url) and os.path.exists(THREADS_COOKIES_PATH):
        ydl_opts["cookiefile"] = THREADS_COOKIES_PATH
    elif os.path.exists(COOKIES_PATH):
        ydl_opts["cookiefile"] = COOKIES_PATH

    ytdlp_ok = False
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        files = sorted([
            f for f in os.listdir(tmp_dir)
            if not f.endswith((".part", ".ytdl"))
        ])
        ytdlp_ok = bool(files)
    except Exception as e:
        logger.error(f"yt-dlp error for {url}: {e}")

    if ytdlp_ok:
        results = []
        for fname in sorted([f for f in os.listdir(tmp_dir) if not f.endswith((".part", ".ytdl"))]):
            fpath = os.path.join(tmp_dir, fname)
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            mime = MIME_MAP.get(ext, "application/octet-stream")
            ftype = "video" if "video" in mime else "image"
            results.append({"type": ftype, "path": fpath, "mime": mime, "_dir": tmp_dir})
        return results

    shutil.rmtree(tmp_dir, ignore_errors=True)

    # gallery-dl fallback for Threads
    if is_threads(url):
        tmp_dir = f"/tmp/bot_{uuid.uuid4().hex}"
        os.makedirs(tmp_dir, exist_ok=True)
        cmd = ["python3", "-m", "gallery_dl", "--dest", tmp_dir, "--filename", "{num:>02}.{extension}", url]
        if os.path.exists(THREADS_COOKIES_PATH):
            cmd += ["--cookies", THREADS_COOKIES_PATH]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.error(f"gallery-dl error: {result.stderr.strip()}")
            else:
                files = sorted([
                    f for f in os.listdir(tmp_dir)
                    if not f.endswith((".part", ".ytdl"))
                ])
                if files:
                    results = []
                    for fname in files:
                        fpath = os.path.join(tmp_dir, fname)
                        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                        mime = MIME_MAP.get(ext, "application/octet-stream")
                        ftype = "video" if "video" in mime else "image"
                        results.append({"type": ftype, "path": fpath, "mime": mime, "_dir": tmp_dir})
                    return results
        except Exception as e:
            logger.error(f"gallery-dl exception: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # httpx scrape fallback for Threads public posts
        cdn_items = _threads_scrape(url)
        if cdn_items:
            results = []
            for item in cdn_items:
                downloaded = _download_cdn_url(item["cdn_url"], item["type"])
                if downloaded:
                    results.append(downloaded)
            return results
        return []

    # Instaloader fallback for Instagram
    if is_instagram(url):
        cdn_items = _ig_cdn_items(url)
        if cdn_items:
            results = []
            for item in cdn_items:
                downloaded = _download_cdn_url(item["cdn_url"], item["type"])
                if downloaded:
                    results.append(downloaded)
            return results

    return []
