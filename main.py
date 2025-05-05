import logging
import os
import asyncio
import json

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,        # Оставляем для UpdateType
    ContextTypes,
    TypeHandler,
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

# --- Обработчик для логирования ВСЕХ обновлений (оставляем для отладки) ---
async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"--- Received Raw Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

# --- ИЗМЕНЕННЫЙ Основной обработчик сообщений ---
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает БИЗНЕС-сообщения и пересылает их пользователю."""
    logger.info(">>> handle_business_message triggered <<<") # Поменяли имя функции в логе

    # --- ИЗМЕНЕНИЕ: Получаем сообщение из business_message ---
    message = update.business_message

    # Проверка на всякий случай, хотя фильтр должен это обеспечить
    if not message:
        logger.debug("handle_business_message: Update does not contain a business_message.")
        return

    # --- ИЗМЕНЕНИЕ: Остальная логика теперь работает с 'message', который равен update.business_message ---
    original_chat = message.chat
    # В бизнес-сообщении 'from' - это реальный отправитель
    sender = message.from_user

    # Проверка на пересылку из целевого чата (остается)
    if original_chat.id == MY_TELEGRAM_ID:
        logger.debug(f"handle_business_message: Ignoring message from target chat {MY_TELEGRAM_ID}.")
        return

    # Проверка на сообщения от бота (маловероятно для business_message, но пусть будет)
    if sender and sender.id == context.bot.id:
        logger.debug("handle_business_message: Ignoring message from bot itself.")
        return

    # Формирование информации (без изменений)
    sender_info = "Unknown Sender"
    if sender:
        sender_info = f"{sender.first_name}"
        if sender.last_name: sender_info += f" {sender.last_name}"
        if sender.username: sender_info += f" (@{sender.username})"
        sender_info += f" (ID: {sender.id})"

    chat_info = f"Chat ID: {original_chat.id}"
    if original_chat.title: # Маловероятно для личных чатов, но вдруг бот подключен к бизнес-группе
        chat_info = f"'{original_chat.title}' ({original_chat.id})"
    elif original_chat.type == ChatType.PRIVATE:
         # Используем имя собеседника из chat, т.к. это личный чат
         chat_user_name = original_chat.first_name
         if original_chat.last_name: chat_user_name += f" {original_chat.last_name}"
         if original_chat.username: chat_user_name += f" (@{original_chat.username})"
         chat_info = f"Private Chat with {chat_user_name} ({original_chat.id})"

    message_text = message.text
    if not message_text:
        # У бизнес-сообщений может не быть effective_attachment, проверяем стандартные поля
        if message.photo: message_text = "[Photo]"
        elif message.video: message_text = "[Video]"
        elif message.audio: message_text = "[Audio]"
        elif message.voice: message_text = "[Voice Message]"
        elif message.document: message_text = "[Document]"
        elif message.sticker: message_text = f"[Sticker: {message.sticker.emoji}]"
        else: message_text = "[Non-text/Unknown Attachment]"
        # Добавляем подпись, если она есть
        if message.caption: message_text += f"\nCaption: {message.caption}"

    # Функция экранирования (без изменений)
    def escape_markdown_v2(text: str) -> str:
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        escaped_text = ""
        for char in str(text): # Убедимся, что работаем со строкой
            if char in escape_chars: escaped_text += f'\\{char}'
            else: escaped_text += char
        return escaped_text

    # Экранируем части текста перед вставкой в Markdown
    safe_sender_info = escape_markdown_v2(sender_info)
    safe_chat_info = escape_markdown_v2(chat_info)
    safe_message_text = escape_markdown_v2(message_text)

    forward_text = (
        f"📩 *Business Message*\n\n" # Поменяли заголовок для ясности
        f"*From:* {safe_sender_info}\n"
        f"*In:* {safe_chat_info}\n"
        f"───────\n"
        f"{safe_message_text}"
    )

    # Отправка сообщения (без изменений)
    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID, text=forward_text, parse_mode='MarkdownV2'
        )
        logger.info(f"handle_business_message: Forwarded message from chat {original_chat.id} to {MY_TELEGRAM_ID}")
    except TelegramError as e:
        logger.error(f"handle_business_message: Failed to forward message (MarkdownV2) to {MY_TELEGRAM_ID}: {e}")
        try:
             forward_text_plain = (
                f"📩 Business Message\n\n"
                f"From: {sender_info}\n"
                f"In: {chat_info}\n"
                f"───────\n"
                f"{message_text}"
             )
             await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID, text=forward_text_plain, parse_mode=None
             )
             logger.info(f"handle_business_message: Forwarded message (plain text retry) to {MY_TELEGRAM_ID}")
        except Exception as e2:
             logger.error(f"handle_business_message: Failed to forward message (plain text retry) to {MY_TELEGRAM_ID}: {e2}")


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
            url=webhook_full_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.url == webhook_full_url: logger.info("Webhook successfully set!")
        else: logger.warning(f"Webhook URL mismatch: {webhook_info.url} != {webhook_full_url}")
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

    # --- Регистрация обработчиков ---
    # Логгер ВСЕХ обновлений (оставляем первым)
    application.add_handler(TypeHandler(Update, log_all_updates), group=-1)

    # --- ИЗМЕНЕНИЕ: Ловим BUSINESS_MESSAGE, а не MESSAGE ---
    # Убираем фильтр ~filters.COMMAND, т.к. в бизнес-сообщениях команд скорее всего нет
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))

    # Можно добавить обработчик и для отредактированных бизнес-сообщений, если нужно
    # application.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, handle_edited_business_message))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        logger.info("Running application.run_webhook...")
        webhook_runner = application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_full_url
        )
        logger.info(f"application.run_webhook returned: {type(webhook_runner)}")
        asyncio.run(webhook_runner)

    except ValueError as e:
        logger.critical(f"CRITICAL ERROR during asyncio.run: {e}", exc_info=True)
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")