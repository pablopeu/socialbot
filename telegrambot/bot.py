import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from downloader import download_media, is_instagram, is_twitter

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(Path(__file__).parent / "bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
ALLOWED_USERS_PATH = BASE_DIR / "allowed_users.txt"


def load_token() -> str:
    with open(CONFIG_PATH) as f:
        return json.load(f)["token"]


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS_PATH.exists():
        return False
    allowed = {
        line.strip()
        for line in ALLOWED_USERS_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    return str(user_id) in allowed


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("No tenés acceso. Contactate con el admin.")
        return
    await update.message.reply_text(
        "Hola! Mandame un link de Instagram o Twitter/X "
        "y te bajo las fotos y videos del post."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("No tenés acceso. Contactate con el admin.")
        return

    text = (update.message.text or "").strip()
    if not (is_instagram(text) or is_twitter(text)):
        await update.message.reply_text(
            "Mandame un link de Instagram o Twitter/X."
        )
        return

    platform = "Instagram" if is_instagram(text) else "Twitter/X"
    logger.info(f"User {user.id} requested: {text}")

    status = await update.message.reply_text(f"Procesando tu link de {platform}...")

    try:
        items = await asyncio.to_thread(download_media, text)
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
        path = item["path"]
        if item.get("_dir"):
            dirs_to_clean.add(item["_dir"])

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
        except TelegramError as e:
            logger.error(f"Telegram error sending item {i}: {e}")
            await update.message.reply_text(
                f"No pude enviar el archivo {i}: el archivo puede ser demasiado grande (límite 50 MB)."
            )
        except Exception as e:
            logger.error(f"Error sending item {i}: {e}")
            await update.message.reply_text(f"Error al enviar el archivo {i}.")
        finally:
            if not item.get("_dir"):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    for d in dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)

    logger.info(f"Sent {total} item(s) to user {user.id}")


def main():
    token = load_token()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    logger.info("Bot iniciado con polling.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
