import atexit
import base64
import os
import tempfile
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, Query, HTTPException, Header
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

# Write Instagram cookies to a temp file at startup.
# Accepts either raw Netscape format or base64-encoded content.
_COOKIE_FILE: Optional[str] = None

_cookie_raw = os.environ.get("INSTAGRAM_COOKIES", "").strip()
if _cookie_raw:
    try:
        _cookie_content = base64.b64decode(_cookie_raw).decode("utf-8")
    except Exception:
        _cookie_content = _cookie_raw  # not base64, use as-is

    _fd, _COOKIE_FILE = tempfile.mkstemp(suffix=".txt", prefix="ig_cookies_")
    with os.fdopen(_fd, "w") as _f:
        _f.write(_cookie_content)
    atexit.register(lambda: os.path.exists(_COOKIE_FILE) and os.unlink(_COOKIE_FILE))


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


@app.get("/health")
def health():
    """Check service status and whether cookies are loaded."""
    raw = os.environ.get("INSTAGRAM_COOKIES", "")
    return {
        "status": "ok",
        "cookies_loaded": _COOKIE_FILE is not None,
        "env_var_set": bool(raw),
        "env_var_length": len(raw),
    }


@app.get("/extract")
def extract(url: str = Query(...), x_secret: Optional[str] = Header(None)):
    _check_header_auth(x_secret)

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": YDL_HTTP_HEADERS,
    }
    if _COOKIE_FILE:
        ydl_opts["cookiefile"] = _COOKIE_FILE

    try:
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
