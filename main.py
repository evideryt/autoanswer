import logging
import os
import asyncio
import json
import httpx # Используем httpx для асинхронных запросов
from datetime import datetime, timezone
from collections import deque

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
MY_TELEGRAM_ID_STR = os.environ.get("MY_TELEGRAM_ID")
# --- Переменные для "неофициального" Qwen API ---
QWEN_TOKEN = os.environ.get("QWEN_API_KEY") # Используем QWEN_API_KEY для токена
# Используем URL из инструкции
QWEN_CHAT_API_ENDPOINT = "https://chat.qwenlm.ai/api/chat/completions"

HISTORY_SIZE = 5
DEBOUNCE_DELAY = 15
MY_NAME_IN_HISTORY = "киткат" # Используем твой ник

# --- Проверки переменных ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID_STR: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
if not QWEN_TOKEN: logger.critical("CRITICAL: Missing QWEN_API_KEY (used for chat token)"); exit() # Проверка токена
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID loaded: {MY_TELEGRAM_ID}")
logger.info(f"QWEN_TOKEN loaded: YES")
logger.info(f"Using Qwen Chat API Endpoint: {QWEN_CHAT_API_ENDPOINT}")
logger.info(f"History size: {HISTORY_SIZE}, Debounce delay: {DEBOUNCE_DELAY}s")

# --- Хранилища в памяти ---
chat_histories = {}
debounce_timers = {}

# --- Новая Функция для взаимодействия с Qwen Chat API ---
async def get_qwen_chat_response(history: deque) -> str | None:
    """Отправляет историю в Qwen Chat API и возвращает ответ."""
    logger.info(f"Requesting Qwen Chat API response for history (size {len(history)})")

    # --- Форматируем историю для Qwen Chat API ---
    messages_for_qwen = []
    for sender_name, _, text in history:
        role = "assistant" if sender_name == MY_NAME_IN_HISTORY else "user"
        # Добавляем поля из инструкции
        messages_for_qwen.append({
            "role": role,
            "content": text,
            "extra": {}, # Поле из инструкции
            "chat_type": "t2t" # Поле из инструкции
        })

    # --- Тело запроса к Qwen Chat API ---
    payload = {
        "chat_type": "t2t", # Поле из инструкции
        "messages": messages_for_qwen,
        "model": "qwen-max-latest", # Модель из инструкции
        "stream": False # Поле из инструкции
    }

    # --- Заголовки из инструкции ---
    headers = {
        "Authorization": f"Bearer {QWEN_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/237.84.2.178 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client: # Увеличим таймаут еще больше
            logger.debug(f"Sending POST to {QWEN_CHAT_API_ENDPOINT}")
            logger.debug(f"Headers: {headers}") # Логируем заголовки (кроме токена)
            # Не логируем payload целиком из-за потенциального размера истории
            logger.debug(f"Payload messages count: {len(messages_for_qwen)}")

            response = await client.post(QWEN_CHAT_API_ENDPOINT, json=payload, headers=headers)

            logger.debug(f"Qwen Chat API Status Code: {response.status_code}")
            # Логируем ответ только если это ошибка, чтобы не засорять логи успехом
            if response.status_code != 200:
                 logger.debug(f"Qwen Chat API Raw Response (Error): {response.text}")

            response.raise_for_status() # Проверка на HTTP ошибки (4xx, 5xx)

            result = response.json()
            logger.debug(f"Qwen Chat API Raw Response (Success): {json.dumps(result, indent=2)}")

            # --- Извлекаем ответ (согласно последнему сниппету) ---
            generated_text = result.get("choices", [{}])[0].get("message", {}).get("content")

            if generated_text:
                logger.info(f"Received Qwen Chat API response: '{generated_text[:50]}...'")
                return generated_text.strip()
            else:
                logger.error(f"Qwen Chat API response does not contain expected text field 'choices[0].message.content'. Response: {result}")
                return None

    except httpx.HTTPStatusError as e:
        logger.error(f"Qwen Chat API request failed with status {e.response.status_code}: {e.response.text}")
        return None
    except httpx.RequestError as e:
        logger.error(f"Qwen Chat API request failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Error processing Qwen Chat API response: {e}", exc_info=True)
        return None


# --- Отложенная задача: Запрос к Qwen и ответ в чат ---
async def trigger_qwen_response(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Получает ответ от Qwen Chat API и отправляет его в оригинальный чат."""
    logger.info(f"Debounce timer expired for chat {chat_id}. Triggering Qwen response.")
    if chat_id not in chat_histories:
        logger.warning(f"History not found for chat {chat_id} when Qwen trigger expired.")
        return

    history = chat_histories[chat_id]
    if not history:
        logger.info(f"History for chat {chat_id} is empty. Nothing to send to Qwen.")
        return

    # Получаем ответ от Qwen Chat API
    qwen_response = await get_qwen_chat_response(history) # <--- Вызываем новую функцию

    if qwen_response:
        try:
            # Отправляем ответ в ОРИГИНАЛЬНЫЙ чат
            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text=qwen_response,
            )
            logger.info(f"Successfully sent Qwen response to chat {chat_id}.")

            # Добавляем ответ бота в историю
            response_timestamp = datetime.now(timezone.utc)
            if chat_id in chat_histories:
                chat_histories[chat_id].append((MY_NAME_IN_HISTORY, response_timestamp, qwen_response))
                logger.debug(f"Added bot's response to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")

        except TelegramError as e:
            logger.error(f"Failed to send Qwen response to chat {chat_id}: {e}")
    else:
        logger.error(f"Did not receive a valid response from Qwen for chat {chat_id}.")

    if chat_id in debounce_timers:
        del debounce_timers[chat_id]


# --- Обработчик бизнес-сообщений ---
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Логика этой функции остается без изменений, как в предыдущем ответе)
    message = update.business_message
    if not message: return
    chat_id = message.chat.id
    sender = message.from_user
    timestamp_dt = message.date
    text = message.text or "[нетекстовое сообщение]"
    if not sender:
        logger.warning(f"Received business_message without sender info in chat {chat_id}.")
        return
    sender_id = sender.id
    sender_name = MY_NAME_IN_HISTORY if sender_id == MY_TELEGRAM_ID else (sender.first_name or f"ID:{sender_id}")
    logger.info(f"Received business message from {sender_name}({sender_id}) in chat {chat_id}")
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=HISTORY_SIZE)
    chat_histories[chat_id].append((sender_name, timestamp_dt, text))
    logger.debug(f"Added message to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")
    if sender_id != MY_TELEGRAM_ID:
        if chat_id in debounce_timers:
            debounce_timers[chat_id].cancel()
            logger.debug(f"Cancelled previous debounce timer for chat {chat_id}")
        logger.debug(f"Starting new {DEBOUNCE_DELAY}s debounce timer for chat {chat_id}")
        new_timer = asyncio.create_task(
            asyncio.sleep(DEBOUNCE_DELAY, result=chat_id)
        )
        new_timer.add_done_callback(
            lambda task: asyncio.create_task(trigger_qwen_response(task.result(), context)) if not task.cancelled() else None
        )
        debounce_timers[chat_id] = new_timer
    else:
        logger.debug(f"Message from owner ({MY_NAME_IN_HISTORY}). History updated, debounce timer not started/reset.")

# --- Обработчик для логирования (без изменений) ---
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
    logger.info("Initializing Qwen Chat API Autoresponder Bot...") # Новое имя

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрация обработчиков ---
    application.add_handler(TypeHandler(Update, log_all_updates), group=-1)
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))

    logger.info("Application built. Starting webhook listener...")
    try:
        # Эту переменную определяем здесь, т.к. она нужна для run_webhook
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