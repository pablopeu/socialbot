import base64
import contextlib
import http.cookiejar
import os
import re
import shutil
import tempfile
import uuid
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
import httpx
import instaloader
import yt_dlp

app = FastAPI()

SECRET_TOKEN = os.environ.get("SECRET_TOKEN")

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


@contextlib.contextmanager
def _tmp_cookie_file_raw(content: str):
    """Write raw Netscape cookie file content to a temp file and yield its path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ig_cookies_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _check_header_auth(x_secret: Optional[str]):
    if SECRET_TOKEN and x_secret != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _check_param_auth(secret: Optional[str]):
    if SECRET_TOKEN and secret != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _is_direct(f: dict) -> bool:
    """True if the format is a direct file (not HLS/DASH stream)."""
    return (
        f.get("protocol", "") not in ("m3u8", "m3u8_native", "m3u8_local", "dash")
        and not (f.get("url") or "").endswith(".m3u8")
        and not (f.get("url") or "").endswith(".mpd")
    )


def pick_best_mp4(formats: list) -> Optional[str]:
    """Return the highest-resolution direct MP4 URL from a format list."""
    # Direct MP4 with video codec
    candidates = [
        f for f in formats
        if f.get("url")
        and f.get("ext") == "mp4"
        and f.get("vcodec", "none") not in ("none", None, "")
        and _is_direct(f)
    ]
    if not candidates:
        # Fallback: any direct format with video codec
        candidates = [
            f for f in formats
            if f.get("url")
            and f.get("vcodec", "none") not in ("none", None, "")
            and _is_direct(f)
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("height") or 0)["url"]


def entry_to_media(entry: dict) -> Optional[dict]:
    formats = entry.get("formats") or []

    if formats:
        video_url = pick_best_mp4(formats)
        if video_url:
            return {"type": "video", "url": video_url}

    direct_url = entry.get("url")
    if direct_url:
        ext = (entry.get("ext") or "").lower()
        vcodec = entry.get("vcodec") or ""
        if ext in ("mp4", "mov", "webm", "mkv") or vcodec:
            return {"type": "video", "url": direct_url}
        return {"type": "image", "url": direct_url}

    thumbnail = entry.get("thumbnail")
    if thumbnail:
        return {"type": "image", "url": thumbnail}

    return None


# --- Instagram / instaloader helpers ---

def _is_instagram_url(url: str) -> bool:
    return "instagram.com" in url or "instagr.am" in url


def _ig_shortcode_from_url(url: str) -> Optional[str]:
    """Extract Instagram shortcode from a post/reel/tv URL."""
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _load_netscape_cookies_into_session(session, cookie_content: str):
    """Parse a Netscape cookie file and load cookies into a requests.Session."""
    jar = http.cookiejar.MozillaCookieJar()
    for line in cookie_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, flag, path, secure, expires, name, value = parts[:7]
        try:
            exp = int(expires)
        except (ValueError, TypeError):
            exp = None
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=path,
            path_specified=bool(path),
            secure=secure.upper() == "TRUE",
            expires=exp,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        jar.set_cookie(cookie)
    session.cookies.update(jar)


def _ig_items_from_instaloader(url: str, cookie_content: Optional[str]) -> Optional[list]:
    """Use instaloader to fetch media items from an Instagram post."""
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

    if cookie_content:
        _load_netscape_cookies_into_session(L.context._session, cookie_content)

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception:
        return None

    items = []
    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if node.is_video:
                items.append({"type": "video", "url": node.video_url})
            else:
                items.append({"type": "image", "url": node.display_url})
    elif post.is_video:
        items.append({"type": "video", "url": post.video_url})
    else:
        items.append({"type": "image", "url": post.url})

    return items if items else None


def _decode_cookies(x_ig_cookies: Optional[str]) -> Optional[str]:
    if not x_ig_cookies:
        return None
    try:
        return base64.b64decode(x_ig_cookies).decode("utf-8")
    except Exception:
        return x_ig_cookies  # not base64, use as-is


# --- API endpoints ---

@app.get("/debug-env")
def debug_env():
    """List all environment variable names visible to the process."""
    return {"env_keys": sorted(os.environ.keys())}


@app.get("/debug-headers")
def debug_headers(request: Request):
    """Echo back relevant headers to verify the PHP bot is sending cookies."""
    return {
        "x_ig_session_set": bool(request.headers.get("x-ig-session")),
        "x_ig_csrf_set": bool(request.headers.get("x-ig-csrf")),
        "x_secret_set": bool(request.headers.get("x-secret")),
    }


@app.get("/health")
def health():
    """Check service status and whether cookies are loaded."""
    return {"status": "ok"}


@app.get("/extract")
def extract(
    url: str = Query(...),
    x_secret: Optional[str] = Header(None),
    x_ig_cookies: Optional[str] = Header(None),
):
    _check_header_auth(x_secret)

    cookie_content = _decode_cookies(x_ig_cookies)

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HTTP_HEADERS,
    }

    ytdlp_error = None
    try:
        cookie_ctx = contextlib.nullcontext(None)
        if cookie_content:
            cookie_ctx = _tmp_cookie_file_raw(cookie_content)

        with cookie_ctx as cookie_path:
            if cookie_path:
                ydl_opts["cookiefile"] = cookie_path
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

        if info is None:
            ytdlp_error = "No information extracted"
        else:
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
            else:
                entries = [info]

            media = []
            for entry in entries:
                if entry is None:
                    continue
                item = entry_to_media(entry)
                if item:
                    media.append(item)

            if media:
                return {"media": media}
            ytdlp_error = "No media found in the given URL"

    except yt_dlp.utils.DownloadError as e:
        ytdlp_error = str(e)
    except Exception as e:
        ytdlp_error = f"Extraction failed: {str(e)}"

    # Fallback to instaloader for Instagram URLs
    if _is_instagram_url(url):
        items = _ig_items_from_instaloader(url, cookie_content)
        if items:
            return {"media": items}

    raise HTTPException(status_code=400, detail=ytdlp_error or "No media found")


@app.get("/download")
def download_media(
    url: str = Query(...),
    index: int = Query(0),
    x_secret: Optional[str] = Header(None),
    x_ig_cookies: Optional[str] = Header(None),
):
    """Download the Nth media item using yt-dlp (handles CDN auth) and stream it back."""
    _check_header_auth(x_secret)

    cookie_content = _decode_cookies(x_ig_cookies)

    tmp_dir = f"/tmp/ytdl_{uuid.uuid4().hex}"
    os.makedirs(tmp_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "playlist_items": str(index + 1),
        "http_headers": YDL_HTTP_HEADERS,
    }

    ytdlp_ok = False
    ytdlp_error = None

    try:
        cookie_ctx = contextlib.nullcontext(None)
        if cookie_content:
            cookie_ctx = _tmp_cookie_file_raw(cookie_content)

        with cookie_ctx as cookie_path:
            if cookie_path:
                ydl_opts["cookiefile"] = cookie_path
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        files = [f for f in os.listdir(tmp_dir) if not f.endswith((".part", ".ytdl"))]
        if files:
            ytdlp_ok = True
        else:
            ytdlp_error = "No file downloaded"
    except Exception as e:
        ytdlp_error = str(e)

    if ytdlp_ok:
        files = [f for f in os.listdir(tmp_dir) if not f.endswith((".part", ".ytdl"))]
        file_path = os.path.join(tmp_dir, sorted(files)[0])
        file_size = os.path.getsize(file_path)
        ext = file_path.rsplit(".", 1)[-1].lower()
        mime_map = {
            "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp",
        }
        content_type = mime_map.get(ext, "application/octet-stream")

        def streamer():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(65536):
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return StreamingResponse(
            streamer(),
            media_type=content_type,
            headers={"content-length": str(file_size)},
        )

    # yt-dlp failed — clean up and try instaloader fallback for Instagram
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if _is_instagram_url(url):
        items = _ig_items_from_instaloader(url, cookie_content)
        if items and index < len(items):
            item = items[index]
            cdn_url = item["url"]
            item_type = item["type"]

            suffix = ".mp4" if item_type == "video" else ".jpg"
            fd, tmp_file = tempfile.mkstemp(suffix=suffix, prefix="ig_cdn_")
            os.close(fd)

            try:
                with httpx.Client(follow_redirects=True, timeout=120) as client:
                    with client.stream("GET", cdn_url, headers=BROWSER_HEADERS) as r:
                        if r.status_code != 200:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Instagram CDN returned {r.status_code}",
                            )
                        with open(tmp_file, "wb") as f:
                            for chunk in r.iter_bytes(65536):
                                f.write(chunk)

                file_size = os.path.getsize(tmp_file)
                ext = tmp_file.rsplit(".", 1)[-1].lower()
                mime_map = {
                    "mp4": "video/mp4", "webm": "video/webm",
                    "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp",
                }
                content_type = mime_map.get(ext, "application/octet-stream")

                def ig_streamer():
                    try:
                        with open(tmp_file, "rb") as f:
                            while chunk := f.read(65536):
                                yield chunk
                    finally:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(tmp_file)

                return StreamingResponse(
                    ig_streamer(),
                    media_type=content_type,
                    headers={"content-length": str(file_size)},
                )

            except HTTPException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_file)
                raise
            except Exception as e:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_file)
                raise HTTPException(
                    status_code=400,
                    detail=f"Instagram CDN download failed: {str(e)}",
                )

    raise HTTPException(status_code=400, detail=ytdlp_error or "Download failed")


@app.get("/proxy")
async def proxy(url: str = Query(...), secret: Optional[str] = Query(None)):
    """Stream a CDN media file through this service so Telegram can fetch it
    without hitting IP restrictions or missing required headers."""
    _check_param_auth(secret)

    client = httpx.AsyncClient(follow_redirects=True, timeout=60)
    try:
        req = client.build_request("GET", url, headers=BROWSER_HEADERS)
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=400, detail=f"Proxy fetch failed: {str(e)}")

    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=400, detail=f"CDN returned {resp.status_code}")

    content_type = resp.headers.get("content-type", "application/octet-stream")

    # Forward Content-Length and Accept-Ranges so clients know the full file size
    forward_headers = {}
    for h in ("content-length", "accept-ranges", "content-disposition"):
        if h in resp.headers:
            forward_headers[h] = resp.headers[h]

    async def streamer() -> AsyncGenerator[bytes, None]:
        async for chunk in resp.aiter_bytes(chunk_size=65536):
            yield chunk
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(streamer(), media_type=content_type, headers=forward_headers)
