import logging
import os
import asyncio
import json
from collections import deque
import google.generativeai as genai

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup # <--- Добавляем кнопки
from telegram.ext import (
    Application,
    TypeHandler,
    ContextTypes,
    # CallbackQueryHandler, # <--- Раскомментируем, если будем обрабатывать нажатия
)
from telegram.constants import ChatType, ParseMode # <--- Добавляем ParseMode для HTML
from telegram.error import TelegramError, Forbidden, BadRequest

# --- Настройки и переменные (без изменений) ---
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

MAX_HISTORY_PER_CHAT = 30
DEBOUNCE_DELAY = 15
MY_NAME_FOR_HISTORY = "киткат"
SYSTEM_PROMPT = f"""Ты — ИИ-ассистент, отвечающий на сообщения в Telegram вместо пользователя по имени '{MY_NAME_FOR_HISTORY}'.
Тебе будет предоставлена история переписки (роль 'user' - собеседник, роль 'model' - предыдущие ответы '{MY_NAME_FOR_HISTORY}').
Твоя задача — сгенерировать следующий ответ от имени '{MY_NAME_FOR_HISTORY}', сохраняя его стиль и манеру общения, продолжая диалог по существу.
Не используй форматирование типа Markdown. Отвечай только текстом сообщения.
Не добавляй никаких префиксов типа '{MY_NAME_FOR_HISTORY}:'. Просто напиши сам ответ."""

chat_histories = {}
debounce_tasks = {}
gemini_model = None

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
logger.info(f"History length: {MAX_HISTORY_PER_CHAT}, Debounce delay: {DEBOUNCE_DELAY}s")

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
            generation_config=genai.types.GenerationConfig(temperature=0.7),
            safety_settings={'HARM_CATEGORY_HARASSMENT': 'block_none', 'HARM_CATEGORY_HATE_SPEECH': 'block_none',
                             'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none', 'HARM_CATEGORY_DANGEROUS_CONTENT': 'block_none'}
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

# --- ИЗМЕНЕННАЯ: Функция обработки чата ПОСЛЕ задержки ---
async def process_chat_after_delay(chat_id: int, sender_name: str, context: ContextTypes.DEFAULT_TYPE):
    """Вызывается после задержки дебаунса для генерации и отправки ответа."""
    logger.info(f"Debounce timer expired for chat {chat_id}. Processing...")
    current_history = get_formatted_history(chat_id)
    gemini_response = await generate_gemini_response(current_history)

    if gemini_response:
        update_chat_history(chat_id, "model", gemini_response) # Обновляем историю ДО отправки себе

        # --- ИЗМЕНЕНИЯ: Русский UI, моноширинный текст и кнопка ---
        try:
            # 1. Формируем текст сообщения на русском и с тегом <code>
            # Используем html.escape для безопасности, если sender_name может содержать < > &
            import html
            safe_sender_name = html.escape(sender_name)
            # Оборачиваем ответ Gemini в <code>
            escaped_gemini_response = html.escape(gemini_response)
            reply_text = (
                f"🤖 <b>Предложенный ответ для чата {chat_id}</b> (<i>{safe_sender_name}</i>):\n"
                f"──────────────────\n"
                f"<code>{escaped_gemini_response}</code>" # <--- Моноширинный текст
            )

            # 2. Создаем inline-кнопку
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Отправить в чат", callback_data=f"send_{chat_id}")]
                # Можно добавить другие кнопки, например, "Редактировать", "Отмена"
            ])

            # 3. Отправляем сообщение с кнопкой и HTML-разметкой
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=reply_text,
                reply_markup=keyboard, # <--- Добавляем клавиатуру
                parse_mode=ParseMode.HTML # <--- Указываем HTML
            )
            logger.info(f"Sent suggested reply with button for chat {chat_id} to {MY_TELEGRAM_ID}")

        except TelegramError as e:
            logger.error(f"Failed to send suggested reply (HTML) to {MY_TELEGRAM_ID}: {e}")
            # Пробуем отправить как обычный текст в случае ошибки
            try:
                reply_text_plain = (
                    f"🤖 Предложенный ответ для чата {chat_id} ({sender_name}):\n"
                    f"──────────────────\n"
                    f"{gemini_response}" # Без <code>
                )
                await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=reply_text_plain)
                logger.info(f"Sent suggested reply (plain fallback) for chat {chat_id} to {MY_TELEGRAM_ID}")
            except Exception as e2:
                logger.error(f"Failed to send suggested reply (plain fallback) to {MY_TELEGRAM_ID}: {e2}")
        # --- Конец изменений ---
    else:
        logger.warning(f"No response generated by Gemini for chat {chat_id} after debounce.")

    if chat_id in debounce_tasks:
        del debounce_tasks[chat_id]
        logger.debug(f"Removed completed debounce task for chat {chat_id}")

# --- Обработчик бизнес-сообщений handle_business_update (без изменений) ---
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"--- Received Business Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")
    business_message = update.business_message
    if not business_message: logger.debug("Update does not contain a business_message."); return
    chat = business_message.chat
    sender = business_message.from_user
    text = business_message.text
    if not text: logger.debug(f"Ignoring non-text business message in chat {chat.id}"); return
    if sender and sender.id == MY_TELEGRAM_ID: logger.info(f"Ignoring outgoing msg from {MY_TELEGRAM_ID}"); return

    chat_id = chat.id
    sender_name = "Собеседник"
    if sender: sender_name = sender.first_name or f"User_{sender.id}"

    update_chat_history(chat_id, "user", text) # Обновляем историю сразу

    if chat_id in debounce_tasks: # Отменяем старую задачу
        logger.debug(f"Cancelling previous debounce task for chat {chat_id}")
        try: debounce_tasks[chat_id].cancel()
        except Exception as e: logger.error(f"Error cancelling task for chat {chat_id}: {e}")

    logger.info(f"Scheduling new response generation for chat {chat_id} in {DEBOUNCE_DELAY}s")
    async def delayed_processing(): # Создаем новую задачу
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
            logger.debug(f"Debounce delay finished for chat {chat_id}. Starting processing.")
            await process_chat_after_delay(chat_id, sender_name, context)
        except asyncio.CancelledError:
            logger.info(f"Debounce task for chat {chat_id} was cancelled.")
        except Exception as e:
            logger.error(f"Error in delayed processing for chat {chat_id}: {e}", exc_info=True)
    task = asyncio.create_task(delayed_processing())
    debounce_tasks[chat_id] = task
    logger.debug(f"Scheduled task {task.get_name()} for chat {chat_id}")

# --- Функция post_init ---
async def post_init(application: Application):
    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    logger.info(f"Attempting to set webhook using: {webhook_full_url}")
    try:
        # Добавляем 'callback_query' в allowed_updates, если планируем обрабатывать нажатия кнопок
        await application.bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=[
                "message", "edited_message", "channel_post", "edited_channel_post",
                "business_connection", "business_message", "edited_business_message",
                "deleted_business_messages", "my_chat_member", "chat_member",
                "callback_query" # <--- Добавлено для кнопок
             ],
            drop_pending_updates=True
        )
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.url == webhook_full_url: logger.info("Webhook successfully set!")
        else: logger.warning(f"Webhook URL reported differ: {webhook_info.url}")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}", exc_info=True)

# --- Основная точка входа ---
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=SYSTEM_PROMPT)
        logger.info(f"Gemini model '{gemini_model.model_name}' initialized successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to initialize Gemini: {e}", exc_info=True); exit()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Основной обработчик для всех обновлений
    application.add_handler(TypeHandler(Update, handle_business_update))
    # --- ЕСЛИ нужно будет обрабатывать нажатие кнопки ---
    # from telegram.ext import CallbackQueryHandler
    # async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #     query = update.callback_query
    #     await query.answer() # Отвечаем на колбэк, чтобы кнопка перестала "крутиться"
    #     callback_data = query.data
    #     logger.info(f"Button pressed with data: {callback_data}")
    #     if callback_data and callback_data.startswith("send_"):
    #         target_chat_id = callback_data.split("_", 1)[1]
    #         # Здесь нужна логика получения ТЕКСТА сообщения (из query.message.text?)
    #         # и отправки его в target_chat_id от имени бизнес-аккаунта
    #         # Например:
    #         # original_reply_text = query.message.text # Или parse query.message.html_text
    #         # await context.bot.send_message(chat_id=target_chat_id, text=original_reply_text)
    #         await query.edit_message_text(text=f"{query.message.text}\n\n[Кнопка обработана - отправка пока не реализована]", parse_mode=ParseMode.HTML)
    # application.add_handler(CallbackQueryHandler(button_handler))
    # --- Конец блока обработки кнопки ---

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        asyncio.run(application.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=webhook_full_url
        ))
    except ValueError as e: logger.critical(f"CRITICAL ERROR asyncio.run: {e}", exc_info=True)
    except Exception as e: logger.critical(f"CRITICAL ERROR Webhook server: {e}", exc_info=True)
    finally: logger.info("Webhook server shut down.")