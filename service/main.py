import os
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Header
import yt_dlp

app = FastAPI()

SECRET_TOKEN = os.environ.get("SECRET_TOKEN")


def pick_best_mp4(formats: list) -> Optional[str]:
    """Return the highest-resolution MP4 URL from a yt-dlp format list."""
    # Prefer MP4 with a real video codec
    candidates = [
        f for f in formats
        if f.get("url")
        and f.get("ext") == "mp4"
        and f.get("vcodec", "none") not in ("none", None, "")
    ]
    if not candidates:
        # Fallback: any format with a video codec
        candidates = [
            f for f in formats
            if f.get("url") and f.get("vcodec", "none") not in ("none", None, "")
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("height") or 0)["url"]


def entry_to_media(entry: dict) -> Optional[dict]:
    """Convert a yt-dlp info entry to a {'type': ..., 'url': ...} dict."""
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


@app.get("/extract")
def extract(url: str = Query(...), x_secret: Optional[str] = Header(None)):
    # Optional bearer auth
    if SECRET_TOKEN and x_secret != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction failed: {str(e)}")

    if info is None:
        raise HTTPException(status_code=400, detail="No information extracted")

    # Carousel / playlist vs single item
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
