import logging
import os
import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler, # Нужен только обработчик сообщений
    filters,        # Импортируем filters, чтобы использовать UpdateType
    ContextTypes,
)
from telegram.constants import ChatType
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
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID:
    logger.critical("CRITICAL: Missing MY_TELEGRAM_ID environment variable. Set it to your Telegram User ID.")
    exit()
try:
    MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError:
    logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID}') is not a valid integer.")
    exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}") # Render сам подставит значение $PORT
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")

# --- Основной обработчик сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received update: {update.to_json()}") # Логируем все обновление

    message = update.message
    if not message:
        logger.debug("Update does not contain a message.")
        return

    original_chat = message.chat
    sender = message.from_user

    if original_chat.id == MY_TELEGRAM_ID:
        logger.debug(f"Ignoring message from the forward target chat ({MY_TELEGRAM_ID}).")
        return
    if sender and sender.id == context.bot.id:
        logger.debug("Ignoring message from the bot itself.")
        return

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

    # Экранируем символы Markdown V2 для безопасности
    def escape_markdown_v2(text: str) -> str:
        # Символы для экранирования в MarkdownV2
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        # Создаем регулярное выражение для поиска этих символов
        # Нужно экранировать сам символ '-' в диапазоне или списке
        # escape_pattern = re.compile(f'([{re.escape(escape_chars)}])') # Старый вариант с re
        # Простой вариант без re
        escaped_text = ""
        for char in str(text): # Убедимся, что работаем со строкой
            if char in escape_chars:
                escaped_text += f'\\{char}'
            else:
                escaped_text += char
        return escaped_text

    # Экранируем части текста перед вставкой в Markdown
    safe_sender_info = escape_markdown_v2(sender_info)
    safe_chat_info = escape_markdown_v2(chat_info)
    safe_message_text = escape_markdown_v2(message_text) # Экранируем и сам текст сообщения

    forward_text = (
        f"📩 *New Message*\n\n"
        f"*From:* {safe_sender_info}\n"
        f"*In:* {safe_chat_info}\n"
        f"───────\n"
        f"{safe_message_text}" # Используем экранированный текст
    )

    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=forward_text,
            parse_mode='MarkdownV2'
        )
        logger.info(f"Forwarded message from chat {original_chat.id} to {MY_TELEGRAM_ID}")
    except TelegramError as e:
        logger.error(f"Failed to forward message to {MY_TELEGRAM_ID} (MarkdownV2): {e}")
        # Попробуем отправить как обычный текст, если Markdown не сработал
        try:
            # Собираем текст без Markdown символов
             forward_text_plain = (
                f"📩 New Message\n\n"
                f"From: {sender_info}\n" # Обычный текст
                f"In: {chat_info}\n"     # Обычный текст
                f"───────\n"
                f"{message_text}"      # Обычный текст
             )
             await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=forward_text_plain,
                parse_mode=None
             )
             logger.info(f"Forwarded message (plain text retry) from chat {original_chat.id} to {MY_TELEGRAM_ID}")
        except Exception as e2:
             logger.error(f"Failed to forward message (plain text retry) to {MY_TELEGRAM_ID}: {e2}")


# --- Функция установки вебхука (без изменений) ---
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
            allowed_updates=Update.ALL_TYPES, # Оставляем ALL_TYPES для теста
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


# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Forwarder Bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрация ОДНОГО обработчика с ИСПРАВЛЕННЫМ фильтром ---
    # Ловим ВСЕ сообщения (текст, фото, стикеры и т.д.), не являющиеся командами
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_message))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        asyncio.run(application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_full_url
        ))
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")