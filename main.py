import logging
import os
import asyncio
import json
from datetime import datetime, timezone # Для работы с датами
from collections import deque # Для хранения истории

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    TypeHandler,
)
from telegram.constants import ChatType
from telegram.error import TelegramError

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID_STR = os.environ.get("MY_TELEGRAM_ID") # Читаем как строку
HISTORY_SIZE = 5 # Храним последние 5 сообщений
DEBOUNCE_DELAY = 15 # Задержка в секундах

# --- Проверки переменных ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID_STR: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
try:
    MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")
logger.info(f"History size: {HISTORY_SIZE}, Debounce delay: {DEBOUNCE_DELAY}s")

# --- Хранилища в памяти ---
# {chat_id: deque([(sender_name, timestamp, text), ...], maxlen=HISTORY_SIZE)}
chat_histories = {}
# {chat_id: asyncio.Task} - для отслеживания и отмены таймеров
debounce_timers = {}

# --- Функция форматирования времени ---
def format_timestamp(ts: int) -> str:
    """Форматирует UNIX timestamp в читаемую строку ДД.ММ.ГГГГ ЧЧ:ММ:СС"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Можно настроить формат и часовой пояс по желанию
    return dt.strftime("%d.%m.%Y %H:%M:%S UTC")

# --- Отложенная задача отправки истории ---
async def send_history_to_owner(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Форматирует и отправляет историю чата владельцу."""
    logger.info(f"Debounce timer expired for chat {chat_id}. Preparing to send history.")
    if chat_id not in chat_histories:
        logger.warning(f"History not found for chat {chat_id} when timer expired.")
        return

    history = chat_histories[chat_id]
    if not history:
        logger.info(f"History for chat {chat_id} is empty. Nothing to send.")
        return

    formatted_history = [f"История чата (ID: {chat_id}):"]
    for sender_name, timestamp, text in history:
        time_str = format_timestamp(timestamp)
        # Экранируем имя и текст на всякий случай
        safe_sender = sender_name.replace("<", "<").replace(">", ">").replace("&", "&")
        safe_text = text.replace("<", "<").replace(">", ">").replace("&", "&")
        formatted_history.append(f"<b>{safe_sender}</b> [{time_str}]:\n{safe_text}")

    final_text = "\n\n".join(formatted_history)

    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=final_text,
            parse_mode='HTML' # Используем HTML для жирного шрифта
        )
        logger.info(f"Successfully sent history for chat {chat_id} to owner {MY_TELEGRAM_ID}.")
    except TelegramError as e:
        logger.error(f"Failed to send history for chat {chat_id} to owner {MY_TELEGRAM_ID}: {e}")
    finally:
        # Удаляем таймер из словаря после выполнения
        if chat_id in debounce_timers:
            del debounce_timers[chat_id]


# --- Обработчик бизнес-сообщений ---
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие и ИСХОДЯЩИЕ бизнес-сообщения."""
    message = update.business_message
    if not message: return # На всякий случай

    chat_id = message.chat.id
    sender = message.from_user
    timestamp = message.date # UNIX timestamp
    text = message.text or "[нетекстовое сообщение]" # Если текста нет

    if not sender:
        logger.warning(f"Received business_message without sender info in chat {chat_id}. Update: {update.to_json()}")
        return

    sender_id = sender.id
    sender_name = sender.first_name or f"ID:{sender_id}" # Используем имя или ID

    logger.info(f"Received business message from {sender_name}({sender_id}) in chat {chat_id}")

    # --- Добавляем сообщение в историю ---
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=HISTORY_SIZE)

    # Сохраняем кортеж (имя, время, текст)
    chat_histories[chat_id].append((sender_name, timestamp, text))
    logger.debug(f"Added message to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")

    # --- Логика задержки (Debounce) ---
    # Запускаем таймер только если сообщение НЕ от владельца бота
    if sender_id != MY_TELEGRAM_ID:
        # Отменяем предыдущий таймер для этого чата, если он есть
        if chat_id in debounce_timers:
            debounce_timers[chat_id].cancel()
            logger.debug(f"Cancelled previous debounce timer for chat {chat_id}")

        # Запускаем новый таймер
        logger.debug(f"Starting new {DEBOUNCE_DELAY}s debounce timer for chat {chat_id}")
        new_timer = asyncio.create_task(
            asyncio.sleep(DEBOUNCE_DELAY, result=chat_id) # Передаем chat_id в результат sleep
        )
        # Добавляем callback, который вызовет send_history_to_owner
        new_timer.add_done_callback(
            lambda task: asyncio.create_task(send_history_to_owner(task.result(), context)) if not task.cancelled() else None
        )
        debounce_timers[chat_id] = new_timer
    else:
        logger.debug(f"Message from owner ({sender_id}). History updated, debounce timer not started/reset.")


# --- Обработчик для логирования ВСЕХ обновлений (оставляем для отладки) ---
async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"--- Received Raw Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

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
    logger.info("Initializing Telegram Business Debounce Bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрация обработчиков ---
    application.add_handler(TypeHandler(Update, log_all_updates), group=-1) # Логгер
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message)) # Обработчик бизнес-сообщений

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