import html as html_lib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

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
        # Verificados desde el VPS (og real en post y reel) primero;
        # el health check diario rankea por latencia y saltea los muertos.
        "zzinstagram.com,instagram7.com,toinstagram.com,uuinstagram.com,"
        "vxinstagram.com,fxstagram.com,eeinstagram.com,"
        "dtoinstagram.com,ddinstagram.com,instagramez.com,kkinstagram.com,oginstagram.com",
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
INSTAGRAM_DOWNREELS_API_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_DOWNREELS_API_URL",
    "https://downreels.com/api/fetch.php",
)
INSTAGRAM_FASTVIDL_API_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_FASTVIDL_API_URL",
    "https://fastvidl.com/api/lookup",
)
INSTAGRAM_NUELINK_API_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_NUELINK_API_URL",
    "https://tools.nuelink.com/api/socialVideoDownloader/instagram/getDownloadLink",
)
INSTAGRAM_LISTNR_API_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_LISTNR_API_URL",
    "https://bff.listnr.tech/backend/user/getInfoYT",
)
INSTAGRAM_INSTAPDOWN_API_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_INSTAPDOWN_API_URL",
    "https://instapdown.com/api/download",
)
INSTAGRAM_PROBE_TIMEOUT = float(
    os.getenv("SOCIALBOT_INSTAGRAM_PROBE_TIMEOUT", "5")
)
INSTAGRAM_USE_COOKIES = os.getenv(
    "SOCIALBOT_INSTAGRAM_USE_COOKIES", "0"
).lower() in {"1", "true", "yes", "on"}
INSTAGRAM_MAX_CAROUSEL_ITEMS = max(
    1, int(os.getenv("SOCIALBOT_INSTAGRAM_MAX_CAROUSEL_ITEMS", "20"))
)
TWITTER_FXTWITTER_API_URL = os.getenv(
    "SOCIALBOT_TWITTER_FXTWITTER_API_URL",
    "https://api.fxtwitter.com/status/{id}",
)
TWITTER_VXTWITTER_API_URL = os.getenv(
    "SOCIALBOT_TWITTER_VXTWITTER_API_URL",
    "https://api.vxtwitter.com/twitter/status/{id}",
)


class DownloadError(Exception):
    """User-facing download error."""


class _RouteTrace:
    """Accumulates per-route failure reasons for Instagram diagnostics.

    Each route (direct instaloader, fixers, yt-dlp) appends a short reason when
    it fails. The summary feeds the admin alert body; the key (sorted route
    names) drives edge-triggered dedup so repeated identical failures only
    notify once and a success rearms the alert.
    """

    def __init__(self):
        self._entries = []  # list of (route, reason)

    def add(self, route: str, reason: str):
        self._entries.append((route, str(reason)))

    def key(self) -> str:
        names = sorted({route for route, _ in self._entries})
        return ";".join(names) if names else "instagram"

    def summary(self) -> str:
        if not self._entries:
            return ""
        return "; ".join(f"{route}: {reason}" for route, reason in self._entries)


def instagram_status() -> dict:
    state = _load_health_state()
    return {
        "fixer_hosts": list(INSTAGRAM_FIXER_HOSTS),
        "fixer_verify_ssl": INSTAGRAM_FIXER_VERIFY_SSL,
        "use_cookies": INSTAGRAM_USE_COOKIES,
        "max_carousel_items": INSTAGRAM_MAX_CAROUSEL_ITEMS,
        "downreels_enabled": bool(INSTAGRAM_DOWNREELS_API_URL),
        "fastvidl_enabled": bool(INSTAGRAM_FASTVIDL_API_URL),
        "nuelink_enabled": bool(INSTAGRAM_NUELINK_API_URL),
        "listnr_enabled": bool(INSTAGRAM_LISTNR_API_URL),
        "instapdown_enabled": bool(INSTAGRAM_INSTAPDOWN_API_URL),
        "probe_timeout_s": INSTAGRAM_PROBE_TIMEOUT,
        "health_check_time": (
            f"{INSTAGRAM_HEALTH_CHECK_HOUR:02d}:{INSTAGRAM_HEALTH_CHECK_MINUTE:02d}"
        ),
        "health": {
            "date": state.get("date"),
            "checked_at": state.get("checked_at"),
            "methods": state.get("methods", {}),
        },
    }


def _trip_instagram_circuit(reason: str):
    logger.warning("Instagram public access failed (%s)", reason)


# --- Salud de métodos Instagram ---
#
# Cada madrugada (hora Buenos Aires) se sondea cada método contra un post
# canario público y se persiste en instagram_health.json qué métodos viven.
# Los muertos se saltean durante todo el día sin gastarles un solo request;
# reviven en el chequeo del día siguiente. Si en vivo un método falla a nivel
# de transporte (timeout, 5xx), se marca muerto para el resto del día.

try:
    BA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:
    BA_TZ = timezone(timedelta(hours=-3))

INSTAGRAM_HEALTH_CANARY_URL = os.getenv(
    "SOCIALBOT_INSTAGRAM_HEALTH_CANARY",
    "https://www.instagram.com/p/BsOGulcndj-/",
)
INSTAGRAM_HEALTH_CHECK_HOUR = int(os.getenv("SOCIALBOT_INSTAGRAM_HEALTH_HOUR", "4"))
INSTAGRAM_HEALTH_CHECK_MINUTE = int(os.getenv("SOCIALBOT_INSTAGRAM_HEALTH_MINUTE", "30"))
HEALTH_STATE_PATH = os.path.join(os.path.dirname(__file__), "instagram_health.json")

_HEALTH_LOCK = threading.Lock()
_HEALTH_STATE = None  # caché en memoria del último estado cargado/guardado


def _health_today() -> str:
    return datetime.now(BA_TZ).date().isoformat()


def _health_exc_reason(e: Exception) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text[:80]}" if text else type(e).__name__


def _load_health_state() -> dict:
    global _HEALTH_STATE
    if _HEALTH_STATE is None:
        try:
            with open(HEALTH_STATE_PATH) as f:
                loaded = json.load(f)
            _HEALTH_STATE = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _HEALTH_STATE = {}
    return _HEALTH_STATE


def _save_health_state_locked(state: dict):
    global _HEALTH_STATE
    _HEALTH_STATE = state
    try:
        with open(HEALTH_STATE_PATH, "w") as f:
            json.dump(state, f, ensure_ascii=True, indent=2)
            f.write("\n")
    except Exception as e:
        logger.warning(f"Failed to write Instagram health state: {e}")


def instagram_health_stale() -> bool:
    """True si no hay chequeo hecho hoy (hora Buenos Aires)."""
    return _load_health_state().get("date") != _health_today()


def method_alive(name: str) -> bool:
    """True salvo que el chequeo de hoy haya marcado al método como muerto."""
    state = _load_health_state()
    if state.get("date") != _health_today():
        return True
    record = state.get("methods", {}).get(name)
    if not isinstance(record, dict):
        return True
    return bool(record.get("alive"))


def _mark_method_dead(name: str, reason: str):
    with _HEALTH_LOCK:
        state = _load_health_state()
        today = _health_today()
        if state.get("date") != today:
            state = {"date": today, "methods": {}}
        state.setdefault("methods", {})[name] = {
            "alive": False,
            "reason": str(reason)[:100],
            "at": datetime.now(BA_TZ).isoformat(timespec="seconds"),
        }
        _save_health_state_locked(state)
    logger.info(
        "Instagram: método %s marcado MUERTO hasta el próximo chequeo (%s).",
        name,
        reason,
    )


def instagram_note_total_failure():
    """Si falló todo y hoy no queda ningún método vivo, vencer el estado.

    Cura el caso de un chequeo hecho durante un problema de red de la VM:
    el próximo pedido (o el loop de salud) vuelve a probar todos los métodos.
    """
    with _HEALTH_LOCK:
        state = _load_health_state()
        if state.get("date") != _health_today():
            return
        methods = state.get("methods", {})
        if methods and not any(
            isinstance(r, dict) and r.get("alive") for r in methods.values()
        ):
            state["date"] = ""
            _save_health_state_locked(state)
            logger.warning(
                "Instagram: ningún método vivo y falló todo; estado vencido "
                "para re-chequear en el próximo pedido."
            )


def instagram_seconds_until_next_check() -> float:
    now = datetime.now(BA_TZ)
    target = now.replace(
        hour=INSTAGRAM_HEALTH_CHECK_HOUR,
        minute=INSTAGRAM_HEALTH_CHECK_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds() + 1)


def _health_probe_direct(shortcode: str) -> tuple:
    if not shortcode:
        return (False, "canario inválido", None)
    try:
        L = _new_instaloader()
        started = time.monotonic()
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    return (True, f"ok ({post.typename})", latency_ms)


def _health_probe_fixer(host: str, canary: str) -> tuple:
    path = _ig_path_from_url(canary)
    if not path:
        return (False, "canario inválido", None)
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=INSTAGRAM_PROBE_TIMEOUT,
            verify=INSTAGRAM_FIXER_VERIFY_SSL,
        ) as client:
            started = time.monotonic()
            r = client.get(f"https://{host}{path}", headers=YDL_HTTP_HEADERS)
            latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    if r.status_code != 200:
        return (False, f"http {r.status_code}", None)
    items = _extract_og_media_items(r.text)
    if not items:
        return (False, "sin media en OG", None)
    return (True, f"ok ({len(items)} medios)", latency_ms)


def _health_probe_instapdown(canary: str) -> tuple:
    if not INSTAGRAM_INSTAPDOWN_API_URL:
        return (False, "deshabilitado", None)
    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            started = time.monotonic()
            r = client.post(
                INSTAGRAM_INSTAPDOWN_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"url": canary, "variant": "photo"},
            )
            if r.status_code != 200:
                return (False, f"http {r.status_code}", None)
            payload = r.json()
            latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    if not payload.get("ok") or not payload.get("items"):
        return (False, "sin media", None)
    return (True, f"ok ({len(payload['items'])} medios)", latency_ms)


def _health_probe_downreels(canary: str) -> tuple:
    if not INSTAGRAM_DOWNREELS_API_URL:
        return (False, "deshabilitado", None)
    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            started = time.monotonic()
            r = client.post(
                INSTAGRAM_DOWNREELS_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Content-Type": "application/json",
                    "Referer": "https://downreels.com/",
                },
                json={"url": canary},
            )
            if r.status_code != 200:
                return (False, f"http {r.status_code}", None)
            r.json()
            latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    return (True, "ok", latency_ms)


def _health_probe_fastvidl(canary: str) -> tuple:
    if not INSTAGRAM_FASTVIDL_API_URL:
        return (False, "deshabilitado", None)
    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            started = time.monotonic()
            r = client.post(
                INSTAGRAM_FASTVIDL_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Content-Type": "application/json",
                    "Referer": "https://fastvidl.com/instagram-video-downloader-free",
                },
                json={"url": canary},
            )
            if r.status_code != 200:
                return (False, f"http {r.status_code}", None)
            r.json()
            latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    return (True, "ok", latency_ms)


def _health_probe_nuelink(canary: str) -> tuple:
    if not INSTAGRAM_NUELINK_API_URL:
        return (False, "deshabilitado", None)
    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            started = time.monotonic()
            r = client.get(
                INSTAGRAM_NUELINK_API_URL,
                params={"link": canary},
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Referer": "https://nuelink.com/tools/instagram-video-downloader",
                },
            )
            if r.status_code != 200:
                return (False, f"http {r.status_code}", None)
            r.json()
            latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    return (True, "ok", latency_ms)


def _health_probe_listnr(canary: str) -> tuple:
    if not INSTAGRAM_LISTNR_API_URL:
        return (False, "deshabilitado", None)
    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            started = time.monotonic()
            r = client.post(
                INSTAGRAM_LISTNR_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Content-Type": "application/json",
                    "Referer": "https://listnr.ai/instagram-video-downloader",
                    "Origin": "https://listnr.ai",
                },
                json={"url": canary, "platform": "instagram", "type": "video"},
            )
            if r.status_code != 200:
                return (False, f"http {r.status_code}", None)
            r.json()
            latency_ms = int((time.monotonic() - started) * 1000)
    except Exception as e:
        return (False, _health_exc_reason(e), None)
    return (True, "ok", latency_ms)


def instagram_run_health_check() -> dict:
    """Sondea todos los métodos contra el post canario y persiste el estado del día."""
    canary = INSTAGRAM_HEALTH_CANARY_URL
    probes = {
        "direct": lambda: _health_probe_direct(_ig_shortcode_from_url(canary)),
        "instapdown": lambda: _health_probe_instapdown(canary),
        "downreels": lambda: _health_probe_downreels(canary),
        "fastvidl": lambda: _health_probe_fastvidl(canary),
        "nuelink": lambda: _health_probe_nuelink(canary),
        "listnr": lambda: _health_probe_listnr(canary),
    }
    for host in INSTAGRAM_FIXER_HOSTS:
        probes[f"fixer:{host}"] = lambda host=host: _health_probe_fixer(host, canary)

    methods = {}
    for name, probe in probes.items():
        try:
            alive, reason, latency_ms = probe()
        except Exception as e:
            alive, reason, latency_ms = False, _health_exc_reason(e), None
        record = {
            "alive": bool(alive),
            "reason": str(reason)[:100],
            "at": datetime.now(BA_TZ).isoformat(timespec="seconds"),
        }
        if alive and isinstance(latency_ms, (int, float)):
            record["latency_ms"] = int(latency_ms)
        methods[name] = record

    state = {
        "date": _health_today(),
        "checked_at": datetime.now(BA_TZ).isoformat(timespec="seconds"),
        "canary_url": canary,
        "probe_timeout_s": INSTAGRAM_PROBE_TIMEOUT,
        "methods": methods,
    }
    with _HEALTH_LOCK:
        _save_health_state_locked(state)

    parts = []
    for name, rec in methods.items():
        if rec["alive"] and isinstance(rec.get("latency_ms"), int):
            parts.append(f"{name}=vivo ({rec['latency_ms']} ms)")
        else:
            parts.append(f"{name}={'vivo' if rec['alive'] else 'MUERTO'}")
    logger.info("Instagram health check: %s", ", ".join(parts))
    return state


_DEFAULT_API_ORDER = ("instapdown", "downreels", "fastvidl", "nuelink", "listnr")


def instagram_ranked_stages() -> list:
    """Etapas de terceros ordenadas por la latencia medida en el chequeo de hoy.

    La etapa 'fixers' se ubica según su fixer vivo más rápido (dentro de la
    etapa los hosts se ordenan con `_ranked_fixer_hosts`). Los vivos sin
    latencia medida van después, y los muertos al final (igual se saltean).
    Sin datos de hoy devuelve el orden por defecto.
    """
    stages = ["fixers", *_DEFAULT_API_ORDER]
    state = _load_health_state()
    if state.get("date") != _health_today():
        return list(stages)
    methods = state.get("methods", {})
    default_idx = {name: i for i, name in enumerate(stages)}

    def fixer_records() -> list:
        return [
            r for n, r in methods.items()
            if isinstance(r, dict) and n.startswith("fixer:")
        ]

    def stage_key(name: str):
        if name == "fixers":
            lats = [
                r["latency_ms"] for r in fixer_records()
                if r.get("alive") and isinstance(r.get("latency_ms"), int)
            ]
            if lats:
                return (0, float(min(lats)), default_idx[name])
            if any(r.get("alive") for r in fixer_records()):
                return (1, 0.0, default_idx[name])
            return (2, 0.0, default_idx[name])
        record = methods.get(name)
        if isinstance(record, dict) and record.get("alive"):
            latency = record.get("latency_ms")
            if isinstance(latency, int):
                return (0, float(latency), default_idx[name])
            return (1, 0.0, default_idx[name])
        return (2, 0.0, default_idx[name])

    return sorted(stages, key=stage_key)


def _ranked_fixer_hosts() -> list:
    hosts = list(INSTAGRAM_FIXER_HOSTS)
    state = _load_health_state()
    if state.get("date") != _health_today():
        return hosts
    methods = state.get("methods", {})
    default_idx = {host: i for i, host in enumerate(hosts)}

    def host_key(host: str):
        record = methods.get(f"fixer:{host}")
        if isinstance(record, dict) and record.get("alive"):
            latency = record.get("latency_ms")
            if isinstance(latency, int):
                return (0, float(latency), default_idx[host])
            return (1, 0.0, default_idx[host])
        return (2, 0.0, default_idx[host])

    return sorted(hosts, key=host_key)


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


def _ig_download_via_downreels(url: str, on_item=None, trace: _RouteTrace = None) -> list:
    """Use downreels.com's JSON API, which resolves direct fbcdn media URLs."""
    if not INSTAGRAM_DOWNREELS_API_URL:
        return []
    if not method_alive("downreels"):
        if trace:
            trace.add("downreels", "salteado (muerto hoy)")
        return []

    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            r = client.post(
                INSTAGRAM_DOWNREELS_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Content-Type": "application/json",
                    "Referer": "https://downreels.com/",
                },
                json={"url": url},
            )
            if r.status_code != 200:
                logger.debug("downreels HTTP %s", r.status_code)
                if r.status_code >= 500:
                    _mark_method_dead("downreels", f"http {r.status_code}")
                if trace:
                    trace.add("downreels", f"http {r.status_code}")
                return []
            payload = r.json()
    except Exception as e:
        logger.debug("downreels request failed: %s", e)
        _mark_method_dead("downreels", _health_exc_reason(e))
        if trace:
            trace.add("downreels", "error")
        return []

    entries = payload.get("videos")
    if payload.get("status") != "ok" or not entries:
        if trace:
            trace.add("downreels", "sin media")
        return []

    results = []
    seen = set()
    seen_hashes = set()
    for entry in entries:
        media_url = entry.get("url")
        if not media_url or media_url in seen:
            continue
        seen.add(media_url)
        item_type = "video" if entry.get("isVideo", True) else "image"
        downloaded = _download_cdn_url(media_url, item_type, headers=BROWSER_HEADERS)
        if downloaded:
            _emit_downloaded_item(downloaded, results, on_item, seen_hashes=seen_hashes)

    if results:
        logger.info("Instagram media downloaded via downreels")
        if trace:
            trace.add("downreels", "ok")
    elif trace:
        trace.add("downreels", "cdn sin descargas")
    return results


def _ig_download_via_fastvidl(url: str, on_item=None, trace: _RouteTrace = None) -> list:
    """Use fastvidl.com's JSON API, which resolves direct CDN media URLs."""
    if not INSTAGRAM_FASTVIDL_API_URL:
        return []
    if not method_alive("fastvidl"):
        if trace:
            trace.add("fastvidl", "salteado (muerto hoy)")
        return []

    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            r = client.post(
                INSTAGRAM_FASTVIDL_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Content-Type": "application/json",
                    "Referer": "https://fastvidl.com/instagram-video-downloader-free",
                },
                json={"url": url},
            )
            if r.status_code != 200:
                logger.debug("fastvidl HTTP %s", r.status_code)
                if r.status_code >= 500:
                    _mark_method_dead("fastvidl", f"http {r.status_code}")
                if trace:
                    trace.add("fastvidl", f"http {r.status_code}")
                return []
            payload = r.json()
    except Exception as e:
        logger.debug("fastvidl request failed: %s", e)
        _mark_method_dead("fastvidl", _health_exc_reason(e))
        if trace:
            trace.add("fastvidl", "error")
        return []

    entries = payload.get("media")
    if not payload.get("ok") or not entries:
        if trace:
            trace.add("fastvidl", "sin media")
        return []

    results = []
    seen = set()
    seen_hashes = set()
    for entry in entries:
        media_url = entry.get("url")
        if not media_url or media_url in seen:
            continue
        seen.add(media_url)
        item_type = "video" if entry.get("type") == "video" else "image"
        downloaded = _download_cdn_url(media_url, item_type, headers=BROWSER_HEADERS)
        if downloaded:
            _emit_downloaded_item(downloaded, results, on_item, seen_hashes=seen_hashes)

    if results:
        logger.info("Instagram media downloaded via fastvidl")
        if trace:
            trace.add("fastvidl", "ok")
    elif trace:
        trace.add("fastvidl", "cdn sin descargas")
    return results


def _ig_download_via_nuelink(url: str, on_item=None, trace: _RouteTrace = None) -> list:
    """Use nuelink.com's API, which mirrors the media to its own storage."""
    if not INSTAGRAM_NUELINK_API_URL:
        return []
    if not method_alive("nuelink"):
        if trace:
            trace.add("nuelink", "salteado (muerto hoy)")
        return []

    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            r = client.get(
                INSTAGRAM_NUELINK_API_URL,
                params={"link": url},
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Referer": "https://nuelink.com/tools/instagram-video-downloader",
                },
            )
            if r.status_code != 200:
                logger.debug("nuelink HTTP %s", r.status_code)
                if r.status_code >= 500:
                    _mark_method_dead("nuelink", f"http {r.status_code}")
                if trace:
                    trace.add("nuelink", f"http {r.status_code}")
                return []
            payload = r.json()
    except Exception as e:
        logger.debug("nuelink request failed: %s", e)
        _mark_method_dead("nuelink", _health_exc_reason(e))
        if trace:
            trace.add("nuelink", "error")
        return []

    media_url = payload.get("data")
    if payload.get("error") or not media_url:
        if trace:
            trace.add("nuelink", "sin media")
        return []

    item_type = "image" if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", media_url, re.I) else "video"
    downloaded = _download_cdn_url(media_url, item_type, headers=BROWSER_HEADERS)
    results = []
    if downloaded:
        _emit_downloaded_item(downloaded, results, on_item)

    if results:
        logger.info("Instagram media downloaded via nuelink")
        if trace:
            trace.add("nuelink", "ok")
    elif trace:
        trace.add("nuelink", "cdn sin descargas")
    return results


def _ig_download_via_listnr(url: str, on_item=None, trace: _RouteTrace = None) -> list:
    """Use listnr.ai's API, which proxies media via a signed JWT link."""
    if not INSTAGRAM_LISTNR_API_URL:
        return []
    if not method_alive("listnr"):
        if trace:
            trace.add("listnr", "salteado (muerto hoy)")
        return []

    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            r = client.post(
                INSTAGRAM_LISTNR_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Content-Type": "application/json",
                    "Referer": "https://listnr.ai/instagram-video-downloader",
                    "Origin": "https://listnr.ai",
                },
                json={"url": url, "platform": "instagram", "type": "video"},
            )
            if r.status_code != 200:
                logger.debug("listnr HTTP %s", r.status_code)
                if r.status_code >= 500:
                    _mark_method_dead("listnr", f"http {r.status_code}")
                if trace:
                    trace.add("listnr", f"http {r.status_code}")
                return []
            payload = r.json()
    except Exception as e:
        logger.debug("listnr request failed: %s", e)
        _mark_method_dead("listnr", _health_exc_reason(e))
        if trace:
            trace.add("listnr", "error")
        return []

    media_url = payload.get("url")
    if not media_url:
        if trace:
            trace.add("listnr", "sin media")
        return []

    downloaded = _download_cdn_url(media_url, "video", headers=BROWSER_HEADERS)
    results = []
    if downloaded:
        _emit_downloaded_item(downloaded, results, on_item)

    if results:
        logger.info("Instagram media downloaded via listnr")
        if trace:
            trace.add("listnr", "ok")
    elif trace:
        trace.add("listnr", "cdn sin descargas")
    return results


def _ig_download_via_instapdown(url: str, on_item=None, trace: _RouteTrace = None) -> list:
    """Use instapdown.com's JSON API, which returns direct CDN media URLs."""
    if not INSTAGRAM_INSTAPDOWN_API_URL:
        return []
    if not method_alive("instapdown"):
        if trace:
            trace.add("instapdown", "salteado (muerto hoy)")
        return []

    if _ig_story_path_from_url(url):
        variant = "story"
    elif re.search(r"/(?:reel|reels|tv)/", url):
        variant = "reels"
    else:
        variant = "carousel"

    try:
        with httpx.Client(timeout=INSTAGRAM_PROBE_TIMEOUT) as client:
            r = client.post(
                INSTAGRAM_INSTAPDOWN_API_URL,
                headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"url": url, "variant": variant},
            )
            if r.status_code != 200:
                logger.debug("instapdown HTTP %s", r.status_code)
                if r.status_code >= 500:
                    _mark_method_dead("instapdown", f"http {r.status_code}")
                if trace:
                    trace.add("instapdown", f"http {r.status_code}")
                return []
            payload = r.json()
    except Exception as e:
        logger.debug("instapdown request failed: %s", e)
        _mark_method_dead("instapdown", _health_exc_reason(e))
        if trace:
            trace.add("instapdown", "error")
        return []

    entries = payload.get("items")
    if not payload.get("ok") or not entries:
        if trace:
            trace.add("instapdown", "sin media")
        return []

    results = []
    seen = set()
    seen_hashes = set()
    for entry in entries:
        media_url = entry.get("url")
        if not media_url or media_url in seen:
            continue
        seen.add(media_url)
        item_type = "video" if entry.get("kind") == "video" else "image"
        downloaded = _download_cdn_url(media_url, item_type, headers=BROWSER_HEADERS)
        if downloaded:
            _emit_downloaded_item(downloaded, results, on_item, seen_hashes=seen_hashes)

    if results:
        logger.info("Instagram media downloaded via instapdown")
        if trace:
            trace.add("instapdown", "ok")
    elif trace:
        trace.add("instapdown", "cdn sin descargas")
    return results


def _ig_collect_fixer_items(client: httpx.Client, host: str, url: str) -> tuple:
    path = _ig_path_from_url(url)
    if not path or not INSTAGRAM_FIXER_HOSTS:
        return ([], None)

    fixer_parts = urlsplit(url)
    fixer_url = urlunsplit(("https", host, path, fixer_parts.query, ""))
    r = client.get(fixer_url, headers=YDL_HTTP_HEADERS)
    if r.status_code != 200:
        logger.debug(f"Instagram fixer {host} returned HTTP {r.status_code}")
        return ([], r.status_code)

    return (_extract_og_media_items(r.text), r.status_code)


def _ig_download_via_fixers(url: str, source_url: str = None, on_item=None, trace: _RouteTrace = None) -> list:
    path = _ig_path_from_url(url)
    if not path or not INSTAGRAM_FIXER_HOSTS:
        return []

    source_url = source_url or url
    prefer_video = bool(re.search(r"/(?:reel|reels|tv)/", path))
    is_post = bool(re.search(r"/p/[A-Za-z0-9_-]+/?$", path))
    requested_img_index = _ig_img_index_from_url(source_url)
    should_probe_carousel = is_post and not prefer_video

    host_outcomes = []
    for host in _ranked_fixer_hosts():
        if not method_alive(f"fixer:{host}"):
            host_outcomes.append(f"{host}=muerto hoy")
            continue

        results = []
        items = []
        seen = set()
        seen_hashes = set()
        duplicate_or_empty_seen = False
        host_reason = None
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=INSTAGRAM_PROBE_TIMEOUT,
                verify=INSTAGRAM_FIXER_VERIFY_SSL,
            ) as client:
                base_items, base_status = _ig_collect_fixer_items(client, host, url)
                if base_status and base_status >= 500 and not base_items:
                    _mark_method_dead(f"fixer:{host}", f"http {base_status}")
                    host_outcomes.append(f"{host}=http {base_status} (muerto hoy)")
                    continue
                _append_unique_media_items(items, base_items, seen)

                if should_probe_carousel:
                    for index in range(1, INSTAGRAM_MAX_CAROUSEL_ITEMS + 1):
                        indexed_url = _ig_url_with_img_index(url, index)
                        indexed_items, _ = _ig_collect_fixer_items(
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
            _mark_method_dead(f"fixer:{host}", _health_exc_reason(e))
            host_outcomes.append(f"{host}=error (muerto hoy)")
            continue

        if not items:
            host_reason = "sin media"
        elif prefer_video and not any(item["type"] == "video" for item in items):
            host_reason = "solo imágenes"
        elif requested_img_index and len(items) < requested_img_index:
            host_reason = "carousel incompleto"
        elif should_probe_carousel and len(items) == 1 and not duplicate_or_empty_seen:
            host_reason = "carousel sin probing"

        if host_reason:
            logger.debug(f"Instagram fixer {host}: {host_reason}")
            host_outcomes.append(f"{host}={host_reason}")
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
                host_reason = f"cdn {status_code}"
                results = []
                break
            elif status_code:
                logger.debug(
                    "Instagram fixer %s media download returned HTTP %s for %s",
                    host,
                    status_code,
                    item["type"],
                )
                if not host_reason:
                    host_reason = f"cdn {status_code}"
            else:
                logger.debug(
                    "Instagram fixer %s media download failed without HTTP status for %s",
                    host,
                    item["type"],
                )
                if not host_reason:
                    host_reason = "cdn sin status"

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
                host_outcomes.append(f"{host}=carousel incompleto descarga")
                continue
            logger.info(f"Instagram media downloaded via fixer {host}")
            if trace:
                trace.add("fixers", f"ok vía {host}")
            return results

        host_outcomes.append(f"{host}={host_reason or 'sin media descargable'}")

    summary = ", ".join(host_outcomes) if host_outcomes else "sin hosts"
    logger.info(f"Instagram fixers no resolvieron el post: {summary}")
    if trace:
        trace.add("fixers", summary)
    return []


def _new_instaloader() -> "instaloader.Instaloader":
    return instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
        max_connection_attempts=1,
    )


def _ig_download_direct(url: str, on_item=None, trace: _RouteTrace = None) -> list:
    """Use anonymous Instaloader access for public Instagram posts."""
    shortcode = _ig_shortcode_from_url(url)
    if not shortcode:
        raise DownloadError("Link de Instagram inválido.")

    L = _new_instaloader()

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as e:
        message = str(e)
        if _is_instagram_auth_or_rate_limit_error(message):
            _trip_instagram_circuit(message)
            _mark_method_dead("direct", "bloqueo auth")
            if trace:
                trace.add("direct", "bloqueo auth (graphql)")
            raise DownloadError(
                "Instagram bloqueó el acceso público desde esta VM. "
                "El bot no usa ninguna cuenta de Instagram."
            ) from e
        if trace:
            trace.add("direct", "post no extraíble")
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
        if trace:
            trace.add("direct", "sin media")
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
            _mark_method_dead("direct", f"cdn http {status_code}")
            if trace:
                trace.add("direct", f"cdn {status_code}")
            raise DownloadError(
                "Instagram bloqueó la descarga pública desde esta VM."
            )
    if results:
        return results

    if trace:
        trace.add("direct", "cdn sin descargas")
    raise DownloadError(
        "Instagram resolvió el post, pero no pude bajar los archivos desde la CDN."
    )


def _ig_download(url: str, source_url: str = None, on_item=None, trace: _RouteTrace = None) -> list:
    source_url = source_url or url
    api_runners = {
        "instapdown": lambda: _ig_download_via_instapdown(url, on_item=on_item, trace=trace),
        "downreels": lambda: _ig_download_via_downreels(url, on_item=on_item, trace=trace),
        "fastvidl": lambda: _ig_download_via_fastvidl(url, on_item=on_item, trace=trace),
        "nuelink": lambda: _ig_download_via_nuelink(url, on_item=on_item, trace=trace),
        "listnr": lambda: _ig_download_via_listnr(url, on_item=on_item, trace=trace),
    }

    if _ig_story_path_from_url(url):
        fixer_results = _ig_download_via_fixers(
            url, source_url=source_url, on_item=on_item, trace=trace
        )
        if fixer_results:
            return fixer_results
        saveinsta_results = _ig_download_story_via_saveinsta(url, on_item=on_item)
        if saveinsta_results:
            return saveinsta_results
        for stage in instagram_ranked_stages():
            runner = api_runners.get(stage)
            if runner is None:
                continue
            results = runner()
            if results:
                return results
        if trace:
            trace.add("story", "no disponible")
        instagram_note_total_failure()
        raise DownloadError(
            "No pude obtener esa historia de Instagram con los métodos alternativos. "
            "Puede haber vencido, ser privada o no estar disponible públicamente."
        )

    direct_error = None
    if method_alive("direct"):
        try:
            return _ig_download_direct(url, on_item=on_item, trace=trace)
        except DownloadError as e:
            direct_error = e
    elif trace:
        trace.add("direct", "salteado (muerto hoy)")

    runners = {
        "fixers": lambda: _ig_download_via_fixers(
            url, source_url=source_url, on_item=on_item, trace=trace
        ),
        **api_runners,
    }
    for stage in instagram_ranked_stages():
        results = runners[stage]()
        if results:
            return results
    instagram_note_total_failure()
    if direct_error:
        raise direct_error
    raise DownloadError(
        "No pude obtener el contenido de Instagram por ningún método disponible hoy."
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


_TWITTER_SHORTCUT_RE = re.compile(
    r"\s*(?:https?://)?(?:t\.co|pic\.twitter\.com)/\S+\s*"
)


def _strip_twitter_shortcuts(text: str) -> str:
    """Drop t.co / pic.twitter.com shortcuts: the caption already carries the X link."""
    return _TWITTER_SHORTCUT_RE.sub(" ", text).strip()


def _post_text_from_info(info) -> str:
    """Extract the post text from yt-dlp metadata (tweet text lives in description)."""
    if not isinstance(info, dict):
        return ""
    text = (info.get("description") or "").strip()
    if not text:
        entries = info.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                candidate = (entry.get("description") or entry.get("title") or "").strip()
                if candidate:
                    text = candidate
                    break
    return _strip_twitter_shortcuts(text)


def _tweet_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)", url)
    return m.group(1) if m else None


_TWITTER_IMG_URL_RE = re.compile(
    r"\.(?:jpe?g|png|webp)(?:[?#]|$)|[?&]format=(?:jpe?g|png|webp)", re.I
)


def _twitter_media_type(media_url: str, kind: str = "") -> str:
    kind = (kind or "").lower()
    if kind in ("photo", "image"):
        return "image"
    if kind in ("video", "gif"):
        return "video"
    if _TWITTER_IMG_URL_RE.search(media_url or ""):
        return "image"
    return "video"


def _twitter_api_payload_media(payload) -> tuple:
    """Extract (media_items, text) from fxtwitter or vxtwitter JSON responses."""
    if not isinstance(payload, dict):
        return [], ""
    tweet = payload.get("tweet")
    if isinstance(tweet, dict):  # fxtwitter shape
        entries = (tweet.get("media") or {}).get("all") or []
        text = (tweet.get("text") or "").strip()
    else:  # vxtwitter shape
        entries = payload.get("media_extended") or []
        text = (payload.get("text") or "").strip()

    items = []
    seen = set()

    def _add(media_url, kind):
        if not media_url or media_url in seen:
            return
        seen.add(media_url)
        items.append(
            {"type": _twitter_media_type(media_url, kind), "cdn_url": media_url}
        )

    for entry in entries:
        if isinstance(entry, dict):
            _add(entry.get("url"), entry.get("type"))

    if not items:  # vxtwitter also exposes a plain mediaURLs list
        for media_url in payload.get("mediaURLs") or []:
            _add(media_url, "")
    return items, text


def _twitter_download_via_apis(url: str, on_item=None) -> list:
    """Download X post media via fxtwitter/vxtwitter public APIs.

    yt-dlp fails on image-only tweets and on login-walled ones; these APIs
    resolve both, including the post text.
    """
    tweet_id = _tweet_id_from_url(url)
    if not tweet_id:
        return []

    api_headers = {
        "User-Agent": YDL_HTTP_HEADERS["User-Agent"],
        "Accept": "application/json",
    }
    media_headers = {
        "User-Agent": YDL_HTTP_HEADERS["User-Agent"],
        "Accept": "*/*",
    }
    for api_template in (TWITTER_FXTWITTER_API_URL, TWITTER_VXTWITTER_API_URL):
        if not api_template:
            continue
        try:
            with httpx.Client(timeout=30) as client:
                r = client.get(api_template.format(id=tweet_id), headers=api_headers)
                if r.status_code != 200:
                    logger.debug("Twitter API %s HTTP %s", api_template, r.status_code)
                    continue
                payload = r.json()
        except Exception as e:
            logger.debug("Twitter API %s failed: %s", api_template, e)
            continue

        media_items, text = _twitter_api_payload_media(payload)
        if not media_items:
            continue

        results = []
        for item in media_items:
            downloaded = _download_cdn_url(
                item["cdn_url"], item["type"], headers=media_headers
            )
            if downloaded:
                if text:
                    downloaded["post_text"] = _strip_twitter_shortcuts(text)
                results.append(downloaded)
                if on_item:
                    on_item(downloaded)
        if results:
            logger.info(
                "Twitter media downloaded via %s", urlsplit(api_template).netloc
            )
            return results
    return []


def download_media(url: str, on_item=None) -> list:
    """
    Download all media from a URL.
    Returns list of: {'type': 'video'|'image', 'path': str, 'mime': str}
    Caller is responsible for deleting the temp files.
    """
    source_url = url.strip()
    url = _normalize_url(source_url)

    instagram_error = None
    instagram_trace = _RouteTrace()
    if is_instagram(url):
        try:
            return _ig_download(
                url, source_url=source_url, on_item=on_item, trace=instagram_trace
            )
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
    info = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = sorted([
            f for f in os.listdir(tmp_dir)
            if not f.endswith((".part", ".ytdl"))
        ])
        ytdlp_ok = bool(files)
    except Exception as e:
        logger.error(f"yt-dlp error for {url}: {e}")
        if is_instagram(url) and _is_instagram_auth_or_rate_limit_error(str(e)):
            instagram_trace.add("yt-dlp", "auth/rate-limit")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if cookiefile:
                err = DownloadError(
                    "Instagram bloqueó temporalmente la sesión o las cookies vencieron. "
                    "Esperá unos minutos, renová `cookies.txt` y reintentá."
                )
            else:
                err = DownloadError(
                    "Instagram bloqueó el acceso anónimo desde esta VM. "
                    "El bot no usa ninguna cuenta de Instagram."
                )
            err.route_trace = instagram_trace.summary()
            err.route_key = instagram_trace.key()
            raise err from e

    if ytdlp_ok:
        post_text = _post_text_from_info(info) if is_twitter(url) else ""
        results = []
        for fname in sorted([f for f in os.listdir(tmp_dir) if not f.endswith((".part", ".ytdl"))]):
            fpath = os.path.join(tmp_dir, fname)
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            mime = MIME_MAP.get(ext, "application/octet-stream")
            ftype = "video" if "video" in mime else "image"
            item = {"type": ftype, "path": fpath, "mime": mime, "_dir": tmp_dir}
            if post_text:
                item["post_text"] = post_text
            results.append(item)
            if on_item:
                on_item(item)
        return results

    shutil.rmtree(tmp_dir, ignore_errors=True)

    if instagram_error:
        instagram_trace.add("yt-dlp", "sin salida")
        instagram_error.route_trace = instagram_trace.summary()
        instagram_error.route_key = instagram_trace.key()
        raise instagram_error

    # fxtwitter/vxtwitter fallback: yt-dlp no resuelve posts solo-imagen ni
    # los que piden login
    if is_twitter(url):
        return _twitter_download_via_apis(url, on_item=on_item)

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
