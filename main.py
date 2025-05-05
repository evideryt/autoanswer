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
QWEN_TOKEN = os.environ.get("QWEN_API_KEY")
QWEN_CHAT_API_ENDPOINT = "https://chat.qwenlm.ai/api/chat/completions"
HISTORY_SIZE = 5
DEBOUNCE_DELAY = 15
MY_NAME_IN_HISTORY = "киткат"

if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID_STR: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
if not QWEN_TOKEN: logger.critical("CRITICAL: Missing QWEN_API_KEY (used for chat token)"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID loaded: {MY_TELEGRAM_ID}")
logger.info(f"QWEN_TOKEN loaded: YES")
logger.info(f"Using Qwen Chat API Endpoint: {QWEN_CHAT_API_ENDPOINT}")
logger.info(f"History size: {HISTORY_SIZE}, Debounce delay: {DEBOUNCE_DELAY}s")

chat_histories = {}
debounce_timers = {}

# --- ИЗМЕНЕННАЯ Функция для взаимодействия с Qwen Chat API ---
async def get_qwen_chat_response(history: deque) -> str | None:
    """Отправляет историю в Qwen Chat API и возвращает ответ."""
    logger.info(f"Requesting Qwen Chat API response for history (size {len(history)})")

    messages_for_qwen = []
    for sender_name, _, text in history:
        role = "assistant" if sender_name == MY_NAME_IN_HISTORY else "user"
        messages_for_qwen.append({
            "role": role, "content": text, "extra": {}, "chat_type": "t2t"
        })

    payload = {
        "chat_type": "t2t", "messages": messages_for_qwen,
        "model": "qwen-max-latest", "stream": False
    }
    headers = {
        "Authorization": f"Bearer {QWEN_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/237.84.2.178 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            logger.debug(f"Sending POST to {QWEN_CHAT_API_ENDPOINT}")
            # Логируем заголовки без токена для безопасности
            logger.debug(f"Headers: Authorization=Bearer ****, Content-Type={headers['Content-Type']}, User-Agent={headers['User-Agent']}")
            logger.debug(f"Payload messages count: {len(messages_for_qwen)}")

            response = await client.post(QWEN_CHAT_API_ENDPOINT, json=payload, headers=headers)

            logger.info(f"Qwen Chat API Status Code: {response.status_code}")
            # Логируем СЫРОЙ ТЕКСТ ответа ПЕРЕД парсингом
            raw_response_text = response.text
            logger.info(f"Qwen Chat API Raw Response Text (first 500 chars): {raw_response_text[:500]}")

            # Проверяем статус ПОСЛЕ логирования текста
            response.raise_for_status() # Если статус 4xx или 5xx, вылетит исключение

            # Оборачиваем парсинг JSON в try-except
            try:
                result = response.json()
                logger.debug(f"Qwen Chat API Parsed JSON: {json.dumps(result, indent=2)}")

                # Извлекаем ответ
                generated_text = result.get("choices", [{}])[0].get("message", {}).get("content")

                if generated_text:
                    logger.info(f"Received Qwen Chat API response: '{generated_text[:50]}...'")
                    return generated_text.strip()
                else:
                    logger.error(f"Qwen Chat API JSON response missing 'choices[0].message.content'. Response: {result}")
                    return None
            except json.JSONDecodeError as json_err:
                logger.error(f"Failed to decode Qwen Chat API response as JSON: {json_err}")
                # Логируем текст, который вызвал ошибку JSON
                logger.error(f"Raw response text that caused JSON error: {raw_response_text}")
                return None

    except httpx.HTTPStatusError as e:
        # Этот лог уже содержит текст ошибки из response.text
        logger.error(f"Qwen Chat API request failed with status {e.response.status_code}: {e.response.text}")
        return None
    except httpx.RequestError as e:
        logger.error(f"Qwen Chat API request failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error processing Qwen Chat API request/response: {type(e).__name__}: {e}", exc_info=True)
        return None

# --- Отложенная задача: Запрос к Qwen и ответ в чат ---
async def trigger_qwen_response(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Debounce timer expired for chat {chat_id}. Triggering Qwen response.")
    if chat_id not in chat_histories:
        logger.warning(f"History not found for chat {chat_id} when Qwen trigger expired.")
        return

    history = chat_histories[chat_id]
    if not history:
        logger.info(f"History for chat {chat_id} is empty. Nothing to send to Qwen.")
        return

    qwen_response = await get_qwen_chat_response(history) # Вызываем обновленную функцию

    if qwen_response:
        try:
            sent_message = await context.bot.send_message(
                chat_id=chat_id, text=qwen_response,
            )
            logger.info(f"Successfully sent Qwen response to chat {chat_id}.")
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

# --- Обработчик бизнес-сообщений (без изменений) ---
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# --- Основная точка входа (без изменений) ---
if __name__ == "__main__":
    logger.info("Initializing Qwen Chat API Autoresponder Bot...")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    application.add_handler(TypeHandler(Update, log_all_updates), group=-1)
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))
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