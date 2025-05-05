import logging
import os
import asyncio
import json # Для красивого вывода JSON в лог

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    TypeHandler, # <--- Добавляем TypeHandler
)
from telegram.constants import ChatType
from telegram.error import TelegramError

# --- Настройки и переменные (без изменений) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")

if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")

# --- НОВЫЙ Обработчик для логирования ВСЕХ обновлений ---
async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Логирует любое полученное обновление в формате JSON."""
    logger.info(f"--- Received Raw Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")
    # Мы не будем ничего делать с этим обновлением здесь, только логировать.
    # Оно потом пойдет дальше к другим обработчикам (если они есть).

# --- Основной обработчик сообщений (пока оставляем как есть) ---
# Ожидаем, что он НЕ БУДЕТ срабатывать для Business сообщений,
# но пусть будет на случай, если придут и обычные.
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает стандартные сообщения и пересылает их."""
    logger.info(">>> handle_message triggered <<<") # Добавим лог, чтобы видеть, если он вдруг сработает

    # Логируем JSON и здесь, чтобы сравнить, если сработает
    logger.info(f"Update passed to handle_message: {update.to_json()}")

    message = update.message
    if not message:
        logger.debug("handle_message: Update does not contain a standard message.")
        return

    original_chat = message.chat
    sender = message.from_user

    if original_chat.id == MY_TELEGRAM_ID:
        logger.debug(f"handle_message: Ignoring message from target chat {MY_TELEGRAM_ID}.")
        return
    if sender and sender.id == context.bot.id:
        logger.debug("handle_message: Ignoring message from bot itself.")
        return

    # (Остальная логика пересылки из handle_message остается без изменений)
    # ... (код форматирования и отправки сообщения) ...
    sender_info = "Unknown Sender"
    if sender:
        sender_info = f"{sender.first_name}"
        if sender.last_name: sender_info += f" {sender.last_name}"
        if sender.username: sender_info += f" (@{sender.username})"
        sender_info += f" (ID: {sender.id})"

    chat_info = f"Chat ID: {original_chat.id}"
    if original_chat.title:
        chat_info = f"'{original_chat.title}' ({original_chat.id})"
    elif original_chat.type == ChatType.PRIVATE:
         chat_info = f"Private Chat ({original_chat.id})"

    message_text = message.text
    if not message_text:
        message_text = f"[Non-text message type: {message.effective_attachment.mime_type if message.effective_attachment else 'Unknown'}]"
        if message.caption: message_text += f"\nCaption: {message.caption}"

    def escape_markdown_v2(text: str) -> str:
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        escaped_text = ""
        for char in str(text):
            if char in escape_chars: escaped_text += f'\\{char}'
            else: escaped_text += char
        return escaped_text

    safe_sender_info = escape_markdown_v2(sender_info)
    safe_chat_info = escape_markdown_v2(chat_info)
    safe_message_text = escape_markdown_v2(message_text)

    forward_text = (
        f"📩 *New Message*\n\n"
        f"*From:* {safe_sender_info}\n"
        f"*In:* {safe_chat_info}\n"
        f"───────\n"
        f"{safe_message_text}"
    )
    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID, text=forward_text, parse_mode='MarkdownV2'
        )
        logger.info(f"handle_message: Forwarded message from chat {original_chat.id} to {MY_TELEGRAM_ID}")
    except TelegramError as e:
        logger.error(f"handle_message: Failed to forward message (MarkdownV2) to {MY_TELEGRAM_ID}: {e}")
        try:
             forward_text_plain = (
                f"📩 New Message\n\n"
                f"From: {sender_info}\n"
                f"In: {chat_info}\n"
                f"───────\n"
                f"{message_text}"
             )
             await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID, text=forward_text_plain, parse_mode=None
             )
             logger.info(f"handle_message: Forwarded message (plain text retry) to {MY_TELEGRAM_ID}")
        except Exception as e2:
             logger.error(f"handle_message: Failed to forward message (plain text retry) to {MY_TELEGRAM_ID}: {e2}")


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
        await application.bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.url == webhook_full_url:
            logger.info("Webhook successfully set!")
        else:
            logger.warning(f"Webhook URL reported by Telegram ({webhook_info.url}) differs from the URL we tried to set ({webhook_full_url}).")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}", exc_info=True)
        # Возвращаем None или райзим исключение, чтобы asyncio.run понял, что запуск не удался?
        # Пока оставляем как есть, ошибка просто логируется.
        # Но возможно, если set_webhook падает, run_webhook возвращает None?
        # Это может объяснить ошибку "a coroutine was expected".
        # Добавим явный raise, чтобы увидеть, если ошибка вебхука - причина.
        # raise e # Раскомментируй, если хочешь, чтобы ошибка вебхука роняла старт

# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Forwarder Bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- ИЗМЕНЕНО: Добавляем универсальный логгер ПЕРВЫМ ---
    # group=-1 означает, что этот обработчик запустится раньше других
    application.add_handler(TypeHandler(Update, log_all_updates), group=-1)

    # Оставляем старый обработчик, но ожидаем, что он не сработает
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_message))

    logger.info("Application built. Starting webhook listener...")
    try:
        # --- Убедимся, что post_init не вернул None или не вызвал исключение ---
        # (Хотя post_init не должен ничего возвращать)
        # Эта проверка избыточна, но для отладки оставим мысль:
        # если бы post_init мог вернуть что-то, что помешало бы run_webhook...
        logger.info("Running application.run_webhook...")
        webhook_runner = application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_full_url # Передаем URL явно
        )
        # Логируем, что вернул run_webhook перед передачей в asyncio.run
        logger.info(f"application.run_webhook returned: {type(webhook_runner)}")

        # Запускаем основную корутину
        asyncio.run(webhook_runner)

    except ValueError as e:
        # Ловим конкретно ошибку ValueError, чтобы увидеть сообщение
        logger.critical(f"CRITICAL ERROR during asyncio.run: {e}", exc_info=True)
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")