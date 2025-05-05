import logging
import os
import asyncio
import json
from collections import deque # Для хранения истории

# --- НОВОЕ: Импорт для Gemini ---
import google.generativeai as genai

from telegram import Update
from telegram.ext import (
    Application,
    # MessageHandler, # Пока не используем MessageHandler напрямую для бизнес-сообщений
    TypeHandler,    # Используем TypeHandler для перехвата всех обновлений
    ContextTypes,
    # filters,      # filters пока не нужны явно
)
from telegram.constants import ChatType
from telegram.error import TelegramError

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Уменьшаем спам от библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google.generativeai").setLevel(logging.INFO) # Логи Gemini могут быть полезны
logger = logging.getLogger(__name__)

# --- Переменные окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")
# --- НОВОЕ: Ключ Gemini API ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- Настройки для бота и Gemini ---
MAX_HISTORY_PER_CHAT = 50 # Сколько последних сообщений помнить
MY_NAME_FOR_HISTORY = "киткат" # Как бот будет представлять тебя в истории для Gemini
# --- НОВОЕ: Системный промпт для Gemini ---
SYSTEM_PROMPT = f"""Ты — ИИ-ассистент, отвечающий на сообщения в Telegram вместо пользователя по имени '{MY_NAME_FOR_HISTORY}'.
Тебе будет предоставлена история переписки (роль 'user' - собеседник, роль 'model' - предыдущие ответы '{MY_NAME_FOR_HISTORY}').
Твоя задача — сгенерировать следующий ответ от имени '{MY_NAME_FOR_HISTORY}', сохраняя его стиль и манеру общения, продолжая диалог по существу.
Не используй форматирование типа Markdown. Отвечай только текстом сообщения.
Не добавляй никаких префиксов типа '{MY_NAME_FOR_HISTORY}:'. Просто напиши сам ответ."""

# --- Хранилище истории сообщений (в памяти) ---
# Структура: { chat_id: deque([{"role": "user"/"model", "parts": [{"text": "..."}]}], maxlen=...) }
chat_histories = {}

# --- НОВОЕ: Настройка Gemini ---
gemini_model = None # Инициализируем позже

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit() # Проверка ключа Gemini

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID loaded: {MY_TELEGRAM_ID}")
logger.info(f"GEMINI_API_KEY loaded: YES")

# --- Функции для работы с историей ---
def update_chat_history(chat_id: int, role: str, text: str):
    """Добавляет сообщение в историю чата."""
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_PER_CHAT)
    # Формат для Gemini API
    chat_histories[chat_id].append({"role": role, "parts": [{"text": text}]})
    logger.debug(f"Updated history for chat {chat_id}. New length: {len(chat_histories[chat_id])}")

def get_formatted_history(chat_id: int) -> list:
    """Возвращает историю чата в формате списка для Gemini."""
    return list(chat_histories.get(chat_id, []))

# --- НОВОЕ: Функция для вызова Gemini API ---
async def generate_gemini_response(chat_history: list) -> str | None:
    """Отправляет историю в Gemini и возвращает сгенерированный ответ."""
    global gemini_model
    if not gemini_model:
        logger.error("Gemini model not initialized!")
        return None

    if not chat_history:
        logger.warning("Cannot generate response for empty history.")
        return None

    logger.info(f"Sending request to Gemini with {len(chat_history)} history entries.")
    # logger.debug(f"Gemini History Payload: {chat_history}") # Раскомментируй для детальной отладки

    try:
        # Важно: Передаем системный промпт через system_instruction, если модель поддерживает (gemini-pro поддерживает)
        # История передается как список словарей
        response = await gemini_model.generate_content_async(
            chat_history,
            generation_config=genai.types.GenerationConfig(
                # Настройки генерации (можно настроить температуру, top_k, top_p)
                # temperature=0.7 # Пример
            ),
            safety_settings={'HARM_CATEGORY_HARASSMENT': 'block_none', # Пример настройки безопасности
                             'HARM_CATEGORY_HATE_SPEECH': 'block_none',
                             'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none',
                             'HARM_CATEGORY_DANGEROUS_CONTENT': 'block_none'}
        )

        # Обработка ответа
        if response and response.parts:
            generated_text = "".join(part.text for part in response.parts).strip()
            # Проверка на пустой ответ или стандартные отказы Gemini
            if generated_text and "I cannot fulfill this request" not in generated_text:
                 logger.info(f"Received response from Gemini: '{generated_text[:50]}...'")
                 return generated_text
            else:
                 logger.warning(f"Gemini returned an empty or refusal response: {response.text if hasattr(response, 'text') else '[No text]'}")
                 return None
        elif response and response.prompt_feedback:
             logger.warning(f"Gemini request blocked due to safety settings or other issues: {response.prompt_feedback}")
             return None
        else:
            logger.warning(f"Gemini returned an unexpected or empty response structure: {response}")
            return None

    except Exception as e:
        logger.error(f"Error calling Gemini API: {type(e).__name__}: {e}", exc_info=True)
        return None

# --- Основной обработчик бизнес-сообщений ---
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает обновления, связанные с бизнес-аккаунтом."""
    # Логируем всё обновление, чтобы видеть структуру
    logger.info(f"--- Received Business Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

    # --- Ищем бизнес-сообщение в обновлении ---
    # Telegram может менять структуру, проверяем наличие business_message
    business_message = update.business_message
    if not business_message:
        logger.debug("Update does not contain a business_message.")
        # Можно также проверить update.edited_business_message и т.д., если нужно
        return

    # Извлекаем данные из бизнес-сообщения
    chat = business_message.chat
    sender = business_message.from_user # Пользователь, который написал БИЗНЕС-АККАУНТУ
    text = business_message.text

    # --- Проверка: Игнорируем сообщения без текста ---
    if not text:
        logger.debug(f"Ignoring non-text business message in chat {chat.id}")
        return

    # --- Проверка: НЕ реагируем на сообщения, отправленные САМИМ бизнес-аккаунтом ---
    # Это ВАЖНО, чтобы бот не отвечал сам себе или на сообщения, отправленные вручную
    # ID бизнес-аккаунта обычно совпадает с ID чата в business_message? Проверим.
    # ИЛИ нужно проверять, что sender.id НЕ равен ID аккаунта (который = MY_TELEGRAM_ID?)
    # Точный способ определить исходящее сообщение нужно проверить по реальным логам.
    # Пока предполагаем, что если sender есть и его ID не MY_TELEGRAM_ID, то это входящее.
    if sender and sender.id == MY_TELEGRAM_ID:
         logger.info(f"Ignoring outgoing business message sent by account {MY_TELEGRAM_ID} in chat {chat.id}")
         return

    chat_id = chat.id

    # Определяем имя отправителя для истории
    sender_name = "Собеседник" # Имя по умолчанию
    if sender:
        sender_name = sender.first_name or f"User_{sender.id}" # Используем имя или ID

    # 1. Обновляем историю ВХОДЯЩИМ сообщением
    update_chat_history(chat_id, "user", text)

    # 2. Получаем историю для Gemini
    current_history = get_formatted_history(chat_id)

    # 3. Генерируем ответ через Gemini
    gemini_response = await generate_gemini_response(current_history)

    if gemini_response:
        # 4. Обновляем историю СГЕНЕРИРОВАННЫМ ответом (как будто его отправили)
        update_chat_history(chat_id, "model", gemini_response)

        # 5. Отправляем предложенный ответ ТЕБЕ в личку
        try:
            forward_text = f"🤖 *Suggested reply for chat {chat_id}* ({sender_name}):\n───────\n{gemini_response}"
            # Попробуем отправить с Markdown (если Gemini не выдаст конфликтующие символы)
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=forward_text,
                parse_mode='Markdown' # Используем обычный Markdown для простоты
            )
            logger.info(f"Sent suggested reply for chat {chat_id} to {MY_TELEGRAM_ID}")
        except TelegramError as e:
            logger.error(f"Failed to send suggested reply to {MY_TELEGRAM_ID}: {e}")
            # Пробуем отправить как обычный текст
            try:
                forward_text_plain = f"🤖 Suggested reply for chat {chat_id} ({sender_name}):\n───────\n{gemini_response}"
                await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=forward_text_plain)
                logger.info(f"Sent suggested reply (plain) for chat {chat_id} to {MY_TELEGRAM_ID}")
            except Exception as e2:
                logger.error(f"Failed to send suggested reply (plain retry) to {MY_TELEGRAM_ID}: {e2}")
    else:
        logger.warning(f"No response generated by Gemini for chat {chat_id}.")
        # Можно отправить уведомление себе, что ответа нет
        # await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"⚠️ Не удалось сгенерировать ответ для чата {chat_id}.")


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
            # Указываем типы обновлений, которые хотим получать
            # Важно включить 'business_message' и, возможно, другие связанные типы
            allowed_updates=[
                "message", # Обычные сообщения (если бот используется и вне Business)
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "business_connection", # Связь с бизнес-аккаунтом
                "business_message",    # Новое сообщение для бизнес-аккаунта
                "edited_business_message", # Измененное бизнес-сообщение
                "deleted_business_messages", # Удаленные бизнес-сообщения
                "my_chat_member",
                "chat_member",
                # Можно добавить другие по мере необходимости
             ],
            drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.url == webhook_full_url: logger.info("Webhook successfully set!")
        else: logger.warning(f"Webhook URL reported differ: {webhook_info.url}")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}", exc_info=True)
        # raise e # Раскомментируй, чтобы падать при ошибке вебхука


# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")

    # --- НОВОЕ: Инициализация Gemini ---
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        # Выбираем модель (gemini-1.5-flash - быстрая и недорогая, gemini-pro - стандартная)
        gemini_model = genai.GenerativeModel(
             model_name="gemini-1.5-flash", # Или 'gemini-pro'
             system_instruction=SYSTEM_PROMPT # Передаем системный промпт при инициализации
        )
        # Пробный вызов для проверки ключа и модели (опционально)
        # asyncio.run(gemini_model.generate_content_async("Test prompt"))
        logger.info(f"Gemini model '{gemini_model.model_name}' initialized successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to initialize Gemini: {e}", exc_info=True)
        exit()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
    # 1. Логгер для всех обновлений (для отладки)
    # application.add_handler(TypeHandler(Update, log_all_updates), group=-1) # Можно включить при отладке

    # 2. Основной обработчик для бизнес-обновлений
    # Мы используем TypeHandler, так как специального фильтра для business_message может не быть
    # Внутри handle_business_update мы проверяем наличие update.business_message
    application.add_handler(TypeHandler(Update, handle_business_update))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        # Запускаем вебхук (asyncio.run сама обработает корутину)
        asyncio.run(application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_full_url
        ))
    except ValueError as e:
        logger.critical(f"CRITICAL ERROR during asyncio.run: {e}", exc_info=True)
    except Exception as e:
         logger.critical(f"CRITICAL ERROR: Webhook server failed to start or run: {e}", exc_info=True)
    finally:
         logger.info("Webhook server shut down.")