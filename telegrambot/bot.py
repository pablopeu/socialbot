import asyncio
import json
import logging
import os
import queue
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from downloader import (
    DownloadError,
    download_media,
    instagram_status,
    is_instagram,
    is_twitter,
    is_facebook,
    is_tiktok,
    is_threads,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(Path(__file__).parent / "bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _send_downloaded_item(update: Update, item: dict, caption: str):
    path = item["path"]
    try:
        with open(path, "rb") as f:
            if item["type"] == "video":
                await update.message.reply_video(
                    f,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                await update.message.reply_photo(f, caption=caption)
    finally:
        if not item.get("_dir"):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


async def _download_and_send_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, status):
    user = update.effective_user
    item_queue = queue.Queue()
    done = object()
    result = {"items": [], "error": None}

    def on_item(item: dict):
        item_queue.put(item)

    def worker():
        try:
            result["items"] = download_media(text, on_item=on_item)
        except Exception as e:
            result["error"] = e
        finally:
            item_queue.put(done)

    worker_task = asyncio.create_task(asyncio.to_thread(worker))
    sent = 0
    status_deleted = False
    dirs_to_clean = set()

    def cleanup_download_dirs():
        for d in dirs_to_clean:
            shutil.rmtree(d, ignore_errors=True)

    while True:
        item = await asyncio.to_thread(item_queue.get)
        if item is done:
            break

        sent += 1
        if item.get("_dir"):
            dirs_to_clean.add(item["_dir"])
        if not status_deleted:
            try:
                await status.delete()
            except TelegramError:
                pass
            status_deleted = True

        caption = f"{sent} - {text}" if sent > 1 else text
        try:
            await _send_downloaded_item(update, item, caption)
        except TelegramError as e:
            logger.error(f"Telegram error sending Instagram item {sent}: {e}")
            await update.message.reply_text(
                f"No pude enviar el archivo {sent}: el archivo puede ser demasiado grande (límite 50 MB)."
            )
        except Exception as e:
            logger.error(f"Error sending Instagram item {sent}: {e}")
            await update.message.reply_text(f"Error al enviar el archivo {sent}.")

    await worker_task

    if result["error"]:
        if isinstance(result["error"], DownloadError):
            logger.error(f"Download error for {text}: {result['error']}")
            await _maybe_notify_instagram_admin(context, user.id, text, str(result["error"]))
            if sent:
                await update.message.reply_text(str(result["error"]))
            else:
                await status.edit_text(str(result["error"]))
            cleanup_download_dirs()
            return

        logger.error(f"Error in download_media: {result['error']}")
        if sent:
            await update.message.reply_text("Error inesperado al descargar el contenido.")
        else:
            await status.edit_text("Error inesperado al descargar el contenido.")
        cleanup_download_dirs()
        return

    if not sent:
        await status.edit_text(
            "No pude obtener el contenido de Instagram.\n"
            "El post puede ser privado o el link inválido."
        )
        cleanup_download_dirs()
        return

    cleanup_download_dirs()
    logger.info(f"Sent {sent} Instagram item(s) to user {user.id}")

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
ALLOWED_USERS_PATH = BASE_DIR / "allowed_users.txt"
INSTAGRAM_ALERT_STATE_PATH = BASE_DIR / "instagram_alert_state.json"
INSTAGRAM_ALERT_LOCK = asyncio.Lock()


def load_token() -> str:
    with open(CONFIG_PATH) as f:
        return json.load(f)["token"]


def _parse_allowed_users() -> list[tuple[str, str]]:
    """
    Parse allowed_users.txt and return list of (id, comment) tuples.
    Supports inline comments: '123456789  # Pablo'
    Lines starting with # are skipped entirely.
    """
    if not ALLOWED_USERS_PATH.exists():
        return []
    result = []
    for line in ALLOWED_USERS_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("#", 1)
        user_id = parts[0].strip()
        comment = parts[1].strip() if len(parts) > 1 else ""
        if user_id.isdigit():
            result.append((user_id, comment))
    return result


def is_allowed(user_id: int) -> bool:
    return any(uid == str(user_id) for uid, _ in _parse_allowed_users())


def get_admin_id() -> Optional[int]:
    """The first valid entry in allowed_users.txt is the admin."""
    entries = _parse_allowed_users()
    return int(entries[0][0]) if entries else None


def is_admin(user_id: int) -> bool:
    return user_id == get_admin_id()


def _load_instagram_alert_state() -> dict:
    if not INSTAGRAM_ALERT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(INSTAGRAM_ALERT_STATE_PATH.read_text())
    except Exception as e:
        logger.warning(f"Failed to read Instagram alert state: {e}")
        return {}


def _save_instagram_alert_state(state: dict):
    try:
        INSTAGRAM_ALERT_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=True, indent=2) + "\n"
        )
    except Exception as e:
        logger.warning(f"Failed to write Instagram alert state: {e}")


def _should_notify_instagram_admin(error_text: str) -> bool:
    text = (error_text or "").lower()
    ignored_errors = ("inválido", "historia de instagram")
    return not any(error in text for error in ignored_errors)


async def _maybe_notify_instagram_admin(
    context: ContextTypes.DEFAULT_TYPE,
    requested_by: int,
    url: str,
    error_text: str,
):
    admin_id = get_admin_id()
    if not admin_id or not _should_notify_instagram_admin(error_text):
        return

    today = datetime.now().date().isoformat()
    async with INSTAGRAM_ALERT_LOCK:
        state = _load_instagram_alert_state()
        if state.get("instagram_failure_alert_date") == today:
            return

        message = (
            "Alerta Instagram: fallaron todas las rutas del bot para un link.\n"
            f"Fecha: {today}\n"
            f"Solicitado por: {requested_by}\n"
            f"URL: {url}\n"
            f"Error: {error_text}"
        )
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            logger.error(f"Failed to send Instagram admin alert: {e}")
            return

        state["instagram_failure_alert_date"] = today
        state["instagram_failure_alert_url"] = url
        state["instagram_failure_alert_error"] = error_text
        state["instagram_failure_alert_requested_by"] = requested_by
        _save_instagram_alert_state(state)


# --- Handlers ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("No tenés acceso. Contactate con el admin.")
        return
    await update.message.reply_text(
        "Hola! Mandame un link de Instagram, Threads, Twitter/X, Facebook o TikTok "
        "y te bajo las fotos y videos del post."
    )


async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /agregar ID [nombre]")
        return

    new_id = context.args[0].strip()
    if not new_id.isdigit():
        await update.message.reply_text("El ID debe ser un número. Ejemplo: /agregar 123456789 Juan")
        return

    if is_allowed(int(new_id)):
        await update.message.reply_text(f"El usuario {new_id} ya está en la lista.")
        return

    comment = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    line = f"{new_id}  # {comment}" if comment else new_id

    with open(ALLOWED_USERS_PATH, "a") as f:
        f.write(f"\n{line}")

    msg = f"Usuario {new_id} agregado."
    if comment:
        msg += f" ({comment})"
    await update.message.reply_text(msg)
    logger.info(f"Admin agregó usuario {new_id} ({comment})")


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /borrar ID")
        return

    target_id = context.args[0].strip()
    if not target_id.isdigit():
        await update.message.reply_text("El ID debe ser un número. Ejemplo: /borrar 123456789")
        return

    if int(target_id) == get_admin_id():
        await update.message.reply_text("No podés borrar al admin.")
        return

    if not is_allowed(int(target_id)):
        await update.message.reply_text(f"El usuario {target_id} no está en la lista.")
        return

    # Rewrite file preserving comments and order, removing the target line
    lines = ALLOWED_USERS_PATH.read_text().splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        line_id = stripped.split("#")[0].strip()
        if line_id == target_id:
            continue  # drop this line
        new_lines.append(line)

    ALLOWED_USERS_PATH.write_text("\n".join(new_lines))
    await update.message.reply_text(f"Usuario {target_id} eliminado.")
    logger.info(f"Admin eliminó usuario {target_id}")


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return

    entries = _parse_allowed_users()
    if not entries:
        await update.message.reply_text("La lista está vacía.")
        return

    lines = ["*Usuarios permitidos:*"]
    for i, (uid, comment) in enumerate(entries):
        label = f"_{comment}_" if comment else "_sin nombre_"
        tag = " (admin)" if i == 0 else ""
        lines.append(f"• `{uid}` — {label}{tag}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_instagram_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return

    status = instagram_status()
    alert_state = _load_instagram_alert_state()

    lines = ["Estado Instagram:"]
    fixers = ", ".join(status["fixer_hosts"]) if status["fixer_hosts"] else "ninguno"
    lines.append(f"Fixers: {fixers}")
    lines.append(f"Max carousel fixer: {status['max_carousel_items']}")
    lines.append(
        f"Verificacion SSL fixers: {'on' if status['fixer_verify_ssl'] else 'off'}"
    )
    if alert_state.get("instagram_failure_alert_date"):
        lines.append(f"Ultima alerta admin: {alert_state['instagram_failure_alert_date']}")
    else:
        lines.append("Ultima alerta admin: ninguna")

    await update.message.reply_text("\n".join(lines))


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("No tenés acceso. Contactate con el admin.")
        return

    text = (update.message.text or "").strip()
    if not (is_instagram(text) or is_twitter(text) or is_facebook(text) or is_tiktok(text) or is_threads(text)):
        await update.message.reply_text(
            "Mandame un link de Instagram, Threads, Twitter/X, Facebook o TikTok."
        )
        return

    if is_instagram(text):
        platform = "Instagram"
    elif is_threads(text):
        platform = "Threads"
    elif is_facebook(text):
        platform = "Facebook"
    elif is_tiktok(text):
        platform = "TikTok"
    else:
        platform = "Twitter/X"
    logger.info(f"User {user.id} requested: {text}")

    status = await update.message.reply_text(f"Procesando tu link de {platform}...")

    if is_instagram(text):
        await _download_and_send_instagram(update, context, text, status)
        return

    try:
        items = await asyncio.to_thread(download_media, text)
    except DownloadError as e:
        logger.error(f"Download error for {text}: {e}")
        await status.edit_text(str(e))
        return
    except Exception as e:
        logger.error(f"Error in download_media: {e}")
        await status.edit_text("Error inesperado al descargar el contenido.")
        return

    if not items:
        await status.edit_text(
            f"No pude obtener el contenido de {platform}.\n"
            "El post puede ser privado o el link inválido."
        )
        return

    await status.delete()

    total = len(items)
    dirs_to_clean = set()

    for i, item in enumerate(items, 1):
        caption = f"{i}/{total} — {text}" if total > 1 else text
        if item.get("_dir"):
            dirs_to_clean.add(item["_dir"])

        try:
            await _send_downloaded_item(update, item, caption)
        except TelegramError as e:
            logger.error(f"Telegram error sending item {i}: {e}")
            await update.message.reply_text(
                f"No pude enviar el archivo {i}: el archivo puede ser demasiado grande (límite 50 MB)."
            )
        except Exception as e:
            logger.error(f"Error sending item {i}: {e}")
            await update.message.reply_text(f"Error al enviar el archivo {i}.")

    for d in dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)

    logger.info(f"Sent {total} item(s) to user {user.id}")


def main():
    token = load_token()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("agregar", cmd_agregar))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("instagram_status", cmd_instagram_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    logger.info("Bot iniciado con polling.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
