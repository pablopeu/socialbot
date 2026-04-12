import atexit
import base64
import contextlib
import os
import tempfile
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
import httpx
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


def pick_best_mp4(formats: list) -> Optional[str]:
    candidates = [
        f for f in formats
        if f.get("url")
        and f.get("ext") == "mp4"
        and f.get("vcodec", "none") not in ("none", None, "")
    ]
    if not candidates:
        candidates = [
            f for f in formats
            if f.get("url") and f.get("vcodec", "none") not in ("none", None, "")
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

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HTTP_HEADERS,
    }

    # Full Netscape cookie file sent base64-encoded from the PHP bot
    cookie_ctx = contextlib.nullcontext(None)
    if x_ig_cookies:
        try:
            cookie_content = base64.b64decode(x_ig_cookies).decode("utf-8")
        except Exception:
            cookie_content = x_ig_cookies  # not base64, use as-is
        cookie_ctx = _tmp_cookie_file_raw(cookie_content)

    try:
        with cookie_ctx as cookie_path:
            if cookie_path:
                ydl_opts["cookiefile"] = cookie_path
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction failed: {str(e)}")

    if info is None:
        raise HTTPException(status_code=400, detail="No information extracted")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
    else:
        entries = [info]

    if not entries:
        raise HTTPException(status_code=400, detail="No entries found")

    media = []
    for entry in entries:
        if entry is None:
            continue
        item = entry_to_media(entry)
        if item:
            media.append(item)

    if not media:
        raise HTTPException(status_code=400, detail="No media found in the given URL")

    return {"media": media}


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

    async def streamer() -> AsyncGenerator[bytes, None]:
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            yield chunk
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(streamer(), media_type=content_type)
