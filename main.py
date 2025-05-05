import logging
import os
import asyncio
import json

# Убрали google.generativeai, deque и т.д. - пока не нужны

from telegram import Update
from telegram.ext import (
    Application,
    # MessageHandler убрали
    # filters убрали
    ContextTypes,
    TypeHandler, # <--- Возвращаем TypeHandler
)
# from telegram.constants import ChatType # Пока не нужен
from telegram.error import TelegramError

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Переменные окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID") # Оставляем на будущее

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit() # Оставляем проверку
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int."); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")

# --- Универсальный обработчик-логгер ---
async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Логирует ЛЮБОЕ полученное обновление в формате JSON."""
    try:
        # Используем ensure_ascii=False для корректного отображения кириллицы
        update_json = json.dumps(update.to_dict(), indent=2, ensure_ascii=False)
        logger.info(f"--- Received Raw Update ---:\n{update_json}")

        # --- ДОБАВИМ ОТПРАВКУ JSON В ЛИЧКУ ДЛЯ УДОБСТВА ---
        # Отправляем первые 3000 символов JSON в чат MY_TELEGRAM_ID
        # (Telegram имеет лимит на длину сообщения ~4096)
        try:
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=f"Received Update JSON:\n```json\n{update_json[:3000]}\n```" # Обрамляем в ```json для подсветки
                 + ("..." if len(update_json) > 3000 else ""), # Добавляем многоточие, если обрезали
                parse_mode='MarkdownV2' # Используем MarkdownV2 для блока кода
            )
            logger.info(f"Sent update JSON snippet to {MY_TELEGRAM_ID}")
        except TelegramError as send_e:
            logger.error(f"Failed to send update JSON to {MY_TELEGRAM_ID}: {send_e}")
            # Попробуем отправить просто текст, если Markdown не сработал
            try:
                 await context.bot.send_message(
                    chat_id=MY_TELEGRAM_ID,
                    text=f"Received Update (raw, first 3k chars):\n{str(update)[:3000]}"
                 )
            except Exception as send_e2:
                 logger.error(f"Failed to send raw update string to {MY_TELEGRAM_ID}: {send_e2}")
        # --- КОНЕЦ БЛОКА ОТПРАВКИ В ЛИЧКУ ---

    except Exception as log_e:
        logger.error(f"Error logging/processing update object: {log_e}", exc_info=True)
        logger.info(f"Received update (raw): {update}")

# --- Функция post_init (без изменений) ---
async def post_init(application: Application):
    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    logger.info(f"Attempting to set webhook using:")
    logger.info(f"  - Base URL (from env): {WEBHOOK_URL}")
    logger.info(f"  - Bot Token: {'*' * (len(BOT_TOKEN) - 4)}{BOT_TOKEN[-4:]}")
    logger.info(f"  - Final Webhook URL for set_webhook: {webhook_full_url}")
    if not webhook_full_url.startswith("https://"):
        logger.error(f"FATAL: The final webhook URL '{webhook_full_url}' does not start with https://.")
    try:
        # Указываем Telegram, что мы хотим получать бизнес-обновления
        # (это может быть важно!)
        allowed_updates = [
            "message", "edited_message", # Стандартные
            "business_connection", "business_message", # Бизнес-обновления
            "edited_business_message", "deleted_business_messages"
            # Можно добавить и другие типы по мере необходимости
        ]
        logger.info(f"Setting allowed_updates: {allowed_updates}")
        await application.bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=allowed_updates, # <--- Передаем список явно
            drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        # Проверим, установились ли наши allowed_updates
        if webhook_info.allowed_updates:
             logger.info(f"Effective allowed_updates: {webhook_info.allowed_updates}")
        else:
             logger.warning("Telegram did not report effective allowed_updates (might be okay).")

        if webhook_info.url == webhook_full_url:
            logger.info("Webhook successfully set!")
        else:
            logger.warning(f"Webhook URL reported by Telegram ({webhook_info.url}) differs from the URL we tried to set ({webhook_full_url}).")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}", exc_info=True)
        # Если вебхук не ставится, нет смысла продолжать
        raise e # Перевыбрасываем ошибку, чтобы asyncio.run упал

# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Logger Bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрируем ТОЛЬКО универсальный логгер ---
    application.add_handler(TypeHandler(Update, log_all_updates))
    logger.info("Registered TypeHandler to log all updates.")

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_runner = application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        )
        logger.info(f"application.run_webhook returned: {type(webhook_runner)}")
        if webhook_runner is None:
             # Это может случиться, если post_init вызвал исключение и оно не было перевыброшено
             logger.critical("CRITICAL ERROR: application.run_webhook returned None, possibly due to failed post_init.")
        else:
             asyncio.run(webhook_runner) # Запускаем корутину
    except ValueError as e:
        logger.critical(f"CRITICAL ERROR during asyncio.run: {e}", exc_info=True)
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")