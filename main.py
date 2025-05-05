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
    MessageHandler,
    filters,
    ContextTypes,
    # TypeHandler убрали пока, попробуем снова с MessageHandler
)
from telegram.constants import ChatType
from telegram.error import TelegramError

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
# Уменьшаем спам от библиотеки googleai
logging.getLogger("google.ai.generativelanguage").setLevel(logging.WARNING)
logging.getLogger("google.api_core").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Переменные окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") # <--- Новый ключ

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit() # <--- Проверка ключа Gemini
try:
    MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError:
    logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID}') is not valid int."); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (forward target) loaded: {MY_TELEGRAM_ID}")
logger.info(f"GEMINI_API_KEY loaded: YES")

# --- Настройка Gemini ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    # Выбираем модель (например, gemini-1.5-flash - быстрая и недорогая)
    # Или gemini-pro для более качественных ответов
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info(f"Gemini model '{gemini_model.model_name}' configured successfully.")
except Exception as e:
    logger.critical(f"CRITICAL: Failed to configure Gemini: {e}", exc_info=True); exit()

# --- Хранилище истории ---
MAX_HISTORY_PER_CHAT = 50 # Сколько пар вопрос-ответ хранить
# Словарь: {chat_id: deque([{'role': 'user'/'model', 'parts': [text]}...], maxlen=MAX_HISTORY_PER_CHAT * 2)}
# Умножаем на 2, так как храним и user, и model сообщения
chat_histories = {}
MY_NAME_FOR_HISTORY = "киткат" # Используется в системном промпте, не в ролях Gemini

# --- Системный промпт ---
# Gemini лучше воспринимает инструкции в начале истории или через system_instruction
# (если модель поддерживает)
SYSTEM_PROMPT = f"""Ты — ИИ-ассистент, отвечающий на сообщения в Telegram вместо пользователя по имени '{MY_NAME_FOR_HISTORY}'.
Тебе будет предоставлена история переписки (роль 'user' - собеседник, роль 'model' - предыдущие ответы {MY_NAME_FOR_HISTORY}).
Твоя задача — сгенерировать следующий ответ от имени '{MY_NAME_FOR_HISTORY}', сохраняя его предполагаемый стиль и манеру общения, продолжая диалог по существу.
Отвечай только текстом самого сообщения, без префиксов типа '{MY_NAME_FOR_HISTORY}:' или 'Ответ:'.
Будь вежлив и естественен. Если сообщение не требует развернутого ответа, ответь кратко.
"""

# --- Основной обработчик сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие сообщения, генерирует ответ через Gemini и пересылает его."""
    # Логируем обновление, чтобы видеть его структуру
    # Используем to_dict() для более надежного логирования
    try:
        logger.info(f"Received update: {json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")
    except Exception as log_e:
        logger.error(f"Error logging update object: {log_e}")
        logger.info(f"Received update (raw): {update}") # Логируем как есть, если to_dict упал

    # --- Определяем, какое сообщение обрабатывать ---
    # ВАЖНО: Нужно понять, как приходит бизнес-сообщение.
    # Пока предполагаем, что оно все еще в update.message
    # Если это не так, логи выше должны показать, где оно лежит (например, update.business_message)
    message = update.message
    if not message:
        logger.debug("Update does not contain a recognized message object (message or business_message).")
        return

    # --- Извлекаем данные ---
    original_chat = message.chat
    sender = message.from_user # Отправитель (может быть не тот, кто в чате)
    message_text = message.text

    # Проверяем, что есть текст
    if not message_text:
        logger.debug(f"Ignoring non-text message in chat {original_chat.id}")
        return

    # --- Фильтры ---
    if original_chat.id == MY_TELEGRAM_ID:
        logger.debug(f"Ignoring message from the forward target chat ({MY_TELEGRAM_ID}).")
        return
    if sender and sender.id == context.bot.id:
        logger.debug("Ignoring message from the bot itself.")
        return
    # ВАЖНО: Добавить проверку, чтобы не отвечать на свои же сообщения,
    # если Telegram Business присылает и их (хотя не должен)
    # if sender and sender.id == YOUR_OWN_TELEGRAM_ID: ...

    # --- Работа с историей ---
    chat_id = original_chat.id
    if chat_id not in chat_histories:
        # Создаем новую историю для этого чата с нужной максимальной длиной
        chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_PER_CHAT * 2)

    # Добавляем текущее сообщение в историю от 'user' (собеседник)
    chat_histories[chat_id].append({'role': 'user', 'parts': [message_text]})
    logger.debug(f"Added user message to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")

    # --- Формирование запроса к Gemini ---
    # Преобразуем deque в список для Gemini
    current_history = list(chat_histories[chat_id])

    # Пытаемся сгенерировать ответ
    try:
        logger.info(f"Sending request to Gemini for chat {chat_id}...")
        # Передаем системный промпт и историю
        # Некоторые модели поддерживают system_instruction отдельно:
        # response = gemini_model.generate_content(current_history, system_instruction=SYSTEM_PROMPT)
        # Если нет, добавляем его как первое сообщение (но это менее эффективно):
        full_prompt_history = [{'role': 'user', 'parts': [SYSTEM_PROMPT]}] + current_history
        # Если модель поддерживает system_instruction, используй его:
        response = await gemini_model.generate_content_async(
             full_prompt_history,
             # safety_settings={ # Можно настроить безопасность
             #    'HATE': 'BLOCK_NONE',
             #    'HARASSMENT': 'BLOCK_NONE',
             #    'SEXUAL': 'BLOCK_NONE',
             #    'DANGEROUS': 'BLOCK_NONE'
             #}
         )
        # response = await gemini_model.generate_content_async(current_history, system_instruction=SYSTEM_PROMPT)

        # Получаем текст ответа
        gemini_response_text = response.text.strip()
        logger.info(f"Received response from Gemini for chat {chat_id}: '{gemini_response_text[:100]}...'")

        # --- Обновляем историю ответом модели ---
        if gemini_response_text: # Только если ответ не пустой
            chat_histories[chat_id].append({'role': 'model', 'parts': [gemini_response_text]})
            logger.debug(f"Added model response to history for chat {chat_id}. History size: {len(chat_histories[chat_id])}")
        else:
            logger.warning(f"Gemini returned an empty response for chat {chat_id}.")
            # Можно отправить сообщение об ошибке или ничего не делать

    except Exception as e:
        logger.error(f"Error calling Gemini API for chat {chat_id}: {e}", exc_info=True)
        gemini_response_text = "[Ошибка при генерации ответа ИИ]" # Ответ по умолчанию

    # --- Отправка результата ТЕБЕ в личку ---
    # Формируем информацию об отправителе и чате (можно упростить)
    sender_name = sender.first_name if sender else "Unknown"
    chat_title = original_chat.title or f"Private ({original_chat.id})"

    forward_text = (
        f"🤖 *AI suggestion for {chat_title}* "
        f"(from: {sender_name}, chat_id: {original_chat.id}):\n\n"
        f"{gemini_response_text}"
    )

    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=forward_text,
            parse_mode='Markdown' # Используем обычный Markdown, он проще
        )
        logger.info(f"Forwarded Gemini suggestion for chat {original_chat.id} to {MY_TELEGRAM_ID}")
    except TelegramError as e:
        logger.error(f"Failed to forward Gemini suggestion to {MY_TELEGRAM_ID}: {e}")


# --- Функция post_init (без изменений) ---
async def post_init(application: Application):
    # ... (код без изменений) ...
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

# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")

    # Историю и рекламу не инициализируем (история создается по ходу)
    # Базу данных не инициализируем

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрируем обработчик ---
    # Снова пробуем MessageHandler, надеясь, что бизнес-сообщения его триггерят
    # Если нет - вернем TypeHandler(Update, ...) и будем разбирать update внутри handle_message
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_message))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_runner = application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}" # Явно передаем URL
        )
        logger.info(f"application.run_webhook returned: {type(webhook_runner)}")
        asyncio.run(webhook_runner) # Запускаем корутину
    except ValueError as e:
        logger.critical(f"CRITICAL ERROR during asyncio.run: {e}", exc_info=True)
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")