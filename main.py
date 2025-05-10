import logging
import os
import asyncio
import json
from collections import deque
import google.generativeai as genai
import html
import time
import uuid
import psycopg
from datetime import datetime

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
# ... (код переменных) ...
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("google.generativeai").setLevel(logging.INFO)
logging.getLogger("psycopg").setLevel(logging.WARNING); logging.getLogger("psycopg.pool").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN"); WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443)); MY_TELEGRAM_ID_STR = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY"); CONFIG_FILE = "adp.txt"; DATABASE_URL = os.environ.get("DATABASE_URL")
CALENDAR_FILE = "calc.txt"
MAX_HISTORY_PER_CHAT = 700; DEBOUNCE_DELAY = 15; MY_NAME_FOR_HISTORY = "киткат"; MESSAGE_SPLIT_DELAY = 0.7
GEMINI_MODEL_NAME = "gemini-2.0-flash"
BASE_SYSTEM_PROMPT = ""; MY_CHARACTER_DESCRIPTION = ""; TOOLS_PROMPT = ""; CHAR_DESCRIPTIONS = {}
debounce_tasks = {}; pending_replies = {}; gemini_model = None; MY_TELEGRAM_ID = None
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit() # и т.д. ...
if not WEBHOOK_URL: logger.critical("CRITICAL: Missing WEBHOOK_URL"); exit()
if not WEBHOOK_URL.startswith("https://"): logger.critical(f"CRITICAL: WEBHOOK_URL must start with 'https://'"); exit()
if not MY_TELEGRAM_ID_STR: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID"); exit()
try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID_STR}') is not a valid integer."); exit()
if not GEMINI_API_KEY: logger.critical("CRITICAL: Missing GEMINI_API_KEY"); exit()
if not DATABASE_URL: logger.critical("CRITICAL: Missing DATABASE_URL for history storage."); exit()

# --- Функция парсинга конфигурационного файла (без изменений) ---
# ... (код parse_config_file) ...
def parse_config_file(filepath: str):
    global BASE_SYSTEM_PROMPT, MY_CHARACTER_DESCRIPTION, TOOLS_PROMPT, CHAR_DESCRIPTIONS; logger.info(f"Attempting to parse config file: {filepath}")
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
        BASE_SYSTEM_PROMPT = sections.get("SYSTEM_PROMPT", "").strip(); MY_CHARACTER_DESCRIPTION = sections.get("MC", "").strip(); TOOLS_PROMPT = sections.get("TOOLS", "").strip(); CHAR_DESCRIPTIONS = {}
        chars_content = sections.get("CHARS", "")
        if chars_content:
            for char_line in chars_content.splitlines():
                if '=' in char_line:
                    parts = char_line.split('=', 1); user_id_str = parts[0].strip(); description = parts[1].strip()
                    if user_id_str.isdigit() and description: CHAR_DESCRIPTIONS[user_id_str] = description
                    else: logger.warning(f"Skipping invalid line in CHARS section: {char_line}")
        if not BASE_SYSTEM_PROMPT: logger.error(f"CRITICAL: '!!SYSTEM_PROMPT' not found or empty in {filepath}.")
        if not TOOLS_PROMPT: logger.warning(f"'!!TOOLS' section not found or empty in {filepath}.")
        logger.info(f"Config loaded from {filepath}:"); logger.info(f"  SYSTEM_PROMPT: {'Loaded' if BASE_SYSTEM_PROMPT else 'MISSING/EMPTY'}"); logger.info(f"  MY_CHARACTER_DESCRIPTION: {'Loaded' if MY_CHARACTER_DESCRIPTION else 'MISSING/EMPTY'}"); logger.info(f"  TOOLS_PROMPT: {'Loaded' if TOOLS_PROMPT else 'MISSING/EMPTY'}"); logger.info(f"  Loaded {len(CHAR_DESCRIPTIONS)} character descriptions."); logger.debug(f"PARSED CHAR_DESCRIPTIONS: {CHAR_DESCRIPTIONS}")
    except FileNotFoundError: logger.critical(f"CRITICAL: Configuration file '{filepath}' not found."); exit()
    except Exception as e: logger.critical(f"CRITICAL: Error parsing config file '{filepath}': {e}", exc_info=True); exit()


# --- Функции работы с БД истории (без изменений) ---
# ... (код init_history_db, update_chat_history, get_formatted_history) ...
def init_history_db():
    sql_create_table = """CREATE TABLE IF NOT EXISTS chat_messages (id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, message_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, role TEXT NOT NULL, content TEXT NOT NULL);"""
    sql_create_index = """CREATE INDEX IF NOT EXISTS idx_chat_id_timestamp_desc ON chat_messages (chat_id, message_timestamp DESC);"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur: logger.debug("Executing CREATE TABLE IF NOT EXISTS..."); cur.execute(sql_create_table); logger.debug("Executing CREATE INDEX IF NOT EXISTS..."); cur.execute(sql_create_index); conn.commit()
        logger.info("PostgreSQL table 'chat_messages' and index checked/created.")
    except psycopg.Error as e: logger.critical(f"CRITICAL: Failed to initialize history DB table/index: {e}", exc_info=True); exit()
def update_chat_history(chat_id: int, role: str, text: str):
    if not text or not text.strip(): logger.warning(f"Attempted to add empty message to history for chat {chat_id}. Skipping."); return
    clean_text = text.strip(); sql_insert = "INSERT INTO chat_messages (chat_id, role, content) VALUES (%s, %s, %s);"
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur: cur.execute(sql_insert, (chat_id, role, clean_text)); conn.commit()
        logger.debug(f"Saved message to DB for chat {chat_id}. Role: {role}, Text: '{clean_text[:30]}...'")
    except psycopg.Error as e: logger.error(f"Failed to save message to history DB for chat {chat_id}: {e}")
def get_formatted_history(chat_id: int) -> list:
    sql_select = "SELECT role, content FROM chat_messages WHERE chat_id = %s ORDER BY message_timestamp DESC LIMIT %s;"
    gemini_history = []
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur: cur.execute(sql_select, (chat_id, MAX_HISTORY_PER_CHAT)); db_rows = cur.fetchall()
        for row in reversed(db_rows): role, content = row; gemini_history.append({"role": role, "parts": [{"text": content}]})
        logger.debug(f"Retrieved {len(gemini_history)} history entries from DB for chat {chat_id}.")
        return gemini_history
    except psycopg.Error as e: logger.error(f"Failed to retrieve history from DB for chat {chat_id}: {e}"); return []


# --- ИЗМЕНЕННАЯ Функция для вызова Gemini API ---
async def generate_gemini_response(dynamic_context_parts: list, chat_history: list) -> str | None:
    global gemini_model
    if not gemini_model: logger.error("Gemini model not initialized!"); return None

    gemini_contents = []
    context_block_text = ""

    # --- ДОБАВЛЕНО: Текущая дата и время в контекст ---
    now = datetime.now()
    current_datetime_str = now.strftime("%d %B %Y года, %A, время: %H:%M") # Формат "07 мая 2025 года, среда, время: 09:30"
    # Пробуем установить русскую локаль для названий месяцев и дней недели
    try:
        # На Render может не быть нужной локали, это просто попытка
        # Для Linux часто 'ru_RU.UTF-8', для Windows 'russian'
        # locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8') # Убрал import locale, т.к. это не очень надежно на серверах
        # Вместо этого можно передавать названия месяцев/дней явно или использовать библиотеку для локализации
        # Пока оставим формат, который дает strftime по умолчанию, он будет на английском, если локаль не русская
        pass # locale.setlocale не очень надежен на серверах без доп.настройки
    except Exception: # locale.Error
        logger.warning("Could not set Russian locale for datetime formatting, using default.")
    context_block_text += f"Текущая дата и время: {current_datetime_str}.\n\n"
    # --- Конец добавления даты/времени ---

    if MY_CHARACTER_DESCRIPTION:
        context_block_text += f"Немного информации обо мне ({MY_NAME_FOR_HISTORY}):\n{MY_CHARACTER_DESCRIPTION}\n\n"
    for part in dynamic_context_parts: # Это описание собеседника или инструкции про календарь
        context_block_text += f"{part}\n\n"

    if context_block_text.strip():
        gemini_contents.append({"role": "model", "parts": [{"text": context_block_text.strip()}]})
        logger.debug(f"Prepended context block to Gemini contents.")

    gemini_contents.extend(chat_history)
    if not gemini_contents: logger.warning("Cannot generate response for empty Gemini contents."); return None

    logger.info(f"Sending request to Gemini with {len(gemini_contents)} content entries.")
    # logger.debug(f"Full Gemini Payload (contents): {json.dumps(gemini_contents, ensure_ascii=False, indent=2)}")

    try:
        response = await gemini_model.generate_content_async(
            contents=gemini_contents,
            generation_config=genai.types.GenerationConfig(temperature=0.7),
            safety_settings={'HARM_CATEGORY_HARASSMENT': 'block_none', 'HARM_CATEGORY_HATE_SPEECH': 'block_none',
                             'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none', 'HARM_CATEGORY_DANGEROUS_CONTENT': 'block_none'}
        )
        # ... (обработка ответа Gemini без изменений) ...
        if response and response.parts:
            generated_text = "".join(part.text for part in response.parts).strip()
            if generated_text and "cannot fulfill" not in generated_text.lower() and "unable to process" not in generated_text.lower():
                 logger.info(f"Received response from Gemini: '{generated_text[:50]}...'"); return generated_text
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
    sender_id_str: str,
    business_connection_id: str | None,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.info(f"Debounce timer expired for chat {chat_id} with sender {sender_id_str}. Processing...")
    current_history = get_formatted_history(chat_id)
    dynamic_prompt_parts_for_gemini = [] # Для описаний и инструкций календаря
    interlocutor_description = CHAR_DESCRIPTIONS.get(sender_id_str)
    if interlocutor_description:
        logger.info(f"FOUND description for sender {sender_id_str}")
        dynamic_prompt_parts_for_gemini.append(f"Информация о текущем собеседнике ({sender_name}, ID: {sender_id_str}):\n{interlocutor_description}")
    else: logger.warning(f"Description NOT FOUND for sender ID {sender_id_str}")

    # --- ПЕРВЫЙ вызов Gemini (с TOOLS) ---
    first_call_context = list(dynamic_prompt_parts_for_gemini) # Копируем, чтобы не менять исходный
    if TOOLS_PROMPT:
        first_call_context.append(f"Инструкции по инструментам:\n{TOOLS_PROMPT}") # Добавляем TOOLS

    logger.debug(f"Attempting initial Gemini call with tools context: {first_call_context}")
    gemini_response_raw = await generate_gemini_response(first_call_context, current_history)

    # --- Проверяем на !fetchcalc ---
    if gemini_response_raw == "!fetchcalc":
        logger.info(f"Received '!fetchcalc' signal for chat {chat_id}. Fetching calendar info...")
        calendar_content = "Информация из календаря недоступна."; # ... (логика чтения CALENDAR_FILE) ...
        try:
            with open(CALENDAR_FILE, 'r', encoding='utf-8') as f: calendar_content = f.read().strip()
            if not calendar_content: logger.warning(f"Calendar file '{CALENDAR_FILE}' is empty."); calendar_content = "Файл календаря пуст."
            else: logger.info(f"Successfully read calendar file '{CALENDAR_FILE}'.")
        except FileNotFoundError: logger.error(f"Calendar file '{CALENDAR_FILE}' not found!")
        except Exception as e: logger.error(f"Error reading calendar file '{CALENDAR_FILE}': {e}")

        now = datetime.now()
        current_datetime_str_for_calendar = now.strftime("%d %B %Y года, %A, %H:%M")

        # --- Формируем ВТОРОЙ промпт (с календарем, БЕЗ TOOLS) ---
        calendar_prompt_context = list(dynamic_prompt_parts_for_gemini) # Описания персонажей
        calendar_instruction = (
            f"Получен запрос на использование календаря (предыдущий ответ был '!fetchcalc').\n"
            f"Текущая дата и время: {current_datetime_str_for_calendar}\n"
            f"Предоставленное расписание:\n------\n{calendar_content}\n------\n"
            f"Пожалуйста, ответь на последний вопрос пользователя, используя эту информацию и следуя основной инструкции (без использования '!fetchcalc' снова)."
        )
        calendar_prompt_context.append(calendar_instruction) # Добавляем инструкцию по календарю

        logger.debug(f"Attempting second Gemini call with calendar context: {calendar_prompt_context}")
        gemini_response_raw = await generate_gemini_response(calendar_prompt_context, current_history)
        if not gemini_response_raw or gemini_response_raw == "!fetchcalc":
             logger.error(f"Second Gemini call (with calendar) failed or returned !fetchcalc for chat {chat_id}.")
             # Можно отправить уведомление себе об ошибке
             # await context.bot.send_message(MY_TELEGRAM_ID, f"⚠️ Ошибка: Gemini вернул !fetchcalc/ошибку после календаря для чата {chat_id}")
             gemini_response_raw = None # Сбрасываем, чтобы не было кнопки

    # --- Обработка финального ответа ---
    if gemini_response_raw:
        reply_uuid = str(uuid.uuid4())
        pending_replies[reply_uuid] = (gemini_response_raw, business_connection_id, chat_id)
        logger.debug(f"Stored final pending reply with UUID {reply_uuid}")

        # --- ИЗМЕНЕНО: Форматирование превью для дробленых сообщений ---
        parts_for_preview = gemini_response_raw.split("!NEWMSG!")
        formatted_preview_parts = []
        for part in parts_for_preview:
            stripped_part = part.strip()
            if stripped_part: # Добавляем только непустые части
                formatted_preview_parts.append(f"<code>{html.escape(stripped_part)}</code>")
        
        # Соединяем части с НЕэкранированным разделителем
        preview_text_html = "🔚<br><br>".join(formatted_preview_parts) # Используем <br> для HTML переноса

        try:
            safe_sender_name = html.escape(sender_name)
            reply_title_html = f"🤖 <b>Предложенный ответ для чата {html.escape(str(chat_id))}</b> (<i>{safe_sender_name}</i>):\n──────────────────\n"
            full_reply_html = reply_title_html + preview_text_html

            callback_data = f"send_{reply_uuid}";
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить в чат", callback_data=callback_data)]])
            await context.bot.send_message(
                chat_id=MY_TELEGRAM_ID, text=full_reply_html, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
            logger.info(f"Sent suggestion preview (UUID: {reply_uuid}) for target_chat {chat_id} to {MY_TELEGRAM_ID}")
        except TelegramError as e:
            logger.error(f"Failed to send suggestion preview (HTML) to {MY_TELEGRAM_ID}: {e}")
            # ... (fallback на простой текст, если HTML не удался) ...
            try: # Fallback на простой текст
                 plain_preview_text = gemini_response_raw.replace("!NEWMSG!", "\n\n🔚\n\n")
                 reply_text_plain = (f"🤖 Предложенный ответ для чата {chat_id} ({sender_name}):\n"
                                   f"──────────────────\n{plain_preview_text}\n(Не удалось добавить кнопку отправки)")
                 await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=reply_text_plain)
            except Exception as e2: logger.error(f"Failed to send suggestion preview (plain fallback): {e2}")
    else:
        logger.warning(f"No response generated by Gemini for chat {chat_id} after debounce (final).")

    if chat_id in debounce_tasks: del debounce_tasks[chat_id]; logger.debug(f"Removed completed debounce task for chat {chat_id}")


# --- Основной обработчик бизнес-сообщений (без изменений) ---
# ... (код handle_business_update) ...
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # logger.info(f"--- Received Update ---:\n{json.dumps(update.to_dict(), indent=2, ensure_ascii=False)}") # Раскомментируй для отладки
    message_to_process = None; business_connection_id = None
    if update.business_message: message_to_process = update.business_message; business_connection_id = message_to_process.business_connection_id; logger.info(f"--- Received Business Message (ID: {message_to_process.message_id}, ConnID: {business_connection_id}) ---")
    elif update.edited_business_message: message_to_process = update.edited_business_message; business_connection_id = getattr(message_to_process, 'business_connection_id', None); logger.info(f"--- Received Edited Business Message (ID: {message_to_process.message_id}, ConnID: {business_connection_id}) ---")
    else: return

    chat = message_to_process.chat; sender = message_to_process.from_user; text = message_to_process.text
    if not text: logger.debug(f"Ignoring non-text business message in chat {chat.id}"); return

    chat_id = chat.id; sender_id_str = str(sender.id) if sender else None; sender_name = "Unknown"
    if sender: sender_name = sender.first_name or f"User_{sender_id_str}"

    if sender and sender.id == MY_TELEGRAM_ID and text.startswith("/v "): # Обработка /v
        transcription = text[3:].strip()
        if transcription:
            logger.info(f"Processing /v command in chat {chat_id}. Transcription: '{transcription[:30]}...'")
            update_chat_history(chat_id, "user", transcription)
            logger.info(f"Message with /v command in chat {chat_id} was not deleted (deletion disabled).")
            fictional_sender_name_for_suggestion = chat.first_name or f"Chat_{chat_id}"; fictional_sender_id_for_description = str(chat_id)
            async def delayed_processing_for_v_command():
                try:
                    await asyncio.sleep(DEBOUNCE_DELAY)
                    logger.debug(f"Debounce for /v in chat {chat_id} finished. Starting processing.")
                    await process_chat_after_delay(chat_id, fictional_sender_name_for_suggestion, fictional_sender_id_for_description, business_connection_id, context)
                except asyncio.CancelledError: logger.info(f"Debounce task for /v in chat {chat_id} was cancelled.")
                except Exception as e: logger.error(f"Error in delayed /v processing for chat {chat_id}: {e}", exc_info=True)
            if chat_id in debounce_tasks:
                try: debounce_tasks[chat_id].cancel()
                except Exception: pass
            task = asyncio.create_task(delayed_processing_for_v_command()); debounce_tasks[chat_id] = task
            logger.info(f"Scheduled response generation for chat {chat_id} after /v command.")
        else: logger.warning(f"Received empty /v command from {MY_TELEGRAM_ID} in chat {chat_id}. Ignoring.")
        return

    is_outgoing = sender and sender.id == MY_TELEGRAM_ID
    if is_outgoing:
        logger.info(f"Processing OUTGOING business message in chat {chat_id} from {sender_id_str}")
        update_chat_history(chat_id, "model", text)
        if chat_id in debounce_tasks:
             logger.debug(f"Cancelling debounce task for chat {chat_id} due to outgoing message.")
             try: debounce_tasks[chat_id].cancel()
             except Exception as e: logger.error(f"Error cancelling task for chat {chat_id}: {e}")
             del debounce_tasks[chat_id]
        return

    if not sender: logger.warning(f"Incoming message in chat {chat_id} without sender info. Skipping."); return

    logger.info(f"Processing INCOMING business message from user {sender_id_str} in chat {chat_id} via ConnID: {business_connection_id}")
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
    task = asyncio.create_task(delayed_processing()); debounce_tasks[chat_id] = task
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
    reply_uuid = None; response_text_raw = None; final_business_connection_id = None; target_chat_id_for_send = None
    try:
        reply_uuid = data.split("_", 1)[1]
        logger.info(f"Button press: Attempting to process reply with UUID: {reply_uuid}")
        pending_data = pending_replies.pop(reply_uuid, None)
        if not pending_data: logger.warning(f"No pending reply found for UUID {reply_uuid}."); await query.edit_message_text(text=query.message.text_html + "\n\n<b>⚠️ Ошибка:</b> Ответ не найден.", parse_mode=ParseMode.HTML, reply_markup=None); return
        response_text_raw, final_business_connection_id, target_chat_id_for_send = pending_data
        if not response_text_raw: logger.error(f"Stored raw response_text is None for UUID {reply_uuid}!"); await query.edit_message_text(text=query.message.text_html + "\n\n<b>⚠️ Ошибка:</b> Пустой текст.", parse_mode=ParseMode.HTML, reply_markup=None); return
        logger.debug(f"Found RAW pending reply for UUID {reply_uuid} (target chat {target_chat_id_for_send}): '{response_text_raw[:50]}...' using ConnID: {final_business_connection_id}")
        message_parts = [part.strip() for part in response_text_raw.split("!NEWMSG!") if part.strip()]
        total_parts = len(message_parts); sent_count = 0; first_error = None
        if not message_parts: logger.warning(f"Raw response for UUID {reply_uuid} resulted in no parts!"); await query.edit_message_text(text=query.message.text_html + "\n\n<b>⚠️ Ошибка:</b> Пустой ответ.", parse_mode=ParseMode.HTML, reply_markup=None); return
        logger.info(f"Attempting to send {total_parts} message parts to chat {target_chat_id_for_send}")
        for i, part_text in enumerate(message_parts):
            logger.debug(f"Sending part {i+1}/{total_parts} to chat {target_chat_id_for_send}")
            try:
                sent_message = await context.bot.send_message(chat_id=target_chat_id_for_send, text=part_text, business_connection_id=final_business_connection_id)
                logger.info(f"Sent part {i+1}/{total_parts} (MsgID: {sent_message.message_id}) to chat {target_chat_id_for_send}")
                update_chat_history(target_chat_id_for_send, "model", part_text)
                sent_count += 1
                if total_parts > 1 and i < total_parts - 1: await asyncio.sleep(MESSAGE_SPLIT_DELAY)
            except Exception as e: logger.error(f"Failed to send part {i+1}/{total_parts}: {type(e).__name__}: {e}", exc_info=True); first_error = e; break
        final_text = query.message.text_html
        if first_error: error_text = f"<b>❌ Ошибка при отправке части {sent_count + 1}/{total_parts}:</b> {html.escape(str(first_error))}"; final_text += f"\n\n{error_text}"
        elif sent_count == total_parts: final_text += "\n\n<b>✅ Отправлено!</b>"; logger.info(f"Finished sending all parts for chat {target_chat_id_for_send}.")
        else: final_text += "\n\n<b>⚠️ Неизвестный результат.</b>"; logger.error(f"Unexpected state after sending parts for {target_chat_id_for_send}.")
        try: await query.edit_message_text(text=final_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception as edit_e: logger.error(f"Failed to edit original suggestion message: {edit_e}")
    except (ValueError, IndexError) as e: logger.error(f"Error parsing callback_data '{data}' or processing reply for UUID {reply_uuid}: {e}");
    except Exception as e: logger.error(f"Unexpected error in button_handler (UUID {reply_uuid}): {e}", exc_info=True);

# --- Функция post_init (без изменений) ---
# ... (код post_init) ...
async def post_init(application: Application):
    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    logger.info(f"Attempting to set webhook using: {webhook_full_url}")
    try:
        await application.bot.set_webhook( url=webhook_full_url,
            allowed_updates=[ "message", "edited_message", "channel_post", "edited_channel_post",
                "business_connection", "business_message", "edited_business_message",
                "deleted_business_messages", "my_chat_member", "chat_member", "callback_query"],
            drop_pending_updates=True )
        webhook_info = await application.bot.get_webhook_info(); logger.info(f"Webhook info after setting: {webhook_info}")
        if webhook_info.url == webhook_full_url: logger.info("Webhook successfully set!")
        else: logger.warning(f"Webhook URL reported differ: {webhook_info.url}")
    except Exception as e: logger.error(f"Error setting webhook: {e}", exc_info=True)


# --- Основная точка входа (без изменений) ---
# ... (код __main__) ...
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")
    parse_config_file(CONFIG_FILE); init_history_db()
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel(GEMINI_MODEL_NAME, system_instruction=BASE_SYSTEM_PROMPT)
        logger.info(f"Gemini model '{gemini_model.model_name}' initialized successfully.")
    except Exception as e: logger.critical(f"CRITICAL: Failed to initialize Gemini: {e}", exc_info=True); exit()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_update))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, handle_business_update))
    application.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Application built. Starting webhook listener...")
    try:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        asyncio.run(application.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=webhook_full_url))
    except ValueError as e: logger.critical(f"CRITICAL ERROR asyncio.run: {e}", exc_info=True)
    except Exception as e: logger.critical(f"CRITICAL ERROR Webhook server: {e}", exc_info=True)
    finally: logger.info("Webhook server shut down.")