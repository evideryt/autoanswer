import logging
import os
import asyncio
import json
from collections import deque
import google.generativeai as genai
import html

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.constants import ChatType, ParseMode
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
pending_replies = {} # Теперь храним {chat_id: (response_text, business_connection_id)}
gemini_model = None

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ (без изменений) ---
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

# --- Функции истории и Gemini (без изменений) ---
def update_chat_history(chat_id: int, role: str, text: str):
    if not text or not text.strip(): logger.warning(f"Attempted to add empty message to history for chat {chat_id}. Skipping."); return
    if chat_id not in chat_histories: chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_PER_CHAT)
    chat_histories[chat_id].append({"role": role, "parts": [{"text": text.strip()}]})
    logger.debug(f"Updated history for chat {chat_id}. Role: {role}. New length: {len(chat_histories[chat_id])}")

def get_formatted_history(chat_id: int) -> list:
    return list(chat_histories.get(chat_id, []))

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
            if generated_text and "cannot fulfill" not in generated_text.lower() and "unable to process" not in generated_text.lower():
                 logger.info(f"Received response from Gemini: '{generated_text[:50]}...'")
                 return generated_text
            else: logger.warning(f"Gemini returned empty/refusal: {response.text if hasattr(response, 'text') else '[No text]'}")
        elif response and response.prompt_feedback: logger.warning(f"Gemini request blocked: {response.prompt_feedback}")
        else: logger.warning(f"Gemini returned unexpected structure: {response}")
        return None
    except Exception as e:
        logger.error(f"Error calling Gemini API: {type(e).__name__}: {e}", exc_info=True)
        return None

# --- ИЗМЕНЕННАЯ Функция обработки чата ПОСЛЕ задержки ---
async def process_chat_after_delay(
    chat_id: int,
    sender_name: str,
    business_connection_id: str | None, # <--- ПРИНИМАЕМ ID СВЯЗИ
    context: ContextTypes.DEFAULT_TYPE
):
    logger.info(f"Debounce timer expired for chat {chat_id}. Processing...")
    current_history = get_formatted_history(chat_id)
    gemini_response = await generate_gemini_response(current_history)

    if gemini_response:
        # Сохраняем ответ И ID СВЯЗИ для кнопки
        pending_replies[chat_id] = (gemini_response, business_connection_id)
        logger.debug(f"Stored pending reply for chat {chat_id} with connection_id {business_connection_id}")

        # Отправляем предложенный ответ ТЕБЕ с кнопкой
        try:
            safe_sender_name = html.escape(sender_name)
            escaped_gemini_response = html.escape(gemini_response)
            reply_text = (
                f"🤖 <b>Предложенный ответ для чата {chat_id}</b> (<i>{safe_sender_name}</i>):\n"
                f"──────────────────\n"
                f"<code>{escaped_gemini_response}</code>"
            )
            # --- ИЗМЕНЕНО: Добавляем connection_id в callback_data ---
            # Формат: "send_<chat_id>_<connection_id>" (если ID есть)
            # Если ID нет (например, старое сообщение?), кнопка не будет иметь ID
            callback_data = f"send_{chat_id}"
            if business_connection_id:
                callback_data += f"_{business_connection_id}"

            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить в чат", callback_data=callback_data)]])

            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID, text=reply_text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
            logger.info(f"Sent suggested reply with button (cb: {callback_data}) for chat {chat_id} to {MY_TELEGRAM_ID}")
        except TelegramError as e:
            logger.error(f"Failed to send suggested reply (HTML) to {MY_TELEGRAM_ID}: {e}")
            # Fallback и т.д. (оставляем как есть)
            try:
                reply_text_plain = (f"🤖 Предложенный ответ для чата {chat_id} ({sender_name}):\n"
                                  f"──────────────────\n{gemini_response}\n(Не удалось добавить кнопку отправки)")
                await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=reply_text_plain)
                logger.info(f"Sent suggested reply (plain fallback) for chat {chat_id} to {MY_TELEGRAM_ID}")
            except Exception as e2: logger.error(f"Failed to send suggested reply (plain fallback) to {MY_TELEGRAM_ID}: {e2}")
    else:
        logger.warning(f"No response generated by Gemini for chat {chat_id} after debounce.")

    if chat_id in debounce_tasks:
        del debounce_tasks[chat_id]
        logger.debug(f"Removed completed debounce task for chat {chat_id}")


# --- ИЗМЕНЕННЫЙ Основной обработчик бизнес-сообщений ---
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- ИЗМЕНЕНО: Логируем все обновление для поиска business_connection_id ---
    logger.info(f"--- Received Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

    # Определяем, какое сообщение обрабатывать и получаем его ID связи
    business_connection_id = None
    message_to_process = None

    if update.business_message:
        message_to_process = update.business_message
        business_connection_id = update.business_message.business_connection_id # <--- Получаем ID
        logger.info(f"--- Received Business Message (ID: {message_to_process.message_id}, ConnID: {business_connection_id}) ---")
    elif update.edited_business_message:
        message_to_process = update.edited_business_message
        # В edited_business_message может не быть connection_id, нужно проверить!
        business_connection_id = getattr(update.edited_business_message, 'business_connection_id', None)
        logger.info(f"--- Received Edited Business Message (ID: {message_to_process.message_id}, ConnID: {business_connection_id}) ---")
    else:
        # logger.debug("Update is not a business_message or edited_business_message. Ignored.")
        return

    chat = message_to_process.chat
    sender = message_to_process.from_user
    text = message_to_process.text

    if not text: logger.debug(f"Ignoring non-text business message in chat {chat.id}"); return

    chat_id = chat.id
    is_outgoing = sender and sender.id == MY_TELEGRAM_ID

    if is_outgoing:
        logger.info(f"Processing OUTGOING business message in chat {chat_id}")
        update_chat_history(chat_id, "model", text)
        # Отменяем таймер дебаунса, если мы ответили вручную раньше
        if chat_id in debounce_tasks:
             logger.debug(f"Cancelling debounce task for chat {chat_id} due to outgoing message.")
             try: debounce_tasks[chat_id].cancel()
             except Exception as e: logger.error(f"Error cancelling task for chat {chat_id}: {e}")
             # Удаляем отмененную задачу
             del debounce_tasks[chat_id]
        return

    # --- Если сообщение ВХОДЯЩЕЕ ---
    logger.info(f"Processing INCOMING business message from user {sender.id if sender else 'Unknown'} in chat {chat_id} via ConnID: {business_connection_id}")
    sender_name = "Собеседник";
    if sender: sender_name = sender.first_name or f"User_{sender.id}"
    update_chat_history(chat_id, "user", text)

    if chat_id in debounce_tasks:
        logger.debug(f"Cancelling previous debounce task for chat {chat_id}")
        try: debounce_tasks[chat_id].cancel()
        except Exception as e: logger.error(f"Error cancelling task for chat {chat_id}: {e}")

    logger.info(f"Scheduling new response generation for chat {chat_id} in {DEBOUNCE_DELAY}s")
    async def delayed_processing():
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
            logger.debug(f"Debounce delay finished for chat {chat_id}. Starting processing.")
            # --- ИЗМЕНЕНО: Передаем business_connection_id ---
            await process_chat_after_delay(chat_id, sender_name, business_connection_id, context)
        except asyncio.CancelledError: logger.info(f"Debounce task for chat {chat_id} was cancelled.")
        except Exception as e: logger.error(f"Error in delayed processing for chat {chat_id}: {e}", exc_info=True)

    task = asyncio.create_task(delayed_processing())
    debounce_tasks[chat_id] = task
    logger.debug(f"Scheduled task {task.get_name()} for chat {chat_id}")


# --- ИЗМЕНЕННЫЙ Обработчик нажатий на кнопку ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: logger.warning("Received update without callback_query in button_handler"); return

    logger.info("--- button_handler triggered ---")
    logger.debug(f"CallbackQuery Data: {query.data}")
    try:
        await query.answer()
        logger.debug("Callback query answered.")
    except Exception as e:
        logger.error(f"CRITICAL: Failed to answer callback query: {e}. Stopping handler."); return

    data = query.data
    # --- ИЗМЕНЕНО: Парсим callback_data с connection_id ---
    if not data or not data.startswith("send_"):
        logger.warning(f"Received unhandled callback_data: {data}")
        # ... (обработка ошибки) ...
        return

    target_chat_id = None
    business_connection_id = None
    response_text = None
    try:
        parts = data.split("_", 2) # Ожидаем "send", chat_id, connection_id
        if len(parts) < 2: raise ValueError("Callback data too short")

        target_chat_id_str = parts[1]
        target_chat_id = int(target_chat_id_str)
        # Получаем connection_id, если он есть (может не быть для старых кнопок)
        business_connection_id = parts[2] if len(parts) > 2 else None

        logger.info(f"Button press: Attempting to send reply to chat {target_chat_id} using ConnID: {business_connection_id}")

        # Получаем данные из pending_replies
        pending_data = pending_replies.pop(target_chat_id, None)
        if not pending_data:
            logger.warning(f"No pending reply found for chat {target_chat_id}.")
            # ... (обработка ошибки) ...
            return

        response_text, stored_conn_id = pending_data
        # Проверяем, совпадает ли connection_id из кнопки и сохраненный (если оба есть)
        if business_connection_id and stored_conn_id and business_connection_id != stored_conn_id:
             logger.warning(f"Connection ID mismatch! Button had {business_connection_id}, stored was {stored_conn_id}. Using stored ID for sending.")
             # Можно решить, какой использовать, пока используем сохраненный
             business_connection_id = stored_conn_id
        elif not business_connection_id and stored_conn_id:
             # Если кнопка старая без ID, но в данных он есть - используем его
             business_connection_id = stored_conn_id
             logger.debug(f"Using stored connection ID {business_connection_id} as button had none.")


        if not response_text: # Дополнительная проверка
             logger.error(f"Extracted response_text is None for chat {target_chat_id}!")
             # ... (обработка ошибки) ...
             return

        logger.debug(f"Found pending reply for chat {target_chat_id}: '{response_text[:50]}...'")
        logger.info(f"Attempting context.bot.send_message with: chat_id={target_chat_id}, text='{response_text[:50]}...', business_connection_id={business_connection_id}")

        # --- Отправка сообщения с business_connection_id ---
        message_sent = False
        last_exception = None
        # Убираем цикл повтора, чтобы не усложнять. Если с ID не сработает - то не сработает.
        try:
            sent_message = await context.bot.send_message(
                chat_id=target_chat_id,
                text=response_text,
                business_connection_id=business_connection_id # <--- ПЕРЕДАЕМ ID СВЯЗИ!
            )
            logger.info(f"Successfully sent message {sent_message.message_id} to chat {target_chat_id} via ConnID {business_connection_id}")
            update_chat_history(target_chat_id, "model", response_text)
            logger.debug(f"Added sent message to history for chat {target_chat_id}")
            await query.edit_message_text(
                text=query.message.text_html + "\n\n<b>✅ Отправлено!</b>",
                parse_mode=ParseMode.HTML, reply_markup=None
            )
            logger.debug(f"Edited original suggestion message for chat {target_chat_id}")
            message_sent = True

        except Forbidden as e: last_exception = e; logger.error(f"Forbidden error sending to chat {target_chat_id} via ConnID {business_connection_id}. Error: {e}")
        except BadRequest as e: last_exception = e; logger.error(f"BadRequest error sending to chat {target_chat_id} via ConnID {business_connection_id}: {e}.")
        except Exception as e: last_exception = e; logger.error(f"Unexpected error sending to chat {target_chat_id} via ConnID {business_connection_id}: {e}", exc_info=True)

        # Если не отправлено
        if not message_sent:
             logger.warning(f"Failed to send message to chat {target_chat_id} via ConnID {business_connection_id}.")
             error_text = "<b>❌ Неизвестная ошибка отправки.</b>"
             if isinstance(last_exception, Forbidden): error_text = "<b>❌ Ошибка:</b> Нет прав на отправку (Forbidden). Проверьте Business Connection / настройки чата."
             elif isinstance(last_exception, BadRequest): error_text = f"<b>❌ Ошибка отправки (BadRequest):</b> {html.escape(str(last_exception))}"
             try:
                await query.edit_message_text(text=query.message.text_html + f"\n\n{error_text}", parse_mode=ParseMode.HTML, reply_markup=None)
             except Exception as edit_e: logger.error(f"Failed to edit message after send failure: {edit_e}")

    # Обработка других ошибок
    except (ValueError, IndexError) as e: logger.error(f"Error parsing callback_data '{data}': {e}"); # ... обработка ошибки ...
    except Exception as e: logger.error(f"Unexpected error in button_handler: {e}", exc_info=True); # ... обработка ошибки ...


# --- Функция post_init (без изменений) ---
async def post_init(application: Application):
    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    logger.info(f"Attempting to set webhook using: {webhook_full_url}")
    try:
        await application.bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=[
                "message", "edited_message", "channel_post", "edited_channel_post",
                "business_connection", "business_message", "edited_business_message",
                "deleted_business_messages", "my_chat_member", "chat_member",
                "callback_query"
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
        gemini_model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=SYSTEM_PROMPT)
        logger.info(f"Gemini model '{gemini_model.model_name}' initialized successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to initialize Gemini: {e}", exc_info=True); exit()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_update))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, handle_business_update))
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        asyncio.run(application.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=webhook_full_url
        ))
    except ValueError as e: logger.critical(f"CRITICAL ERROR asyncio.run: {e}", exc_info=True)
    except Exception as e: logger.critical(f"CRITICAL ERROR Webhook server: {e}", exc_info=True)
    finally: logger.info("Webhook server shut down.")