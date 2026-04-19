import html as html_lib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

import httpx
import instaloader
import yt_dlp

COOKIES_PATH = os.path.join(os.path.dirname(__file__), "cookies.txt")
THREADS_COOKIES_PATH = os.path.join(os.path.dirname(__file__), "threads_cookies.txt")
FACEBOOK_COOKIES_PATH = os.path.join(os.path.dirname(__file__), "facebook_cookies.txt")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.instagram.com/",
}

THREADS_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.threads.com/",
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
INSTAGRAM_COOLDOWN_SECONDS = max(
    0, int(os.getenv("SOCIALBOT_INSTAGRAM_COOLDOWN_SECONDS", "900"))
)
_INSTAGRAM_CIRCUIT_UNTIL = 0.0
_INSTAGRAM_CIRCUIT_LOCK = threading.Lock()


class DownloadError(Exception):
    """User-facing download error."""


def _format_seconds(seconds: float) -> str:
    total = max(1, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes}m {secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _instagram_circuit_remaining() -> float:
    with _INSTAGRAM_CIRCUIT_LOCK:
        remaining = _INSTAGRAM_CIRCUIT_UNTIL - time.monotonic()
    return max(0.0, remaining)


def _trip_instagram_circuit(reason: str):
    if INSTAGRAM_COOLDOWN_SECONDS <= 0:
        return
    until = time.monotonic() + INSTAGRAM_COOLDOWN_SECONDS
    with _INSTAGRAM_CIRCUIT_LOCK:
        global _INSTAGRAM_CIRCUIT_UNTIL
        _INSTAGRAM_CIRCUIT_UNTIL = max(_INSTAGRAM_CIRCUIT_UNTIL, until)
    logger.warning(
        "Instagram circuit breaker armed for %s (%s)",
        _format_seconds(INSTAGRAM_COOLDOWN_SECONDS),
        reason,
    )


def _instagram_circuit_message() -> str:
    remaining = _instagram_circuit_remaining()
    return (
        "Instagram está en cooldown desde esta VM por un bloqueo previo. "
        f"Reintentá en {_format_seconds(remaining)}."
    )


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


def _normalize_url(url: str) -> str:
    """Strip tracking query params from supported post URLs."""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except Exception:
        return url

    host = (parts.netloc or "").lower()
    path = parts.path or ""
    if ("instagram.com" in host or "instagr.am" in host) and re.search(r"/(?:p|reel|tv)/[A-Za-z0-9_-]+/?$", path):
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return url


def _is_instagram_auth_or_rate_limit_error(message: str) -> bool:
    text = (message or "").lower()
    needles = (
        "requested content is not available",
        "rate-limit reached",
        "login required",
        "please wait a few minutes",
        "instagram sent an empty media response",
        "401 unauthorized",
        "403 forbidden",
    )
    return any(needle in text for needle in needles)


def _ig_download(url: str) -> list:
    """Use anonymous Instaloader access for public Instagram posts."""
    if _instagram_circuit_remaining() > 0:
        raise DownloadError(_instagram_circuit_message())

    shortcode = _ig_shortcode_from_url(url)
    if not shortcode:
        raise DownloadError("Link de Instagram inválido.")

    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
        max_connection_attempts=1,
    )

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as e:
        message = str(e)
        if _is_instagram_auth_or_rate_limit_error(message):
            _trip_instagram_circuit(message)
            raise DownloadError(
                "Instagram bloqueó el acceso público desde esta VM. "
                f"El bot no usa ninguna cuenta de Instagram. {_instagram_circuit_message()}"
            ) from e
        raise DownloadError(
            "No pude extraer ese post de Instagram en modo anónimo."
        ) from e

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

    if not items:
        raise DownloadError("Instagram no devolvió medios para ese post.")

    results = []
    for item in items:
        downloaded, status_code = _download_cdn_url(
            item["cdn_url"],
            item["type"],
            return_status=True,
        )
        if downloaded:
            results.append(downloaded)
        elif status_code in (401, 403, 429):
            _trip_instagram_circuit(f"cdn http {status_code}")
            raise DownloadError(
                "Instagram bloqueó la descarga pública desde esta VM. "
                f"{_instagram_circuit_message()}"
            )
    if results:
        return results

    raise DownloadError(
        "Instagram resolvió el post, pero no pude bajar los archivos desde la CDN."
    )


def _download_cdn_url(cdn_url: str, item_type: str, headers: dict = None, return_status: bool = False):
    """Download a CDN URL to a temp file and return {type, path, mime}."""
    suffix = ".mp4" if item_type == "video" else ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="ig_cdn_")
    os.close(fd)
    if headers is None:
        headers = BROWSER_HEADERS
    try:
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            with client.stream("GET", cdn_url, headers=headers) as r:
                if r.status_code != 200:
                    os.unlink(tmp_path)
                    return (None, r.status_code) if return_status else None
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
        mime = "video/mp4" if item_type == "video" else "image/jpeg"
        item = {"type": item_type, "path": tmp_path, "mime": mime}
        return (item, 200) if return_status else item
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        return (None, None) if return_status else None


def _facebook_scrape(url: str) -> Optional[list]:
    """Extract media from a public Facebook post by scraping the page HTML."""
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
            logger.error(f"Facebook page HTTP {r.status_code}")
            return None
        html = r.text.replace("\\u0026", "&").replace("\\u003C", "<").replace("\\u003E", ">")
    except Exception as e:
        logger.error(f"Facebook fetch error: {e}")
        return None

    def _clean(u: str) -> str:
        return html_lib.unescape(u)

    # Video: og:video meta tag
    m = re.search(r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        logger.info("Facebook scrape: found og:video")
        return [{"type": "video", "cdn_url": _clean(m.group(1))}]

    # Image: og:image meta tag
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        logger.info("Facebook scrape: found og:image")
        return [{"type": "image", "cdn_url": _clean(m.group(1))}]

    logger.error("Facebook scrape: no media found in page HTML")
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

    def _clean(url: str) -> str:
        return html_lib.unescape(url)

    # Video: look for video_versions array
    m = re.search(r'"video_versions"\s*:\s*\[\s*\{[^]]*?"url"\s*:\s*"(https://[^"]+)"', html)
    if m:
        logger.info("Threads scrape: found video_versions")
        return [{"type": "video", "cdn_url": _clean(m.group(1))}]

    # Video: og:video meta tag
    m = re.search(r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        logger.info("Threads scrape: found og:video")
        return [{"type": "video", "cdn_url": _clean(m.group(1))}]

    # Image: og:image meta tag
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        logger.info("Threads scrape: found og:image")
        return [{"type": "image", "cdn_url": _clean(m.group(1))}]

    logger.error("Threads scrape: no media found in page HTML")
    return None


def download_media(url: str) -> list:
    """
    Download all media from a URL.
    Returns list of: {'type': 'video'|'image', 'path': str, 'mime': str}
    Caller is responsible for deleting the temp files.
    """
    url = _normalize_url(url)

    if is_instagram(url):
        return _ig_download(url)

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
    cookiefile = None
    if is_threads(url) and os.path.exists(THREADS_COOKIES_PATH):
        cookiefile = THREADS_COOKIES_PATH
    elif is_facebook(url) and os.path.exists(FACEBOOK_COOKIES_PATH):
        cookiefile = FACEBOOK_COOKIES_PATH
    elif os.path.exists(COOKIES_PATH):
        cookiefile = COOKIES_PATH
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    elif is_instagram(url):
        logger.warning("Instagram request without cookies.txt configured")

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
        if is_instagram(url) and _is_instagram_auth_or_rate_limit_error(str(e)):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if cookiefile:
                raise DownloadError(
                    "Instagram bloqueó temporalmente la sesión o las cookies vencieron. "
                    "Esperá unos minutos, renová `cookies.txt` y reintentá."
                ) from e
            raise DownloadError(
                "Instagram pidió login y el bot no tiene `cookies.txt` cargado. "
                "Exportá cookies nuevas desde el navegador y reintentá."
            ) from e

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
                downloaded = _download_cdn_url(item["cdn_url"], item["type"], THREADS_BROWSER_HEADERS)
                if downloaded:
                    results.append(downloaded)
            return results
        return []

    # httpx scrape fallback for Facebook public posts
    if is_facebook(url):
        cdn_items = _facebook_scrape(url)
        if cdn_items:
            results = []
            for item in cdn_items:
                downloaded = _download_cdn_url(item["cdn_url"], item["type"])
                if downloaded:
                    results.append(downloaded)
            return results
        return []

    return []
