import html as html_lib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

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

SAVEINSTA_HEADERS = {
    "User-Agent": YDL_HTTP_HEADERS["User-Agent"],
    "Accept": "*/*",
    "Referer": "https://saveinsta.io/",
}

MIME_MAP = {
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp",
}

MAX_TG_BYTES = 50 * 1024 * 1024  # 50 MB Telegram bot limit
INSTAGRAM_FIXER_HOSTS = tuple(
    host.strip()
    for host in os.getenv(
        "SOCIALBOT_INSTAGRAM_FIXER_HOSTS",
        "vxinstagram.com,zzinstagram.com,fxstagram.com,eeinstagram.com",
    ).split(",")
    if host.strip()
)
INSTAGRAM_FIXER_VERIFY_SSL = os.getenv(
    "SOCIALBOT_INSTAGRAM_FIXER_VERIFY_SSL", "0"
).lower() not in {"0", "false", "no", "off"}
INSTAGRAM_SAVEINSTA_PAGE_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_SAVEINSTA_PAGE_URL",
    "https://saveinsta.io/en/story-downloader",
)
INSTAGRAM_USE_COOKIES = os.getenv(
    "SOCIALBOT_INSTAGRAM_USE_COOKIES", "0"
).lower() in {"1", "true", "yes", "on"}
INSTAGRAM_MAX_CAROUSEL_ITEMS = max(
    1, int(os.getenv("SOCIALBOT_INSTAGRAM_MAX_CAROUSEL_ITEMS", "20"))
)


class DownloadError(Exception):
    """User-facing download error."""


def instagram_status() -> dict:
    return {
        "fixer_hosts": list(INSTAGRAM_FIXER_HOSTS),
        "fixer_verify_ssl": INSTAGRAM_FIXER_VERIFY_SSL,
        "use_cookies": INSTAGRAM_USE_COOKIES,
        "max_carousel_items": INSTAGRAM_MAX_CAROUSEL_ITEMS,
    }


def _trip_instagram_circuit(reason: str):
    logger.warning("Instagram public access failed (%s)", reason)


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


def _ig_img_index_from_url(url: str) -> Optional[int]:
    try:
        query = parse_qs(urlsplit(url).query)
    except Exception:
        return None

    values = query.get("img_index")
    if not values:
        return None

    try:
        index = int(values[0])
    except (TypeError, ValueError):
        return None

    return index if index > 0 else None


def _ig_story_path_from_url(url: str) -> Optional[str]:
    parts = urlsplit(url)
    host = (parts.netloc or "").lower()
    if "instagram.com" not in host and "instagr.am" not in host:
        return None

    path = parts.path or ""
    if re.search(r"/stories/[^/]+/[0-9]+/?$", path):
        return path if path.endswith("/") else f"{path}/"
    return None


def _ig_path_from_url(url: str) -> Optional[str]:
    parts = urlsplit(url)
    path = parts.path or ""
    if re.search(r"/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$", path):
        return path if path.endswith("/") else f"{path}/"
    story_path = _ig_story_path_from_url(url)
    if story_path:
        return story_path
    return None


def _normalize_url(url: str) -> str:
    """Strip tracking query params from supported post URLs."""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except Exception:
        return url

    host = (parts.netloc or "").lower()
    path = parts.path or ""
    if ("instagram.com" in host or "instagr.am" in host) and re.search(
        r"/(?:p|reel|tv|stories/[^/]+)/[A-Za-z0-9_-]+/?$", path
    ):
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return url


def _ig_url_with_img_index(url: str, img_index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, f"img_index={img_index}", "")
    )


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


def _extract_og_media_items(html: str) -> list:
    def _clean(value: str) -> str:
        return html_lib.unescape(value)

    items = []
    seen = set()
    for pattern, item_type in (
        (r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']', "video"),
        (r'<meta[^>]+property=["\']og:image(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']', "image"),
    ):
        for match in re.finditer(pattern, html, re.I):
            cdn_url = _clean(match.group(1))
            if cdn_url not in seen:
                seen.add(cdn_url)
                items.append({"type": item_type, "cdn_url": cdn_url})
    return items


def _ig_fixer_download_headers(host: str) -> dict:
    return {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": f"https://{host}/",
    }


def _normalize_fixer_media_url(cdn_url: str) -> str:
    parts = urlsplit(cdn_url)
    if parts.netloc.endswith("vxinstagram.com") and parts.path == "/VerifySnapsaveLink":
        query = parse_qs(parts.query)
        rapid_urls = query.get("rapidsaveUrl")
        if rapid_urls and rapid_urls[0]:
            return rapid_urls[0]
    return cdn_url


def _append_unique_media_items(items: list, new_items: list, seen: set) -> int:
    added = 0
    for item in new_items:
        media_url = _normalize_fixer_media_url(item["cdn_url"])
        if media_url in seen:
            continue
        seen.add(media_url)
        copied = dict(item)
        copied["cdn_url"] = media_url
        items.append(copied)
        added += 1
    return added


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _emit_downloaded_item(
    item: dict,
    results: list,
    on_item=None,
    seen_hashes: set = None,
) -> bool:
    if seen_hashes is not None:
        try:
            item_hash = _file_sha256(item["path"])
        except Exception as e:
            logger.debug("Could not hash downloaded Instagram item: %s", e)
            item_hash = None

        if item_hash and item_hash in seen_hashes:
            try:
                os.unlink(item["path"])
            except FileNotFoundError:
                pass
            return False
        if item_hash:
            seen_hashes.add(item_hash)

    results.append(item)
    if on_item:
        on_item(item)
    return True


def _decode_saveinsta_script(script: str) -> str:
    """
    Decode Saveinsta's generated script without executing third-party JS.
    The site returns an eval-wrapped base conversion payload containing HTML.
    """
    match = re.search(
        r'\}\("(?P<payload>.*)",\d+,"(?P<alphabet>[^"]+)",'
        r"(?P<offset>\d+),(?P<base>\d+),\d+\)\)$",
        script,
        re.S,
    )
    if not match:
        return ""

    payload = match.group("payload")
    alphabet = match.group("alphabet")
    offset = int(match.group("offset"))
    source_base = int(match.group("base"))
    separator = alphabet[source_base]

    chars = []
    for chunk in payload.split(separator):
        if not chunk:
            continue
        for index, char in enumerate(alphabet):
            chunk = chunk.replace(char, str(index))
        try:
            chars.append(chr(int(chunk, source_base) - offset))
        except ValueError:
            return ""

    return unquote("".join(chars))


def _extract_saveinsta_html(decoded_script: str) -> str:
    assignment = 'document.getElementById("search-result").innerHTML = "'
    start = decoded_script.find(assignment)
    if start < 0:
        return ""
    start += len(assignment)
    end = decoded_script.rfind('";')
    if end <= start:
        return ""

    try:
        return json.loads(f'"{decoded_script[start:end]}"')
    except json.JSONDecodeError:
        return ""


def _extract_saveinsta_media_items(html: str) -> list:
    items = []
    seen = set()
    for item_type, url in re.findall(
        r'<a[^>]+title="Download (Image|Video)"[^>]+href="([^"]+)"',
        html,
        re.I,
    ):
        media_url = html_lib.unescape(url)
        if media_url in seen:
            continue
        seen.add(media_url)
        items.append(
            {
                "type": "video" if item_type.lower() == "video" else "image",
                "cdn_url": media_url,
            }
        )
    return items


def _ig_download_story_via_saveinsta(url: str, on_item=None) -> list:
    if not INSTAGRAM_SAVEINSTA_PAGE_URL:
        return []

    try:
        with httpx.Client(follow_redirects=True, timeout=45) as client:
            page = client.get(INSTAGRAM_SAVEINSTA_PAGE_URL, headers=YDL_HTTP_HEADERS)
            page.raise_for_status()

            exp_match = re.search(r'k_exp="([^"]+)"', page.text)
            token_match = re.search(r'k_token="([^"]+)"', page.text)
            if not exp_match or not token_match:
                logger.debug("Saveinsta page did not expose token metadata")
                return []

            response = client.post(
                "https://saveinsta.io/api/ajaxSearch",
                data={
                    "k_exp": exp_match.group(1),
                    "k_token": token_match.group(1),
                    "q": url,
                    "t": "media",
                    "lang": "en",
                    "v": "v2",
                    "html": "",
                },
                headers={
                    **YDL_HTTP_HEADERS,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://saveinsta.io",
                    "Referer": INSTAGRAM_SAVEINSTA_PAGE_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as e:
        logger.debug("Saveinsta story request failed: %s", e)
        return []

    script = payload.get("data")
    if payload.get("status") != "ok" or not isinstance(script, str) or not script:
        logger.debug("Saveinsta returned no usable story payload")
        return []

    decoded = _decode_saveinsta_script(script)
    html = _extract_saveinsta_html(decoded)
    cdn_items = _extract_saveinsta_media_items(html)
    if not cdn_items:
        logger.debug("Saveinsta returned no downloadable story links")
        return []

    results = []
    for item in cdn_items:
        downloaded = _download_cdn_url(
            item["cdn_url"],
            item["type"],
            headers=SAVEINSTA_HEADERS,
        )
        if downloaded:
            _emit_downloaded_item(downloaded, results, on_item)

    if results:
        logger.info("Instagram story media downloaded via Saveinsta")
    return results


def _ig_collect_fixer_items(client: httpx.Client, host: str, url: str) -> list:
    path = _ig_path_from_url(url)
    if not path or not INSTAGRAM_FIXER_HOSTS:
        return []

    fixer_parts = urlsplit(url)
    fixer_url = urlunsplit(("https", host, path, fixer_parts.query, ""))
    r = client.get(fixer_url, headers=YDL_HTTP_HEADERS)
    if r.status_code != 200:
        logger.debug(f"Instagram fixer {host} returned HTTP {r.status_code}")
        return []

    return _extract_og_media_items(r.text)


def _ig_download_via_fixers(url: str, source_url: str = None, on_item=None) -> list:
    path = _ig_path_from_url(url)
    if not path or not INSTAGRAM_FIXER_HOSTS:
        return []

    source_url = source_url or url
    prefer_video = bool(re.search(r"/(?:reel|reels|tv)/", path))
    is_post = bool(re.search(r"/p/[A-Za-z0-9_-]+/?$", path))
    requested_img_index = _ig_img_index_from_url(source_url)
    should_probe_carousel = is_post and not prefer_video

    for host in INSTAGRAM_FIXER_HOSTS:
        results = []
        items = []
        seen = set()
        seen_hashes = set()
        duplicate_or_empty_seen = False
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=30,
                verify=INSTAGRAM_FIXER_VERIFY_SSL,
            ) as client:
                base_items = _ig_collect_fixer_items(client, host, url)
                _append_unique_media_items(items, base_items, seen)

                if should_probe_carousel:
                    for index in range(1, INSTAGRAM_MAX_CAROUSEL_ITEMS + 1):
                        indexed_url = _ig_url_with_img_index(url, index)
                        indexed_items = _ig_collect_fixer_items(
                            client, host, indexed_url
                        )
                        added = _append_unique_media_items(
                            items, indexed_items, seen
                        )
                        if added == 0:
                            duplicate_or_empty_seen = True
                            if index > 1:
                                break
        except Exception as e:
            logger.debug(f"Instagram fixer {host} request failed: {e}")
            continue

        if not items:
            logger.debug(f"Instagram fixer {host} returned no og media tags")
            continue
        if prefer_video and not any(item["type"] == "video" for item in items):
            logger.debug(f"Instagram fixer {host} returned only images for reel/tv")
            continue
        if requested_img_index and len(items) < requested_img_index:
            logger.debug(
                "Instagram fixer %s only exposed %s/%s carousel items",
                host,
                len(items),
                requested_img_index,
            )
            continue
        if should_probe_carousel and len(items) == 1 and not duplicate_or_empty_seen:
            logger.debug(f"Instagram fixer {host} did not finish carousel probing")
            continue

        for item in items:
            downloaded, status_code = _download_cdn_url(
                item["cdn_url"],
                item["type"],
                headers=_ig_fixer_download_headers(host),
                return_status=True,
            )
            if downloaded:
                _emit_downloaded_item(
                    downloaded,
                    results,
                    on_item,
                    seen_hashes=seen_hashes if should_probe_carousel else None,
                )
            elif status_code in (401, 403, 429):
                logger.debug(
                    "Instagram fixer %s exposed media URL but CDN returned HTTP %s",
                    host,
                    status_code,
                )
                results = []
                break
            elif status_code:
                logger.debug(
                    "Instagram fixer %s media download returned HTTP %s for %s",
                    host,
                    status_code,
                    item["type"],
                )
            else:
                logger.debug(
                    "Instagram fixer %s media download failed without HTTP status for %s",
                    host,
                    item["type"],
                )

        if results:
            if requested_img_index and len(results) < requested_img_index:
                logger.debug(
                    "Instagram fixer %s only downloaded %s/%s unique carousel items",
                    host,
                    len(results),
                    requested_img_index,
                )
                for item in results:
                    try:
                        os.unlink(item["path"])
                    except FileNotFoundError:
                        pass
                continue
            logger.info(f"Instagram media downloaded via fixer {host}")
            return results

    return []


def _ig_download_direct(url: str, on_item=None) -> list:
    """Use anonymous Instaloader access for public Instagram posts."""
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
                "El bot no usa ninguna cuenta de Instagram."
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
    seen_hashes = set()
    for item in items:
        downloaded, status_code = _download_cdn_url(
            item["cdn_url"],
            item["type"],
            return_status=True,
        )
        if downloaded:
            _emit_downloaded_item(
                downloaded,
                results,
                on_item,
                seen_hashes=seen_hashes if len(items) > 1 else None,
            )
        elif status_code in (401, 403, 429):
            _trip_instagram_circuit(f"cdn http {status_code}")
            raise DownloadError(
                "Instagram bloqueó la descarga pública desde esta VM."
            )
    if results:
        return results

    raise DownloadError(
        "Instagram resolvió el post, pero no pude bajar los archivos desde la CDN."
    )


def _ig_download(url: str, source_url: str = None, on_item=None) -> list:
    source_url = source_url or url
    if _ig_story_path_from_url(url):
        fixer_results = _ig_download_via_fixers(
            url, source_url=source_url, on_item=on_item
        )
        if fixer_results:
            return fixer_results
        saveinsta_results = _ig_download_story_via_saveinsta(url, on_item=on_item)
        if saveinsta_results:
            return saveinsta_results
        raise DownloadError(
            "No pude obtener esa historia de Instagram con los métodos alternativos. "
            "Puede haber vencido, ser privada o no estar disponible públicamente."
        )

    try:
        return _ig_download_direct(url, on_item=on_item)
    except DownloadError as direct_error:
        fixer_results = _ig_download_via_fixers(
            url, source_url=source_url, on_item=on_item
        )
        if fixer_results:
            return fixer_results
        raise direct_error


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


def download_media(url: str, on_item=None) -> list:
    """
    Download all media from a URL.
    Returns list of: {'type': 'video'|'image', 'path': str, 'mime': str}
    Caller is responsible for deleting the temp files.
    """
    source_url = url.strip()
    url = _normalize_url(source_url)

    instagram_error = None
    if is_instagram(url):
        try:
            return _ig_download(url, source_url=source_url, on_item=on_item)
        except DownloadError as e:
            instagram_error = e
            logger.debug("Instagram native download failed, trying yt-dlp fallback: %s", e)

    tmp_dir = f"/tmp/bot_{uuid.uuid4().hex}"
    os.makedirs(tmp_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(playlist_index)03d_%(id)s.%(ext)s"),
        "format": "best[ext=mp4][filesize<50M]/best[filesize<50M]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_headers": YDL_HTTP_HEADERS,
    }

    # Pick the right cookies file for the platform
    cookiefile = None
    if is_threads(url) and os.path.exists(THREADS_COOKIES_PATH):
        cookiefile = THREADS_COOKIES_PATH
    elif is_facebook(url) and os.path.exists(FACEBOOK_COOKIES_PATH):
        cookiefile = FACEBOOK_COOKIES_PATH
    elif (not is_instagram(url) or INSTAGRAM_USE_COOKIES) and os.path.exists(COOKIES_PATH):
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
                "Instagram bloqueó el acceso anónimo desde esta VM. "
                "El bot no usa ninguna cuenta de Instagram."
            ) from e

    if ytdlp_ok:
        results = []
        for fname in sorted([f for f in os.listdir(tmp_dir) if not f.endswith((".part", ".ytdl"))]):
            fpath = os.path.join(tmp_dir, fname)
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            mime = MIME_MAP.get(ext, "application/octet-stream")
            ftype = "video" if "video" in mime else "image"
            item = {"type": ftype, "path": fpath, "mime": mime, "_dir": tmp_dir}
            results.append(item)
            if on_item:
                on_item(item)
        return results

    shutil.rmtree(tmp_dir, ignore_errors=True)

    if instagram_error:
        raise instagram_error

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
