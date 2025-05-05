import logging
import os
import asyncio
import json
from collections import deque

# --- Библиотека Gemini ---
import google.generativeai as genai

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler, # Возвращаем MessageHandler
    filters,        # Возвращаем filters
    ContextTypes,
    # TypeHandler убрали
)
from telegram.constants import ChatType
from telegram.error import TelegramError

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google.ai.generativelanguage").setLevel(logging.WARNING)
logging.getLogger("google.api_core").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Переменные окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int."); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")
logger.info(f"GEMINI_API_KEY loaded: YES")

# --- Настройка Gemini ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info(f"Gemini model '{gemini_model.model_name}' configured successfully.")
except Exception as e:
    logger.critical(f"CRITICAL: Failed to configure Gemini: {e}", exc_info=True); exit()

# --- Хранилище истории ---
MAX_HISTORY_PER_CHAT = 50
chat_histories = {}
MY_NAME_FOR_HISTORY = "киткат" # Имя для системного промпта

# --- Системный промпт ---
SYSTEM_PROMPT = f"""Ты — ИИ-ассистент, отвечающий на сообщения в Telegram вместо пользователя по имени '{MY_NAME_FOR_HISTORY}'.
Тебе будет предоставлена история переписки (роль 'user' - собеседник, роль 'model' - предыдущие ответы {MY_NAME_FOR_HISTORY}).
Твоя задача — сгенерировать следующий ответ от имени '{MY_NAME_FOR_HISTORY}', сохраняя его предполагаемый стиль и манеру общения, продолжая диалог по существу.
Отвечай только текстом самого сообщения, без префиксов типа '{MY_NAME_FOR_HISTORY}:' или 'Ответ:'.
Будь вежлив и естественен. Если сообщение не требует развернутого ответа, ответь кратко.
"""

# --- Основной обработчик БИЗНЕС-сообщений ---
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает бизнес-сообщения, генерирует ответ через Gemini и пересылает его."""
    logger.info(">>> handle_business_message triggered <<<") # Лог срабатывания

    # Логируем JSON пришедшего обновления
    try:
        logger.info(f"Received update (JSON): {json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")
    except Exception as log_e:
        logger.error(f"Error logging update object: {log_e}")
        logger.info(f"Received update (raw): {update}")

    # --- Извлекаем данные из business_message ---
    business_message = update.business_message
    if not business_message:
        logger.warning("Update is not a business message or business_message is None.")
        return

    original_chat = business_message.chat
    sender = business_message.from_user # Отправитель сообщения
    message_text = business_message.text

    # Проверяем, что есть текст
    if not message_text:
        logger.debug(f"Ignoring non-text business message in chat {original_chat.id}")
        return

    # --- Фильтры ---
    if original_chat.id == MY_TELEGRAM_ID:
        logger.debug(f"Ignoring business message from the forward target chat ({MY_TELEGRAM_ID}).")
        return
    # Не реагируем на сообщения от ботов (на всякий случай)
    if sender and sender.is_bot:
        logger.debug(f"Ignoring business message from bot {sender.id} in chat {original_chat.id}")
        return
    # ВАЖНО: Проверь свой ID, если не хочешь случайно отвечать на свои же сообщения,
    # если они вдруг придут как business_message (хотя не должны).
    # YOUR_OWN_TELEGRAM_ID = 5375313373 # <--- Твой ID
    # if sender and sender.id == YOUR_OWN_TELEGRAM_ID:
    #    logger.debug(f"Ignoring own message received as business message in chat {original_chat.id}")
    #    return


    # --- Работа с историей ---
    chat_id = original_chat.id
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_PER_CHAT * 2)

    # Добавляем текущее сообщение в историю от 'user'
    chat_histories[chat_id].append({'role': 'user', 'parts': [message_text]})
    logger.debug(f"Added user message to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")

    # --- Формирование запроса к Gemini ---
    current_history = list(chat_histories[chat_id])
    full_prompt_history = [{'role': 'user', 'parts': [SYSTEM_PROMPT]}] + current_history

    try:
        logger.info(f"Sending request to Gemini for chat {chat_id}...")
        response = await gemini_model.generate_content_async(full_prompt_history)
        gemini_response_text = response.text.strip()
        logger.info(f"Received response from Gemini for chat {chat_id}: '{gemini_response_text[:100]}...'")

        # --- Обновляем историю ответом модели ---
        if gemini_response_text:
            chat_histories[chat_id].append({'role': 'model', 'parts': [gemini_response_text]})
            logger.debug(f"Added model response to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")
        else:
            logger.warning(f"Gemini returned an empty response for chat {chat_id}.")

    except Exception as e:
        logger.error(f"Error calling Gemini API for chat {chat_id}: {e}", exc_info=True)
        gemini_response_text = "[Ошибка при генерации ответа ИИ]"

    # --- Отправка результата ТЕБЕ в личку ---
    # Используем имя и ID из `sender` (кто написал сообщение)
    sender_name = "Unknown"
    if sender:
        sender_name = sender.first_name or f"ID:{sender.id}"

    # Используем chat.title или имя собеседника для личных чатов
    chat_title = original_chat.title
    if not chat_title and original_chat.type == ChatType.PRIVATE:
        chat_title = original_chat.first_name or f"Private ({original_chat.id})"

    forward_text = (
        f"🤖 *AI suggestion for {chat_title}* "
        f"(from: {sender_name}, chat_id: {original_chat.id}):\n\n"
        f"{gemini_response_text}"
    )

    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=forward_text,
            parse_mode='Markdown'
        )
        logger.info(f"Forwarded Gemini suggestion for chat {original_chat.id} to {MY_TELEGRAM_ID}")
    except TelegramError as e:
        logger.error(f"Failed to forward Gemini suggestion to {MY_TELEGRAM_ID}: {e}")


# --- Функция post_init (без изменений) ---
async def post_init(application: Application):
    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    logger.info(f"Attempting to set webhook using:")
    logger.info(f"  - Base URL (from env): {WEBHOOK_URL}")
    logger.info(f"  - Bot Token: {'*' * (len(BOT_TOKEN) - 4)}{BOT_TOKEN[-4:]}")
    logger.info(f"  - Final Webhook URL for set_webhook: {webhook_full_url}")
    if not webhook_full_url.startswith("https://"):
        logger.error(f"FATAL: The final webhook URL '{webhook_full_url}' does not start with https://.")
        raise ValueError("Webhook URL must start with https://") # Добавим ошибку
    try:
        allowed_updates = [
            "message", "edited_message",
            "business_connection", "business_message", # Явно указываем бизнес-типы
            "edited_business_message", "deleted_business_messages"
        ]
        logger.info(f"Setting allowed_updates: {allowed_updates}")
        await application.bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=allowed_updates,
            drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.allowed_updates:
             logger.info(f"Effective allowed_updates: {webhook_info.allowed_updates}")
        if webhook_info.url == webhook_full_url:
            logger.info("Webhook successfully set!")
        else:
            logger.warning(f"Webhook URL reported by Telegram ({webhook_info.url}) differs from the URL we tried to set ({webhook_full_url}).")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}", exc_info=True)
        raise e # Перевыбрасываем ошибку

# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрируем обработчик ДЛЯ БИЗНЕС-СООБЩЕНИЙ ---
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))
    logger.info("Registered MessageHandler for BUSINESS_MESSAGE updates.")
    # Можно добавить еще один для filters.UpdateType.EDITED_BUSINESS_MESSAGE, если нужно реагировать на изменения

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
             logger.critical("CRITICAL ERROR: application.run_webhook returned None")
        else:
             asyncio.run(webhook_runner)
    except ValueError as e:
        logger.critical(f"CRITICAL ERROR during asyncio.run: {e}", exc_info=True)
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")