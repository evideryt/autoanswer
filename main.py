import logging
import os
import asyncio
# Убрали psycopg, random и т.д.

from telegram import Update
# Убрали ChatMember, User - пока не нужны для простой пересылки
from telegram.ext import (
    Application,
    MessageHandler, # Нужен только обработчик сообщений
    filters,
    ContextTypes,
)
from telegram.constants import ChatType # Понадобится для определения типа чата
from telegram.error import TelegramError # Для отлова ошибок отправки

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
# !!! ВАЖНО: Укажи СВОЙ Telegram ID в переменных окружения !!!
# Это ID чата, КУДА бот будет пересылать сообщения.
# Обычно это твой личный ID (для пересылки в "Избранное") или ID личного чата с ботом.
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID:
    logger.critical("CRITICAL: Missing MY_TELEGRAM_ID environment variable. Set it to your Telegram User ID.")
    exit()
try:
    # Пытаемся преобразовать ID в число, чтобы отловить некорректные значения
    MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError:
    logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID}') is not a valid integer.")
    exit()


logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")

# --- Основной обработчик сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ВСЕ входящие сообщения и пересылает их пользователю."""
    # Логируем все обновление, чтобы видеть структуру бизнес-сообщений
    logger.info(f"Received update: {update.to_json()}") # Логируем как JSON для читаемости

    message = update.message

    # Проверяем, есть ли вообще сообщение в обновлении
    if not message:
        logger.debug("Update does not contain a message.")
        return

    # Получаем информацию о чате и отправителе из ОРИГИНАЛЬНОГО сообщения
    original_chat = message.chat
    sender = message.from_user # Это может быть реальный отправитель или твой аккаунт

    # --- Важная проверка: НЕ пересылать сообщения ИЗ чата, КУДА мы пересылаем ---
    # Это предотвращает бесконечный цикл, если MY_TELEGRAM_ID - это ID чата с ботом,
    # и ты сам пишешь боту в этот чат.
    if original_chat.id == MY_TELEGRAM_ID:
        logger.debug(f"Ignoring message from the forward target chat ({MY_TELEGRAM_ID}).")
        return

    # --- Вторая важная проверка: НЕ пересылать сообщения, отправленные самим ботом ---
    # (на всякий случай, хотя они не должны приходить через Business API таким образом)
    if sender and sender.id == context.bot.id:
        logger.debug("Ignoring message from the bot itself.")
        return

    # Формируем информацию об отправителе
    sender_info = "Unknown Sender"
    if sender:
        sender_info = f"{sender.first_name}"
        if sender.last_name:
            sender_info += f" {sender.last_name}"
        if sender.username:
            sender_info += f" (@{sender.username})"
        sender_info += f" (ID: {sender.id})"

    # Формируем информацию о чате
    chat_info = f"Chat ID: {original_chat.id}"
    if original_chat.title:
        chat_info = f"'{original_chat.title}' ({original_chat.id})"
    elif original_chat.type == ChatType.PRIVATE:
        # Для личных чатов title нет, можно использовать имя собеседника, если оно не совпадает с отправителем
        # Но для простоты пока оставим только ID
         chat_info = f"Private Chat ({original_chat.id})"


    # Получаем текст сообщения (или заглушку для других типов)
    message_text = message.text
    if not message_text:
        # Если это не текст (фото, видео, стикер и т.д.)
        message_text = f"[Non-text message type: {message.effective_attachment.mime_type if message.effective_attachment else 'Unknown'}]"
        # Можно добавить обработку подписей к медиа:
        if message.caption:
             message_text += f"\nCaption: {message.caption}"

    # Собираем финальное сообщение для пересылки
    forward_text = (
        f"📩 *New Message*\n\n"
        f"*From:* {sender_info}\n"
        f"*In:* {chat_info}\n"
        f"───────\n"
        f"{message_text}"
    )

    # Отправляем сообщение в указанный MY_TELEGRAM_ID
    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=forward_text,
            parse_mode='MarkdownV2' # Используем Markdown для форматирования *From*, *In*
            # Важно: Если в sender_info или message_text могут быть Markdown символы,
            # их нужно экранировать перед отправкой! Но для теста пока оставим так.
        )
        logger.info(f"Forwarded message from chat {original_chat.id} to {MY_TELEGRAM_ID}")
    except TelegramError as e:
        logger.error(f"Failed to forward message to {MY_TELEGRAM_ID}: {e}")
        # Можно попробовать отправить без форматирования в случае ошибки парсинга Markdown
        if "can't parse entities" in str(e).lower():
            logger.warning("Retrying forward without Markdown...")
            try:
                 # Убираем Markdown символы из заголовков
                 forward_text_plain = (
                    f"📩 New Message\n\n"
                    f"From: {sender_info}\n" # Без *
                    f"In: {chat_info}\n"     # Без *
                    f"───────\n"
                    f"{message_text}" # Сам текст оставляем как есть
                 )
                 await context.bot.send_message(
                    chat_id=MY_TELEGRAM_ID,
                    text=forward_text_plain,
                    parse_mode=None
                 )
                 logger.info(f"Forwarded message (plain text) from chat {original_chat.id} to {MY_TELEGRAM_ID}")
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
            # Важно указать, какие типы обновлений мы хотим получать
            # Для Business API могут понадобиться 'business_message', 'edited_business_message' и т.д.
            # Но начнем с Update.ALL_TYPES, чтобы точно все поймать
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


# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Forwarder Bot...")

    # Базу данных и рекламу не инициализируем

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрация ОДНОГО обработчика ---
    # Ловим ВСЕ сообщения (текст, фото, стикеры и т.д.), не являющиеся командами
    application.add_handler(MessageHandler(filters.MESSAGE & ~filters.COMMAND, handle_message))
    # Можно добавить обработчик и для других типов, если понадобится, например, edited_message

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