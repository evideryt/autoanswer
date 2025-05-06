import logging
import os
import asyncio
import json
from collections import deque
import google.generativeai as genai
import html
import time # Для паузы между сообщениями

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
MY_TELEGRAM_ID_STR = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CONFIG_FILE = "adp.txt"

MAX_HISTORY_PER_CHAT = 30
DEBOUNCE_DELAY = 15
MY_NAME_FOR_HISTORY = "киткат"
MESSAGE_SPLIT_DELAY = 0.7

BASE_SYSTEM_PROMPT = ""
MY_CHARACTER_DESCRIPTION = ""
CHAR_DESCRIPTIONS = {}

chat_histories = {}
debounce_tasks = {}
pending_replies = {}
gemini_model = None
MY_TELEGRAM_ID = None # Инициализируем как None

# --- КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕМЕННЫХ ---
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID_STR: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID_STR}') is not a valid integer."); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit()

# --- Функция парсинга конфигурационного файла (без изменений) ---
def parse_config_file(filepath: str):
    global BASE_SYSTEM_PROMPT, MY_CHARACTER_DESCRIPTION, CHAR_DESCRIPTIONS
    logger.info(f"Attempting to parse config file: {filepath}")
    try:
        with open(filepath, 'r', encoding='utf-8') as f: content = f.read()
        sections = {}; current_section_name = None; current_section_content = []
        for line in content.splitlines():
            stripped_line = line.strip()
            if stripped_line.startswith("!!") and len(stripped_line) > 2:
                if current_section_name: sections[current_section_name] = "\n".join(current_section_content).strip()
                current_section_name = stripped_line[2:]; current_section_content = []
            elif current_section_name is not None: current_section_content.append(line)
        if current_section_name: sections[current_section_name] = "\n".join(current_section_content).strip()
        BASE_SYSTEM_PROMPT = sections.get("SYSTEM_PROMPT", "").strip()
        MY_CHARACTER_DESCRIPTION = sections.get("MC", "").strip()
        CHAR_DESCRIPTIONS = {}
        chars_content = sections.get("CHARS", "")
        if chars_content:
            for char_line in chars_content.splitlines():
                if '=' in char_line:
                    parts = char_line.split('=', 1); user_id_str = parts[0].strip(); description = parts[1].strip()
                    if user_id_str.isdigit() and description: CHAR_DESCRIPTIONS[user_id_str] = description
                    else: logger.warning(f"Skipping invalid line in CHARS section: {char_line}")
        if not BASE_SYSTEM_PROMPT: logger.error(f"CRITICAL: '!!SYSTEM_PROMPT' not found or empty in {filepath}.")
        if not MY_CHARACTER_DESCRIPTION: logger.warning(f"'!!MC' not found or empty in {filepath}.")
        logger.info(f"Config loaded from {filepath}:")
        logger.info(f"  SYSTEM_PROMPT: {'Loaded' if BASE_SYSTEM_PROMPT else 'MISSING/EMPTY'}")
        logger.info(f"  MY_CHARACTER_DESCRIPTION: {'Loaded' if MY_CHARACTER_DESCRIPTION else 'MISSING/EMPTY'}")
        logger.info(f"  Loaded {len(CHAR_DESCRIPTIONS)} character descriptions.")
        logger.debug(f"PARSED CHAR_DESCRIPTIONS: {CHAR_DESCRIPTIONS}")
    except FileNotFoundError: logger.critical(f"CRITICAL: Configuration file '{filepath}' not found."); exit()
    except Exception as e: logger.critical(f"CRITICAL: Error parsing config file '{filepath}': {e}", exc_info=True); exit()

# --- Функции истории и Gemini (без изменений) ---
def update_chat_history(chat_id: int, role: str, text: str):
    if not text or not text.strip(): logger.warning(f"Attempted to add empty message to history for chat {chat_id}. Skipping."); return
    if chat_id not in chat_histories: chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_PER_CHAT)
    chat_histories[chat_id].append({"role": role, "parts": [{"text": text.strip()}]})
    logger.debug(f"Updated history for chat {chat_id}. Role: {role}. New length: {len(chat_histories[chat_id])}")

def get_formatted_history(chat_id: int) -> list: return list(chat_histories.get(chat_id, []))

async def generate_gemini_response(dynamic_context_parts: list, chat_history: list) -> str | None:
    global gemini_model; # ... (остальной код функции без изменений) ...
    if not gemini_model: logger.error("Gemini model not initialized!"); return None
    gemini_contents = []; context_block_text = ""
    if MY_CHARACTER_DESCRIPTION: context_block_text += f"Немного информации обо мне ({MY_NAME_FOR_HISTORY}):\n{MY_CHARACTER_DESCRIPTION}\n\n"
    for part in dynamic_context_parts: context_block_text += f"{part}\n\n"
    if context_block_text.strip(): gemini_contents.append({"role": "model", "parts": [{"text": context_block_text.strip()}]}); logger.debug(f"Prepended context block to Gemini contents.")
    gemini_contents.extend(chat_history)
    if not gemini_contents: logger.warning("Cannot generate response for empty Gemini contents."); return None
    logger.info(f"Sending request to Gemini with {len(gemini_contents)} content entries.")
    try:
        response = await gemini_model.generate_content_async(contents=gemini_contents, generation_config=genai.types.GenerationConfig(temperature=0.7),
            safety_settings={'HARM_CATEGORY_HARASSMENT': 'block_none', 'HARM_CATEGORY_HATE_SPEECH': 'block_none', 'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none', 'HARM_CATEGORY_DANGEROUS_CONTENT': 'block_none'})
        if response and response.parts:
            generated_text = "".join(part.text for part in response.parts).strip()
            if generated_text and "cannot fulfill" not in generated_text.lower() and "unable to process" not in generated_text.lower():
                 logger.info(f"Received response from Gemini: '{generated_text[:50]}...'"); return generated_text
            else: logger.warning(f"Gemini returned empty/refusal: {response.text if hasattr(response, 'text') else '[No text]'}")
        elif response and response.prompt_feedback: logger.warning(f"Gemini request blocked: {response.prompt_feedback}")
        else: logger.warning(f"Gemini returned unexpected structure: {response}")
        return None
    except Exception as e: logger.error(f"Error calling Gemini API: {type(e).__name__}: {e}", exc_info=True); return None

# --- Функция обработки чата ПОСЛЕ задержки (без изменений) ---
async def process_chat_after_delay(chat_id: int, sender_name: str, sender_id_str: str, business_connection_id: str | None, context: ContextTypes.DEFAULT_TYPE):
    # ... (код функции без изменений) ...
    logger.info(f"Debounce timer expired for chat {chat_id} with sender {sender_id_str}. Processing...")
    current_history = get_formatted_history(chat_id); dynamic_prompt_parts = []
    logger.debug(f"Looking for description for sender_id_str: '{sender_id_str}' (type: {type(sender_id_str)})")
    interlocutor_description = CHAR_DESCRIPTIONS.get(sender_id_str)
    if interlocutor_description: logger.info(f"FOUND description for sender {sender_id_str}"); dynamic_prompt_parts.append(f"Информация о текущем собеседнике ({sender_name}, ID: {sender_id_str}):\n{interlocutor_description}")
    else: logger.warning(f"Description NOT FOUND for sender ID {sender_id_str}")
    logger.debug(f"Passing dynamic_prompt_parts to generate_gemini_response: {dynamic_prompt_parts}")
    gemini_response_raw = await generate_gemini_response(dynamic_prompt_parts, current_history)
    if gemini_response_raw:
        pending_replies[chat_id] = (gemini_response_raw, business_connection_id); logger.debug(f"Stored RAW pending reply for chat {chat_id}")
        preview_text = gemini_response_raw.replace("!NEWMSG!", "\n\n🔚\n\n")
        try:
            safe_sender_name = html.escape(sender_name); escaped_preview_text = html.escape(preview_text)
            reply_text_html = (f"🤖 <b>Предложенный ответ для чата {chat_id}</b> (<i>{safe_sender_name}</i>):\n"
                               f"──────────────────\n<code>{escaped_preview_text}</code>")
            callback_data = f"send_{chat_id}";
            if business_connection_id: callback_data += f"_{business_connection_id}"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить в чат", callback_data=callback_data)]])
            await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=reply_text_html, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            logger.info(f"Sent suggestion preview for chat {chat_id} to {MY_TELEGRAM_ID}")
        except TelegramError as e: logger.error(f"Failed to send suggestion preview (HTML) to {MY_TELEGRAM_ID}: {e}"); # ... fallback ...
    else: logger.warning(f"No response generated by Gemini for chat {chat_id} after debounce.")
    if chat_id in debounce_tasks: del debounce_tasks[chat_id]; logger.debug(f"Removed completed debounce task for chat {chat_id}")


# --- ИЗМЕНЕННЫЙ Основной обработчик бизнес-сообщений ---
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # logger.info(f"--- Received Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}")

    message_to_process = update.business_message or update.edited_business_message
    if not message_to_process: return

    chat = message_to_process.chat
    sender = message_to_process.from_user
    text = message_to_process.text
    message_id = message_to_process.message_id # ID сообщения для удаления
    business_connection_id = getattr(message_to_process, 'business_connection_id', None)

    if not text: logger.debug(f"Ignoring non-text business message in chat {chat.id}"); return

    chat_id = chat.id
    sender_id_str = str(sender.id) if sender else None

    # --- НОВЫЙ БЛОК: Обработка команды /v от тебя ---
    if sender and sender.id == MY_TELEGRAM_ID and text.startswith("/v "):
        transcription = text[3:].strip() # Извлекаем текст после "/v "
        if transcription:
            logger.info(f"Detected voice transcription command in chat {chat_id}. Text: '{transcription[:30]}...'")
            # Добавляем транскрипцию в историю КАК БУДТО ОТ СОБЕСЕДНИКА
            update_chat_history(chat_id, "user", transcription)
            # Пытаемся удалить исходное /v сообщение
            try:
                # Для удаления бизнес-сообщений нужен business_connection_id
                if business_connection_id:
                     # Метод delete_message ожидает chat_id и message_id
                     # Но возможно, для бизнес сообщений нужен deleteBusinessMessages? Проверяем delete_message
                     # await context.bot.delete_message(chat_id=chat_id, message_id=message_id) # Стандартный метод
                     # Используем deleteBusinessMessages
                     deleted = await context.bot.delete_business_messages(
                          business_connection_id=business_connection_id,
                          chat_id=chat_id,
                          message_ids=[message_id]
                     )
                     if deleted:
                          logger.info(f"Successfully deleted business message {message_id} in chat {chat_id} via ConnID {business_connection_id}")
                     else:
                          logger.warning(f"delete_business_messages for message {message_id} in chat {chat_id} returned False.")
                else:
                     logger.warning(f"Cannot delete message {message_id} in chat {chat_id}: business_connection_id is missing.")

            except Forbidden:
                 logger.error(f"Failed to delete message {message_id} in chat {chat_id}: Bot lacks permission (Forbidden).")
            except BadRequest as e:
                 logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e} (BadRequest). Maybe wrong API call or message too old?")
            except Exception as e:
                 logger.error(f"Unexpected error deleting message {message_id} in chat {chat_id}: {e}", exc_info=True)

            # Важно: НЕ запускаем Gemini и дебаунс для этого сообщения
            return # Прерываем дальнейшую обработку этого /v сообщения
        else:
            logger.warning(f"Received empty /v command from {MY_TELEGRAM_ID} in chat {chat_id}. Ignoring.")
            # Можно отправить уведомление об ошибке
            # await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"Команда /v в чате {chat_id} была пустой.")
            return # Тоже прерываем

    # --- Остальная логика для обычных входящих/исходящих ---
    is_outgoing = sender and sender.id == MY_TELEGRAM_ID

    if is_outgoing:
        # (Логика для исходящих без изменений)
        logger.info(f"Processing OUTGOING business message in chat {chat_id} from {sender_id_str}")
        update_chat_history(chat_id, "model", text)
        if chat_id in debounce_tasks:
             logger.debug(f"Cancelling debounce task for chat {chat_id} due to outgoing message.")
             try: debounce_tasks[chat_id].cancel()
             except Exception as e: logger.error(f"Error cancelling task for chat {chat_id}: {e}")
             del debounce_tasks[chat_id]
        return

    if not sender: logger.warning(f"Incoming message in chat {chat_id} without sender info. Skipping."); return

    # (Логика для входящих без изменений)
    logger.info(f"Processing INCOMING business message from user {sender_id_str} in chat {chat_id} via ConnID: {business_connection_id}")
    sender_name = sender.first_name or f"User_{sender_id_str}"
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
            await process_chat_after_delay(chat_id, sender_name, sender_id_str, business_connection_id, context)
        except asyncio.CancelledError: logger.info(f"Debounce task for chat {chat_id} was cancelled.")
        except Exception as e: logger.error(f"Error in delayed processing for chat {chat_id}: {e}", exc_info=True)
    task = asyncio.create_task(delayed_processing())
    debounce_tasks[chat_id] = task
    logger.debug(f"Scheduled task {task.get_name()} for chat {chat_id}")


# --- Обработчик нажатий на кнопку (без изменений) ---
# ... (код button_handler) ...
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query;
    if not query: logger.warning("Received update without callback_query in button_handler"); return
    logger.info("--- button_handler triggered ---"); logger.debug(f"CallbackQuery Data: {query.data}")
    try: await query.answer()
    except Exception as e: logger.error(f"CRITICAL: Failed to answer callback query: {e}. Stopping handler."); return
    data = query.data;
    if not data or not data.startswith("send_"): logger.warning(f"Received unhandled callback_data: {data}"); return
    target_chat_id = None; business_connection_id_from_button = None; response_text_raw = None
    try:
        parts = data.split("_", 2); target_chat_id_str = parts[1]; target_chat_id = int(target_chat_id_str)
        business_connection_id_from_button = parts[2] if len(parts) > 2 else None
        logger.info(f"Button press: Attempting to send reply to chat {target_chat_id} using ConnID from button: {business_connection_id_from_button}")
        pending_data = pending_replies.pop(target_chat_id, None)
        if not pending_data: logger.warning(f"No pending reply found for chat {target_chat_id}."); return
        response_text_raw, stored_conn_id_from_pending = pending_data
        final_business_connection_id = business_connection_id_from_button or stored_conn_id_from_pending
        if business_connection_id_from_button and stored_conn_id_from_pending and business_connection_id_from_button != stored_conn_id_from_pending:
            logger.warning(f"Mismatch ConnID: button had {business_connection_id_from_button}, stored was {stored_conn_id_from_pending}. Using from button.")
        if not response_text_raw: logger.error(f"Stored raw response_text is None for chat {target_chat_id}!"); return
        logger.debug(f"Found RAW pending reply for chat {target_chat_id}: '{response_text_raw[:50]}...' using final ConnID: {final_business_connection_id}")
        message_parts = [part.strip() for part in response_text_raw.split("!NEWMSG!") if part.strip()]
        total_parts = len(message_parts); sent_count = 0; first_error = None
        if not message_parts: logger.warning(f"Raw response for chat {target_chat_id} resulted in no parts!"); return
        logger.info(f"Attempting to send {total_parts} message parts to chat {target_chat_id}")
        for i, part_text in enumerate(message_parts):
            logger.debug(f"Sending part {i+1}/{total_parts} to chat {target_chat_id}")
            try:
                sent_message = await context.bot.send_message(chat_id=target_chat_id, text=part_text, business_connection_id=final_business_connection_id)
                logger.info(f"Sent part {i+1}/{total_parts} (MsgID: {sent_message.message_id}) to chat {target_chat_id}")
                update_chat_history(target_chat_id, "model", part_text)
                sent_count += 1
                if total_parts > 1 and i < total_parts - 1: await asyncio.sleep(MESSAGE_SPLIT_DELAY)
            except Exception as e: logger.error(f"Failed to send part {i+1}/{total_parts}: {type(e).__name__}: {e}", exc_info=True); first_error = e; break
        final_text = query.message.text_html
        if first_error: error_text = f"<b>❌ Ошибка при отправке части {sent_count + 1}/{total_parts}:</b> {html.escape(str(first_error))}"; final_text += f"\n\n{error_text}"
        elif sent_count == total_parts: final_text += "\n\n<b>✅ Отправлено!</b>"; logger.info(f"Finished sending all parts for chat {target_chat_id}.")
        else: final_text += "\n\n<b>⚠️ Неизвестный результат.</b>"; logger.error(f"Unexpected state after sending parts for {target_chat_id}.")
        try: await query.edit_message_text(text=final_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception as edit_e: logger.error(f"Failed to edit original suggestion message: {edit_e}")
    except (ValueError, IndexError) as e: logger.error(f"Error parsing callback_data '{data}': {e}");
    except Exception as e: logger.error(f"Unexpected error in button_handler: {e}", exc_info=True);


# --- Функция post_init (без изменений) ---
# ... (код post_init) ...
async def post_init(application: Application):
    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    logger.info(f"Attempting to set webhook using: {webhook_full_url}")
    try:
        await application.bot.set_webhook(
            url=webhook_full_url,
            allowed_updates=[ # Убедимся, что все нужные типы здесь
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
# ... (код __main__) ...
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")
    parse_config_file(CONFIG_FILE)
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-pro", system_instruction=BASE_SYSTEM_PROMPT)
        logger.info(f"Gemini model '{gemini_model.model_name}' initialized successfully.")
    except Exception as e: logger.critical(f"CRITICAL: Failed to initialize Gemini: {e}", exc_info=True); exit()

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