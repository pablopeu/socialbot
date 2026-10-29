#!/usr/bin/env python3
"""Testea los fixers y APIs de Instagram desde donde corre el bot.

Uso en el VPS, dentro del repo ya actualizado:
    python3 test_fixers_vps.py

Usa el mismo httpx, headers, timeouts y parser del bot cuando está
disponible (importa telegrambot/downloader.py). Seguro de correr: no
toca estado del bot ni descarga medios, solo mide respuestas.
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "telegrambot"))

try:
    import downloader as d
except Exception as e:
    print(f"(aviso: no pude importar el downloader del bot: {e}; modo standalone)")
    d = None

import httpx

CANARY_POST = "https://www.instagram.com/p/BsOGulcndj-/"
CANARY_REEL = "https://www.instagram.com/reel/DdU5pjGikpX/"

FIXER_HOSTS = [
    "vxinstagram.com", "zzinstagram.com", "fxstagram.com", "eeinstagram.com",
    "instagram7.com", "toinstagram.com", "dtoinstagram.com", "ddinstagram.com",
    "instagramez.com", "kkinstagram.com", "uuinstagram.com", "oginstagram.com",
]

TIMEOUT = 5.0
YDL_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Mode": "navigate",
}


def _extract_og(html: str):
    if d is not None:
        items = d._extract_og_media_items(html)
        return len(items)
    return len(re.findall(r'property="og:(?:image|video)"', html))


def probe_fixer(host: str, url: str):
    path = re.sub(r"^https?://[^/]+", "", url)
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT, verify=False) as client:
            started = time.monotonic()
            r = client.get(f"https://{host}{path}", headers=YDL_HTTP_HEADERS)
            ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return ("X", f"{type(e).__name__}")
    if r.status_code != 200:
        return (str(r.status_code), "sin og")
    n = _extract_og(r.text)
    return (str(r.status_code), f"{ms}ms og:{n}" if n else f"{ms}ms og:0")


def probe_api_instapdown(url: str, variant: str):
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            started = time.monotonic()
            r = client.post(
                "https://instapdown.com/api/download",
                headers={
                    "User-Agent": YDL_HTTP_HEADERS["User-Agent"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"url": url, "variant": variant},
            )
            ms = int((time.monotonic() - started) * 1000)
            payload = r.json()
    except Exception as e:
        return ("X", type(e).__name__)
    items = payload.get("items") if isinstance(payload, dict) else None
    n = len(items) if payload.get("ok") and isinstance(items, list) else 0
    return (str(r.status_code), f"{ms}ms items:{n}" if n else f"{ms}ms sin items")


def probe_api_simple(name: str, url: str):
    requests = {
        "downreels": lambda c: c.post(
            "https://downreels.com/api/fetch.php",
            headers={"User-Agent": YDL_HTTP_HEADERS["User-Agent"], "Content-Type": "application/json", "Referer": "https://downreels.com/"},
            json={"url": url},
        ),
        "fastvidl": lambda c: c.post(
            "https://fastvidl.com/api/lookup",
            headers={"User-Agent": YDL_HTTP_HEADERS["User-Agent"], "Content-Type": "application/json", "Referer": "https://fastvidl.com/instagram-video-downloader-free"},
            json={"url": url},
        ),
        "nuelink": lambda c: c.get(
            "https://tools.nuelink.com/api/socialVideoDownloader/instagram/getDownloadLink",
            params={"link": url},
            headers={"User-Agent": YDL_HTTP_HEADERS["User-Agent"], "Referer": "https://nuelink.com/tools/instagram-video-downloader"},
        ),
        "listnr": lambda c: c.post(
            "https://bff.listnr.tech/backend/user/getInfoYT",
            headers={"User-Agent": YDL_HTTP_HEADERS["User-Agent"], "Content-Type": "application/json", "Referer": "https://listnr.ai/instagram-video-downloader", "Origin": "https://listnr.ai"},
            json={"url": url, "platform": "instagram", "type": "video"},
        ),
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            started = time.monotonic()
            r = requests[name](client)
            ms = int((time.monotonic() - started) * 1000)
            payload = r.json()
    except Exception as e:
        return ("X", type(e).__name__)
    media = 0
    if isinstance(payload, dict):
        for field in ("videos", "media", "data", "url"):
            if payload.get(field):
                media += 1
    return (str(r.status_code), f"{ms}ms media:{'si' if media else 'no'}")


def main():
    try:
        ip = httpx.get("https://api.ipify.org", timeout=5).text
    except Exception:
        ip = "?"
    print(f"IP pública del VPS: {ip}")
    print(f"Canarios: post=BsOGulcndj- reel=DdU5pjGikpX\n")

    print("=== FIXERS (GET, timeout 5s, verify SSL off) ===")
    print(f"{'host':<20} {'post (/p/)':<22} {'reel (/reel/)':<22}")
    post_ok, reel_ok = [], []
    for host in FIXER_HOSTS:
        post = probe_fixer(host, CANARY_POST)
        reel = probe_fixer(host, CANARY_REEL)
        print(f"{host:<20} {post[0]+' '+post[1]:<22} {reel[0]+' '+reel[1]:<22}")
        if post[0] == "200" and "og:" in post[1] and not post[1].endswith("og:0"):
            post_ok.append(host)
        if reel[0] == "200" and "og:" in reel[1] and not reel[1].endswith("og:0"):
            reel_ok.append(host)

    print("\n=== APIs ===")
    print(f"{'api':<12} {'resultado':<30}")
    instapdown_post = probe_api_instapdown(CANARY_POST, "photo")
    instapdown_reel = probe_api_instapdown(CANARY_REEL, "reels")
    print(f"{'instapdown':<12} {instapdown_post[0]+' '+instapdown_post[1]:<30} (post)")
    print(f"{'instapdown':<12} {instapdown_reel[0]+' '+instapdown_reel[1]:<30} (reel)")
    for name in ("downreels", "fastvidl", "nuelink", "listnr"):
        res = probe_api_simple(name, CANARY_REEL)
        print(f"{name:<12} {res[0]+' '+res[1]:<30} (reel)")

    if d is not None:
        print("\n=== direct (instaloader) ===")
        try:
            alive, reason, ms = d._health_probe_direct("BsOGulcndj-")
            print(f"direct: {'vivo' if alive else 'MUERTO'} — {reason} {ms or ''}")
        except Exception as e:
            print(f"direct: error — {e}")

    print("\n=== RESUMEN ===")
    print(f"POST ok:  {', '.join(post_ok) or 'ninguno'}")
    print(f"REEL ok:  {', '.join(reel_ok) or 'ninguno'}")
    both = [h for h in post_ok if h in reel_ok]
    print(f"AMBOS ok: {', '.join(both) or 'ninguno'}")


if __name__ == "__main__":
    main()
