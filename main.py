import logging
import os
import asyncio
import json
from collections import defaultdict, deque # Для хранения истории

# --- НОВОЕ: Импорт для Gemini ---
import google.generativeai as genai

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,        # Используем filters.UpdateType
    ContextTypes,
    TypeHandler,    # Для логирования всех обновлений
)
from telegram.constants import ChatType
from telegram.error import TelegramError

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Уменьшаем спам от библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google.generativeai").setLevel(logging.INFO) # Можно поставить WARNING для меньшего спама от Gemini

logger = logging.getLogger(__name__)

# --- Переменные окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") # <--- Ключ для Gemini

# --- Настройки для Gemini и Истории ---
MAX_HISTORY_PER_CHAT = 50 # Сколько последних сообщений помнить
# Используем defaultdict: если чата нет в словаре, он создаст для него deque
# Храним кортежи: (роль, текст), где роль - 'user' или 'model' (для Gemini)
# Или можно использовать 'Собеседник' и 'киткат' для наглядности
chat_histories = defaultdict(lambda: deque(maxlen=MAX_HISTORY_PER_CHAT))
MY_NAME_FOR_HISTORY = "киткат" # Как называть себя в истории для Gemini
SYSTEM_PROMPT = f"""Ты — ИИ-ассистент, отвечающий на сообщения в Telegram вместо пользователя по имени '{MY_NAME_FOR_HISTORY}'.
Тебе будет предоставлена история переписки (роль 'user' - это собеседник, роль 'model' - это предыдущие ответы '{MY_NAME_FOR_HISTORY}').
Твоя задача — сгенерировать следующий ОДИН ответ от имени '{MY_NAME_FOR_HISTORY}', сохраняя его стиль и манеру общения, продолжая диалог по существу.
Отвечай ТОЛЬКО текстом самого сообщения, без префиксов типа '{MY_NAME_FOR_HISTORY}:' или 'Ответ:'. Будь краток и естественен."""

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit() # <--- Проверяем ключ Gemini
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID (suggestion target) loaded: {MY_TELEGRAM_ID}")
logger.info(f"GEMINI_API_KEY loaded: YES")

# --- Настройка Gemini ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    # Выбираем модель (gemini-1.5-flash - быстрая и дешевая, gemini-pro - стандартная)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info("Gemini API configured successfully.")
except Exception as e:
    logger.critical(f"CRITICAL: Failed to configure Gemini API: {e}", exc_info=True)
    exit()

# --- Функции для работы с Историей ---
def add_to_history(chat_id: int, role: str, text: str):
    """Добавляет сообщение в историю чата."""
    if not text or not text.strip(): # Не добавляем пустые сообщения
        return
    chat_histories[chat_id].append({'role': role, 'parts': [text]}) # Формат для Gemini API
    logger.debug(f"Added to history for chat {chat_id}: role={role}")

def get_formatted_history(chat_id: int) -> list:
    """Возвращает историю чата в формате, нужном для Gemini API."""
    # Возвращаем копию deque как список словарей
    return list(chat_histories[chat_id])

# --- Функция для вызова Gemini ---
async def generate_gemini_response(chat_history: list) -> str | None:
    """Генерирует ответ с помощью Gemini, используя историю и системный промпт."""
    if not chat_history: # Не генерируем ответ, если нет истории (первое сообщение?)
        logger.warning("generate_gemini_response called with empty history.")
        # Можно вернуть стандартный ответ или None
        return None

    logger.debug(f"Generating response with history (last message role: {chat_history[-1]['role']})...")
    try:
        # Создаем сессию чата с системной инструкцией и историей
        convo = gemini_model.start_chat(
            system_instruction=SYSTEM_PROMPT,
            history=chat_history # Передаем историю напрямую
        )
        # Отправляем пустое сообщение, чтобы получить следующий ответ модели
        # (Gemini API ожидает, что последнее сообщение в истории - от user)
        # Если последнее было от model, нам нужно сначала отправить пустое сообщение
        # или просто попросить сгенерировать ответ без нового ввода.
        # Gemini API с start_chat/send_message ожидает чередования user/model.
        # Если последнее было 'model', а нам нужен новый ответ 'model',
        # то просто запросить генерацию может быть сложнее.
        # Попробуем немного другой подход: используем generate_content напрямую

        # Формируем контент для запроса: системная инструкция + история
        # Gemini API v1 (python client) не очень хорошо работает с system_instruction + history в generate_content
        # Лучше склеить их вручную или использовать start_chat, но нужно следить за ролями.

        # --- Вариант 1: Используем generate_content, объединяя промпт ---
        # Склеиваем историю в строку (простой вариант)
        # history_str = "\n".join([f"{msg['role']}: {msg['parts'][0]}" for msg in chat_history])
        # full_prompt = f"{SYSTEM_PROMPT}\n\nИстория:\n{history_str}\n\nОтвет от {MY_NAME_FOR_HISTORY}:"
        # response = await asyncio.to_thread(gemini_model.generate_content, full_prompt)

        # --- Вариант 2: Используем generate_content с message-like структурой ---
        # (Более современный подход для Gemini 1.5)
        messages_for_gemini = [
            # {'role': 'system', 'parts': [SYSTEM_PROMPT]}, # system role не всегда поддерживается в generate_content
            *chat_history # Вставляем список словарей истории
        ]
         # Добавляем системный промпт как первое сообщение 'user', если нужно
        # Или полагаемся на system_instruction в модели (если API позволяет)

        # Генерируем асинхронно (если библиотека поддерживает async, если нет - to_thread)
        # Убедимся, что у нас есть google-generativeai >= 0.5.0 для async
        response = await gemini_model.generate_content_async(messages_for_gemini)


        logger.debug(f"Gemini raw response: {response}")
        generated_text = response.text.strip()
        logger.info(f"Gemini generated response text (trimmed): '{generated_text[:100]}...'")
        return generated_text

    except Exception as e:
        logger.error(f"Error calling Gemini API: {type(e).__name__}: {e}", exc_info=True)
        # В логах будет полный трейсбек
        # Можно проверить specific Gemini errors, e.g., Blocked prompt, Quota exceeded
        if "safety" in str(e).lower():
             logger.warning("Gemini API request potentially blocked due to safety settings.")
        elif "quota" in str(e).lower():
             logger.error("Gemini API quota exceeded.")
        return None # Возвращаем None при любой ошибке


# --- Универсальный логгер обновлений ---
async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"--- Received Raw Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

# --- Основной обработчик БИЗНЕС-сообщений ---
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие бизнес-сообщения, генерирует ответ и предлагает его."""
    logger.info(">>> handle_business_message triggered <<<")

    # В бизнес-обновлении сообщение находится в update.business_message
    message = update.business_message
    if not message:
        logger.warning("handle_business_message: Update does not contain a business_message.")
        return

    original_chat = message.chat
    message_text = message.text
    # Важно: В business_message нет поля from_user!
    # Сообщение всегда адресовано БИЗНЕС-АККАУНТУ.
    # Нам нужно только ID чата, откуда оно пришло.
    sender_name = "Собеседник" # Условное имя для истории

    if not original_chat or not message_text:
        logger.debug("handle_business_message: Missing chat or text in business message.")
        return

    chat_id = original_chat.id

    # Игнорируем пересылку самому себе (на всякий случай)
    if chat_id == MY_TELEGRAM_ID:
        logger.debug(f"handle_business_message: Ignoring message from target chat {MY_TELEGRAM_ID}.")
        return

    # 1. Добавляем входящее сообщение в историю
    add_to_history(chat_id, 'user', message_text) # 'user' - роль собеседника для Gemini

    # 2. Получаем историю для промпта
    history_for_prompt = get_formatted_history(chat_id)

    # 3. Генерируем ответ через Gemini
    gemini_response = await generate_gemini_response(history_for_prompt)

    if gemini_response:
        # 4. Добавляем СГЕНЕРИРОВАННЫЙ ответ в историю
        add_to_history(chat_id, 'model', gemini_response) # 'model' - роль ИИ/бота (нас) для Gemini

        # 5. Отправляем предложенный ответ В ЛИЧКУ ПОЛЬЗОВАТЕЛЮ (MY_TELEGRAM_ID)
        suggestion_header = f"🤖 Ответ для чата {chat_id}"
        if original_chat.title:
             suggestion_header = f"🤖 Ответ для '{original_chat.title}' ({chat_id})"
        elif original_chat.type == ChatType.PRIVATE:
             # Попробуем достать имя собеседника из чата, если оно есть
             chat_peer_name = original_chat.first_name or ""
             if original_chat.last_name: chat_peer_name += f" {original_chat.last_name}"
             if chat_peer_name.strip():
                 suggestion_header = f"🤖 Ответ для {chat_peer_name.strip()} ({chat_id})"

        suggestion_text = f"{suggestion_header}\n───────\n{gemini_response}"

        try:
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=suggestion_text,
                parse_mode=None # Отправляем как простой текст
            )
            logger.info(f"Sent suggestion for chat {chat_id} to {MY_TELEGRAM_ID}")
        except Exception as e:
            logger.error(f"Failed to send suggestion for chat {chat_id} to {MY_TELEGRAM_ID}: {e}")
    else:
        # Если Gemini не смог сгенерировать ответ
        logger.warning(f"Gemini failed to generate response for chat {chat_id}.")
        try:
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=f"⚠️ Не удалось сгенерировать ответ для чата {chat_id}."
            )
        except Exception as e:
             logger.error(f"Failed to send error notification to {MY_TELEGRAM_ID}: {e}")


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
            allowed_updates=Update.ALL_TYPES, # Оставляем ALL_TYPES, чтобы ловить business_message и др.
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
        # raise e # Можно раскомментировать, чтобы ошибка вебхука роняла старт

# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Gemini Auto-Responder Bot...")

    # Историю инициализируем через defaultdict выше
    # Gemini настроили выше

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Регистрация обработчиков ---
    # 1. Логгер всех обновлений (для отладки)
    application.add_handler(TypeHandler(Update, log_all_updates), group=-1)

    # 2. Основной обработчик для БИЗНЕС-сообщений
    # Используем filters.UpdateType.BUSINESS_MESSAGE
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))

    # Можно добавить обработчик для отредактированных бизнес-сообщений, если нужно
    # application.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, handle_edited_business_message))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
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