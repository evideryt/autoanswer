import logging
import os
import asyncio
import json
from collections import deque
import google.generativeai as genai

from telegram import Update
from telegram.ext import (
    Application,
    TypeHandler,
    ContextTypes,
)
from telegram.constants import ChatType
from telegram.error import TelegramError, Forbidden, BadRequest # Добавим ошибки для отмены задач

# --- Настройки и переменные ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google.generativeai").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))
MY_TELEGRAM_ID = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- Настройки для бота и Gemini ---
MAX_HISTORY_PER_CHAT = 30 # <--- ИЗМЕНЕНО: Длина истории
DEBOUNCE_DELAY = 15 # <--- ДОБАВЛЕНО: Задержка в секундах
MY_NAME_FOR_HISTORY = "киткат"
SYSTEM_PROMPT = f"""Ты — ИИ-ассистент, отвечающий на сообщения в Telegram вместо пользователя по имени '{MY_NAME_FOR_HISTORY}'.
Тебе будет предоставлена история переписки (роль 'user' - собеседник, роль 'model' - предыдущие ответы '{MY_NAME_FOR_HISTORY}').
Твоя задача — сгенерировать следующий ответ от имени '{MY_NAME_FOR_HISTORY}', сохраняя его стиль и манеру общения, продолжая диалог по существу.
Не используй форматирование типа Markdown. Отвечай только текстом сообщения.
Не добавляй никаких префиксов типа '{MY_NAME_FOR_HISTORY}:'. Просто напиши сам ответ."""

# --- Хранилище истории и задач дебаунса ---
chat_histories = {}
debounce_tasks = {} # <--- ДОБАВЛЕНО: Словарь для задач {chat_id: asyncio.Task}

gemini_model = None

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID is not valid int"); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit()

logger.info(f"BOT_TOKEN loaded: YES")
logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")
logger.info(f"PORT configured: {PORT}")
logger.info(f"MY_TELEGRAM_ID loaded: {MY_TELEGRAM_ID}")
logger.info(f"GEMINI_API_KEY loaded: YES")
logger.info(f"History length: {MAX_HISTORY_PER_CHAT}, Debounce delay: {DEBOUNCE_DELAY}s") # Лог настроек

# --- Функции для работы с историей (без изменений) ---
def update_chat_history(chat_id: int, role: str, text: str):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_PER_CHAT)
    chat_histories[chat_id].append({"role": role, "parts": [{"text": text}]})
    logger.debug(f"Updated history for chat {chat_id}. New length: {len(chat_histories[chat_id])}")

def get_formatted_history(chat_id: int) -> list:
    return list(chat_histories.get(chat_id, []))

# --- Функция для вызова Gemini API (без изменений) ---
async def generate_gemini_response(chat_history: list) -> str | None:
    global gemini_model
    if not gemini_model: logger.error("Gemini model not initialized!"); return None
    if not chat_history: logger.warning("Cannot generate response for empty history."); return None

    logger.info(f"Sending request to Gemini with {len(chat_history)} history entries.")
    try:
        response = await gemini_model.generate_content_async(
            chat_history,
            generation_config=genai.types.GenerationConfig(temperature=0.7), # Пример температуры
            safety_settings={'HARM_CATEGORY_HARASSMENT': 'block_none',
                             'HARM_CATEGORY_HATE_SPEECH': 'block_none',
                             'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none',
                             'HARM_CATEGORY_DANGEROUS_CONTENT': 'block_none'}
        )
        if response and response.parts:
            generated_text = "".join(part.text for part in response.parts).strip()
            if generated_text and "cannot fulfill this request" not in generated_text.lower():
                 logger.info(f"Received response from Gemini: '{generated_text[:50]}...'")
                 return generated_text
            else: logger.warning(f"Gemini returned empty/refusal: {response.text if hasattr(response, 'text') else '[No text]'}")
        elif response and response.prompt_feedback: logger.warning(f"Gemini request blocked: {response.prompt_feedback}")
        else: logger.warning(f"Gemini returned unexpected structure: {response}")
        return None
    except Exception as e:
        logger.error(f"Error calling Gemini API: {type(e).__name__}: {e}", exc_info=True)
        return None

# --- НОВАЯ: Функция обработки чата ПОСЛЕ задержки ---
async def process_chat_after_delay(chat_id: int, sender_name: str, context: ContextTypes.DEFAULT_TYPE):
    """Вызывается после задержки дебаунса для генерации и отправки ответа."""
    logger.info(f"Debounce timer expired for chat {chat_id}. Processing...")

    # Получаем АКТУАЛЬНУЮ историю на момент вызова
    current_history = get_formatted_history(chat_id)

    # Генерируем ответ через Gemini
    gemini_response = await generate_gemini_response(current_history)

    if gemini_response:
        # Обновляем историю СГЕНЕРИРОВАННЫМ ответом
        # Важно: делаем это здесь, ПОСЛЕ генерации, чтобы Gemini видел только реальный диалог
        update_chat_history(chat_id, "model", gemini_response)

        # Отправляем предложенный ответ ТЕБЕ в личку
        try:
            forward_text = f"🤖 *Suggested reply for chat {chat_id}* ({sender_name}):\n───────\n{gemini_response}"
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID, text=forward_text, parse_mode='Markdown'
            )
            logger.info(f"Sent suggested reply for chat {chat_id} to {MY_TELEGRAM_ID}")
        except TelegramError as e:
            logger.error(f"Failed to send suggested reply to {MY_TELEGRAM_ID}: {e}")
            try: # Пробуем без Markdown
                forward_text_plain = f"🤖 Suggested reply for chat {chat_id} ({sender_name}):\n───────\n{gemini_response}"
                await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=forward_text_plain)
                logger.info(f"Sent suggested reply (plain) for chat {chat_id} to {MY_TELEGRAM_ID}")
            except Exception as e2:
                logger.error(f"Failed to send suggested reply (plain retry) to {MY_TELEGRAM_ID}: {e2}")
    else:
        logger.warning(f"No response generated by Gemini for chat {chat_id} after debounce.")
        # Можно уведомить себя об отсутствии ответа
        # await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"⚠️ Не удалось сгенерировать ответ для чата {chat_id} после задержки.")

    # Удаляем задачу из словаря после завершения
    if chat_id in debounce_tasks:
        del debounce_tasks[chat_id]
        logger.debug(f"Removed completed debounce task for chat {chat_id}")


# --- ИЗМЕНЕННЫЙ Основной обработчик бизнес-сообщений ---
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"--- Received Business Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

    business_message = update.business_message
    if not business_message:
        logger.debug("Update does not contain a business_message.")
        return

    chat = business_message.chat
    sender = business_message.from_user
    text = business_message.text

    if not text:
        logger.debug(f"Ignoring non-text business message in chat {chat.id}")
        return

    # Проверка на исходящее (примерная, может потребовать уточнения по логам)
    if sender and sender.id == MY_TELEGRAM_ID:
         logger.info(f"Ignoring outgoing business message from {MY_TELEGRAM_ID} in chat {chat.id}")
         return

    chat_id = chat.id
    sender_name = "Собеседник"
    if sender: sender_name = sender.first_name or f"User_{sender.id}"

    # 1. Обновляем историю СРАЗУ
    update_chat_history(chat_id, "user", text)

    # 2. Отменяем предыдущую задачу дебаунса для этого чата (если есть)
    if chat_id in debounce_tasks:
        logger.debug(f"Cancelling previous debounce task for chat {chat_id}")
        try:
            debounce_tasks[chat_id].cancel()
        except Exception as e: # Ловим общие ошибки на всякий случай
             logger.error(f"Error cancelling task for chat {chat_id}: {e}")
        # Удаляем отмененную задачу из словаря
        # del debounce_tasks[chat_id] # Удалим ее в process_chat_after_delay

    # 3. Создаем и запускаем НОВУЮ задачу с задержкой
    logger.info(f"Scheduling new response generation for chat {chat_id} in {DEBOUNCE_DELAY}s")
    # Создаем корутину, которая сначала ждет, потом обрабатывает
    async def delayed_processing():
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
            logger.debug(f"Debounce delay finished for chat {chat_id}. Starting processing.")
            # Вызываем основную логику обработки
            await process_chat_after_delay(chat_id, sender_name, context)
        except asyncio.CancelledError:
            logger.info(f"Debounce task for chat {chat_id} was cancelled.")
        except Exception as e:
            logger.error(f"Error in delayed processing for chat {chat_id}: {e}", exc_info=True)
        # finally: # Очистка задачи происходит внутри process_chat_after_delay
            # if chat_id in debounce_tasks:
            #     # Убедимся, что задача удалена, даже если была ошибка
            #     del debounce_tasks[chat_id]

    # Запускаем корутину как задачу и сохраняем ее
    task = asyncio.create_task(delayed_processing())
    debounce_tasks[chat_id] = task
    logger.debug(f"Scheduled task {task.get_name()} for chat {chat_id}")


# --- Функция post_init (без изменений) ---
async def post_init(application: Application):
    # ... (код без изменений, как в прошлый раз) ...
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
            allowed_updates=[ # Оставляем типы обновлений как были
                "message", "edited_message", "channel_post", "edited_channel_post",
                "business_connection", "business_message", "edited_business_message",
                "deleted_business_messages", "my_chat_member", "chat_member",
             ],
            drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.url == webhook_full_url: logger.info("Webhook successfully set!")
        else: logger.warning(f"Webhook URL reported differ: {webhook_info.url}")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}", exc_info=True)


# --- Основная точка входа (без изменений) ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel(
             model_name="gemini-1.5-flash", # Или 'gemini-pro'
             system_instruction=SYSTEM_PROMPT
        )
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
    # Основной обработчик для всех обновлений
    application.add_handler(TypeHandler(Update, handle_business_update))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
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