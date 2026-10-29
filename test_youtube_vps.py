#!/usr/bin/env python3
"""Testea las rutas de descarga de YouTube desde donde corre el bot.

Uso en el VPS, dentro del repo ya actualizado:
    python3 test_youtube_vps.py

Contexto: YouTube bot-checkea la IP de la VM incluso con yt-dlp actual
(2026.10.26 + deno probados). Las instancias Invidious exponen una API
JSON que resuelve formatos y, con local=true, proxeán el archivo por su
propio servidor. Este script mide qué instancias responden y si las URLs
que devuelven se descargan de verdad. Seguro de correr: no toca estado
del bot ni descarga videos completos (solo los primeros KB de cada
formato elegido).
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

CANARY_ID = "S41sbKiMBao"  # short público del pedido original

# Lista amplia: las que respondan de acá definen el default del bot, pero
# la red miente — la lista final la decide este script corrido en el VPS.
CANDIDATE_HOSTS = [
    "invidious.schenkel.eti.br", "yt.chocolatemoo53.com", "inv.nadeko.net",
    "invidious.jing.rocks", "iv.ggtyler.dev", "invidious.privacyredirect.com",
    "yt.artemislena.eu", "iv.melmac.space", "invidious.private.coffee",
    "invidious.reallyaweso.me", "yt.drgnz.club", "invidious.perennialte.ch",
    "iv.datura.network", "invidious.fdn.fr", "invidious.protokolla.fi",
    "yewtu.be", "invidious.f5.si", "invidious.nikkosphere.com",
    "iv.duti.dev", "invidious.dhusch.de", "inv.tux.pizza",
    "iv.catgirl.cloud", "invidious.materialio.us",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
TIMEOUT = 10.0


def pick_formats(payload):
    if d is not None:
        return d._yt_pick_invidious_formats(payload)
    entries = payload.get("adaptiveFormats") or []
    videos = [f for f in entries if (f.get("type") or "").startswith("video/mp4")]
    video = videos[-1] if videos else None
    audios = [f for f in entries if (f.get("type") or "").startswith("audio/mp4")]
    audio = audios[-1] if audios else None
    return (video, audio)


def probe_download(url, referer):
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": UA, "Range": "bytes=0-2047", "Referer": referer},
            timeout=TIMEOUT, follow_redirects=True,
        )
        if r.status_code in (200, 206) and len(r.content) >= 512:
            return "descargable"
        return f"HTTP {r.status_code}"
    except Exception as e:
        return type(e).__name__


LOADERTO_API = "https://loader.to/ajax/download.php"


def probe_loaderto():
    """Pipeline completo de loader.to: acepta conversión → poll → primeros bytes.

    Usa el formato que el bot elige para shorts ("4k": su tope de altura
    preserva la resolución nativa vertical; "1080" daría 608x1080).
    """
    watch = f"https://www.youtube.com/watch?v={CANARY_ID}"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://loader.to/",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as client:
            started = time.monotonic()
            r = client.get(LOADERTO_API, params={"format": "4k", "url": watch})
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:80]}"
            progress_url = (r.json() or {}).get("progress_url")
            if not progress_url:
                return None, f"sin progress_url: {r.text[:80]}"
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                time.sleep(2)
                p = client.get(progress_url).json()
                if p.get("success") and p.get("download_url"):
                    download_url = p["download_url"]
                    break
            else:
                return None, "no terminó la conversión en 120s"
            secs = time.monotonic() - started
            head = client.get(
                download_url,
                headers={"User-Agent": UA, "Range": "bytes=0-2047"},
            )
            ct = head.headers.get("content-type", "?")
            if head.status_code in (200, 206) and len(head.content) >= 512:
                return f"{secs:.0f}s", f"HTTP {head.status_code} ct={ct}"
            return None, f"descarga falló: HTTP {head.status_code} ct={ct}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    hosts = CANDIDATE_HOSTS
    if d is not None:
        for h in d.YOUTUBE_INVIDIOUS_HOSTS:
            if h not in hosts:
                hosts.append(h)
    print(f"Canario: short {CANARY_ID}\n")
    print("=== Invidious: resolución + descarga real ===")
    ok = []
    for host in hosts:
        try:
            started = time.monotonic()
            r = httpx.get(
                f"https://{host}/api/v1/videos/{CANARY_ID}",
                params={"fields": "adaptiveFormats,formatStreams", "local": "true"},
                headers={"User-Agent": UA, "Accept": "application/json",
                         "Referer": f"https://{host}/"},
                timeout=TIMEOUT, follow_redirects=True,
            )
            ms = int((time.monotonic() - started) * 1000)
        except Exception as e:
            print(f"  {host:<32} {type(e).__name__}")
            continue
        if r.status_code != 200:
            print(f"  {host:<32} HTTP {r.status_code}")
            continue
        try:
            payload = r.json()
        except Exception:
            print(f"  {host:<32} json inválido")
            continue
        af = payload.get("adaptiveFormats") or []
        fs = payload.get("formatStreams") or []
        if payload.get("error") or not af:
            print(f"  {host:<32} sin formatos — {str(payload.get('error'))[:60]}")
            continue
        video, audio = pick_formats(payload)
        referer = f"https://{host}/"
        vdesc = probe_download(video["url"], referer) if video else "-"
        adesc = probe_download(audio["url"], referer) if audio else "-"
        res = (video or {}).get("resolution") or f"{(video or {}).get('width')}x{(video or {}).get('height')}"
        print(f"  {host:<32} {ms:>5}ms af={len(af):>3} fs={len(fs)} video({res}):{vdesc[:14]:<14} audio:{adesc[:14]}")
        if vdesc == "descargable" and adesc == "descargable":
            ok.append(host)

    print("\n=== loader.to: conversión + descarga real ===")
    secs, detail = probe_loaderto()
    if secs:
        print(f"  OK — conversión {secs}, mp4: {detail}")
    else:
        print(f"  FALLO — {detail}")

    print("\n=== RESUMEN ===")
    print(f"Instancias que resuelven Y descargan: {', '.join(ok) or 'ninguna'}")
    if secs:
        print("loader.to: FUNCIONA desde esta IP (ruta principal del bot)")
    else:
        print("loader.to: no funciona desde esta IP")
    if ok:
        print(
            "\nPara usarlas como lista del bot (orden por preferencia):\n"
            f"  SOCIALBOT_YOUTUBE_INVIDIOUS_HOSTS={','.join(ok)}"
        )
    print(
        "\nNota: el bot corre el health check diario a las 04:30 (BA) y ya"
        "\nrankea/saltea las muertas solo; esto es para elegir el default."
    )


if __name__ == "__main__":
    main()
