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
from datetime import datetime, timezone
import pytz
import re # <--- ДОБАВЛЕНО: для регулярных выражений

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
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
# ... (все переменные как были) ...
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("google.generativeai").setLevel(logging.INFO)
logging.getLogger("psycopg").setLevel(logging.WARNING); logging.getLogger("psycopg.pool").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN"); WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443)); MY_TELEGRAM_ID_STR = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY"); CONFIG_FILE = "adp.txt"
DATABASE_URL = os.environ.get("DATABASE_URL"); CALENDAR_FILE = "calc.txt"
MAX_HISTORY_PER_CHAT = 700; DEBOUNCE_DELAY = 1; MY_NAME_FOR_HISTORY = "киткат"; MESSAGE_SPLIT_DELAY = 0.7
GEMINI_MODEL_NAME = "gemini-2.0-flash"
BASE_SYSTEM_PROMPT = ""; MY_CHARACTER_DESCRIPTION = ""; TOOLS_PROMPT = ""; CHAR_DESCRIPTIONS = {}
debounce_tasks = {}; pending_replies = {}; gemini_model = None; MY_TELEGRAM_ID = None
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
# ... (остальные проверки) ...
if not DATABASE_URL: logger.critical("CRITICAL: Missing DATABASE_URL"); exit()
if MY_TELEGRAM_ID_STR:
    try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
    except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID_STR}') is not valid."); exit()
else: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID."); exit()


# --- НОВАЯ Функция для "очистки" ответа Gemini ---
def sanitize_gemini_response(text: str) -> str:
    """Удаляет служебные meta_ теги из ответа Gemini."""
    if not text:
        return ""
    # Удаляем паттерны вида "meta_ключ: значение_в_скобках (если есть) и_даже_с_кавычками «...»"
    # Это регулярное выражение ищет "meta_" за которым следуют не-пробельные символы (ключ),
    # затем двоеточие, а затем любые символы до конца строки ИЛИ до следующего meta_ (если они в одной строке, хотя не должны)
    # или до явного текстового ответа.
    # Более простой подход: удалить все, что похоже на meta_reply_to, meta_forwarded_from, meta_content_type и т.д.
    # до первого "нормального" слова или до конца строки, если после meta_ ничего нет.

    # Паттерн для удаления: "meta_" + ключ + ":" + все до конца строки или до осмысленного текста.
    # Это может быть сложно сделать идеально одной регуляркой для всех случаев.
    # Попробуем удалить известные нам маркеры и их типичные продолжения.
    
    # Сначала удаляем общие паттерны meta_...
    # Этот паттерн удалит "meta_что_угодно: все до конца строки" или "meta_что_угодно: все до (текст_ответа..."
    # или "meta_что_угодно: все до «текст...»"
    # Важно: это может быть слишком агрессивно, если Gemini генерирует что-то похожее.
    
    # Паттерн для удаления meta-тегов и всего, что за ними до фактического ответа.
    # Ищем "meta_" + ключ + двоеточие + опциональный пробел + (опционально текст в скобках) + опциональный пробел
    # И заменяем это на пустую строку.
    
    # Более простой и, возможно, более безопасный подход: удаляем конкретные известные префиксы.
    # Этот паттерн пытается удалить строку, начинающуюся с meta_ и идущую до предполагаемого начала ответа
    # или до конца, если ответа нет.
    
    # 1. Удаляем "meta_reply_to: Имя (текст_ответа_начинался_с: «...»)" и следующий за ним пробел
    text = re.sub(r"meta_reply_to:\s*[\w\d_()«»\s.:-]+?\s+", "", text, flags=re.IGNORECASE)
    # 2. Удаляем другие meta_ теги, если они в начале строки ответа
    text = re.sub(r"^\s*meta_[\w_]+:.*?(?=\w{3,}|$)", "", text, flags=re.IGNORECASE).strip() # Удаляет meta_ в начале до первого слова из 3+ букв или до конца
    text = re.sub(r"meta_[\w_]+:\s*[\w\d_().,;:\"'«»<>?!#@$%^&*\-+=/\s]+?(?=\s*\w{2,}|$)", "", text).strip()


    # Еще одна попытка удалить паттерны, которые могут остаться:
    # Удаляем "meta_ключ: значение" и пробелы вокруг
    text = re.sub(r"meta_[\w-]+:\s*[^ ]+\s*", "", text).strip()
    # Удаляем одиноко стоящие meta_ключи, если они как-то просочились
    text = re.sub(r"\s*meta_[\w-]+\s*", " ", text).strip()
    
    # Простая очистка, если Gemini вернул только meta_теги (маловероятно, но все же)
    if text.lower().startswith("meta_"):
        logger.warning(f"Sanitizer: Gemini response started with meta_ and might be fully removed. Original: '{text[:100]}'")
        # Пытаемся найти первое "осмысленное" слово после всех meta
        match = re.search(r"(?:meta_[\w\s:().,«»\"'-]+)+(.+)", text, re.IGNORECASE)
        if match and match.group(1).strip():
            text = match.group(1).strip()
            logger.info(f"Sanitizer: Extracted text after meta_: '{text[:100]}'")
        else:
            return "" # Если ничего кроме meta_ не осталось

    # Убираем лишние пробелы, которые могли остаться после замен
    text = re.sub(r"\s{2,}", " ", text).strip()
    
    logger.debug(f"Sanitized Gemini response: '{text[:100]}...'")
    return text

# --- Функция получения саратовского времени (без изменений) ---
# ... (код get_saratov_datetime_info) ...
def get_saratov_datetime_info():
    try:
        utc_now = datetime.now(timezone.utc); saratov_tz = pytz.timezone('Europe/Saratov'); saratov_now = utc_now.astimezone(saratov_tz)
        days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]; day_of_week_ru = days_ru[saratov_now.weekday()]
        return saratov_now.strftime(f"%Y-%m-%d %H:%M ({day_of_week_ru})")
    except Exception as e: logger.error(f"Error getting Saratov datetime: {e}"); return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC (Error getting local time)")

# --- Функция парсинга конфигурационного файла (без изменений) ---
# ... (код parse_config_file) ...
def parse_config_file(filepath: str):
    global BASE_SYSTEM_PROMPT, MY_CHARACTER_DESCRIPTION,TOOLS_PROMPT, CHAR_DESCRIPTIONS; logger.info(f"Attempting to parse config file: {filepath}")
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
        BASE_SYSTEM_PROMPT = sections.get("SYSTEM_PROMPT", "").strip(); MY_CHARACTER_DESCRIPTION = sections.get("MC", "").strip()
        TOOLS_PROMPT = sections.get("TOOLS", "").strip(); CHAR_DESCRIPTIONS = {}
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
    sql_create_table = "CREATE TABLE IF NOT EXISTS chat_messages (id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, message_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, role TEXT NOT NULL, content TEXT NOT NULL);"
    sql_create_index = "CREATE INDEX IF NOT EXISTS idx_chat_id_timestamp_desc ON chat_messages (chat_id, message_timestamp DESC);"
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
        logger.debug(f"Saved message to DB for chat {chat_id}. Role: {role}, Text: '{clean_text[:50]}...'")
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


# --- Функция enrich_message_for_history (без изменений, мы ее уже обновили) ---
# ... (код enrich_message_for_history) ...
def enrich_message_for_history(message: Message) -> str:
    meta_parts = [] ; text_parts = []   
    if message.reply_to_message:
        reply_to = message.reply_to_message; reply_sender_display_name = "собеседнику"
        if reply_to.from_user: reply_sender_display_name = reply_to.from_user.first_name or reply_to.from_user.full_name or f"User_{reply_to.from_user.id}"
        elif reply_to.chat and reply_to.chat.title: reply_sender_display_name = f"сообщению_из_чата_{reply_to.chat.title.replace(' ', '_')}"
        replied_message_snippet = (reply_to.text or reply_to.caption or "медиа_без_текста")[:30].replace('\n', ' ').replace(':', ';')
        meta_parts.append(f"meta_reply_to: {reply_sender_display_name} (текст_ответа_начинался_с: «{replied_message_snippet}»)")
    fwd_info_str = ""
    forward_from_user = getattr(message, 'forward_from', None); forward_from_chat_obj = getattr(message, 'forward_from_chat', None)
    forward_sender_name_attr = getattr(message, 'forward_sender_name', None)
    if forward_from_user: fwd_info_str = f"user_{forward_from_user.id}_({forward_from_user.first_name or forward_from_user.full_name or 'UnknownName'})"
    elif forward_from_chat_obj:
        fwd_info_str = f"chat_{forward_from_chat_obj.id}_({forward_from_chat_obj.title or 'UnknownChatTitle'})"
        forward_from_message_id_attr = getattr(message, 'forward_from_message_id', None)
        if forward_from_message_id_attr: fwd_info_str += f"_msg_id_{forward_from_message_id_attr}"
    elif forward_sender_name_attr: fwd_info_str = f"hidden_sender_({forward_sender_name_attr.replace(' ', '_')})"
    if fwd_info_str: meta_parts.append(f"meta_forwarded_from: {fwd_info_str}")
    media_type_str = None; media_details_str = None
    if message.photo: media_type_str = "photo"
    elif message.video: media_type_str = "video"
    elif message.audio: media_type_str = "audio"; media_details_str = getattr(message.audio, 'title', None) or getattr(message.audio, 'file_name', None)
    elif message.voice: media_type_str = "voice_message"
    elif message.document: media_type_str = "document"; media_details_str = getattr(message.document, 'file_name', None)
    elif message.sticker: media_type_str = "sticker"; media_details_str = getattr(message.sticker, 'emoji', None)
    if media_type_str:
        meta_parts.append(f"meta_content_type: {media_type_str}")
        if media_details_str: meta_parts.append(f"meta_media_details: {media_details_str.replace(':',';').replace(' ', '_')[:50]}")
    if message.caption: meta_parts.append(f"meta_caption: true"); text_parts.append(message.caption.strip())
    elif message.text: text_parts.append(message.text.strip())
    final_parts_for_history = []
    if meta_parts: final_parts_for_history.append(" ".join(meta_parts))
    if text_parts: final_parts_for_history.append(" ".join(text_parts))
    result_text = " ".join(final_parts_for_history).strip()
    if not result_text:
        if media_type_str: return f"meta_content_type: {media_type_str} (без_текста_или_подписи)"
        return "meta_info: [пустое_или_нераспознанное_сообщение]"
    return result_text

# --- Функция для вызова Gemini API (без изменений) ---
# ... (код generate_gemini_response) ...
async def generate_gemini_response(contents: list) -> str | None:
    global gemini_model;
    if not gemini_model: logger.error("Gemini model not initialized!"); return None
    if not contents: logger.warning("Cannot generate response for empty contents list."); return None
    logger.info(f"Sending request to Gemini with {len(contents)} content entries.")
    try:
        response = await gemini_model.generate_content_async(contents=contents, generation_config=genai.types.GenerationConfig(temperature=0.7),
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
    saratov_time_str = get_saratov_datetime_info()

    initial_contents = []
    context_block_text = ""
    if MY_CHARACTER_DESCRIPTION: context_block_text += f"Немного информации обо мне ({MY_NAME_FOR_HISTORY}):\n{MY_CHARACTER_DESCRIPTION}\n\n"
    interlocutor_description = CHAR_DESCRIPTIONS.get(sender_id_str)
    if interlocutor_description: context_block_text += f"Информация о текущем собеседнике ({sender_name}, ID: {sender_id_str}):\n{interlocutor_description}\n\n"
    context_block_text += f"Текущее время в Саратове (где находится Киткат): {saratov_time_str}\n\n"
    if TOOLS_PROMPT: context_block_text += f"Инструкции по инструментам:\n{TOOLS_PROMPT}\n\n"
    if context_block_text.strip(): initial_contents.append({"role": "model", "parts": [{"text": context_block_text.strip()}]})
    initial_contents.extend(current_history)

    logger.debug("Attempting initial Gemini call...")
    gemini_response_from_api = await generate_gemini_response(initial_contents) # Ответ от API

    # --- ОЧИСТКА ответа перед дальнейшей обработкой ---
    gemini_response_raw = sanitize_gemini_response(gemini_response_from_api) if gemini_response_from_api else None
    if gemini_response_from_api and not gemini_response_raw:
        logger.warning(f"Sanitizer possibly removed the entire Gemini response. Original: '{gemini_response_from_api[:100]}'")


    if gemini_response_raw == "!fetchcalc":
        # ... (логика для !fetchcalc) ...
        logger.info(f"Received '!fetchcalc' signal for chat {chat_id}. Fetching calendar info...")
        calendar_content = "Информация из календаря недоступна."
        try:
            with open(CALENDAR_FILE, 'r', encoding='utf-8') as f: calendar_content = f.read().strip()
            if not calendar_content: logger.warning(f"Calendar file '{CALENDAR_FILE}' is empty."); calendar_content = "Файл календаря пуст."
            else: logger.info(f"Successfully read calendar file '{CALENDAR_FILE}'.")
        except FileNotFoundError: logger.error(f"Calendar file '{CALENDAR_FILE}' not found!")
        except Exception as e: logger.error(f"Error reading calendar file '{CALENDAR_FILE}': {e}")
        calendar_prompt_contents = []; calendar_intro = (f"Для ответа на предыдущий вопрос пользователя требуется информация из его расписания.\n"
                          f"Текущая дата и время в Саратове (где находится пользователь Киткат): {saratov_time_str}\n"
                          f"Вот предоставленное пользователем расписание (содержимое файла {CALENDAR_FILE}):\n------\n{calendar_content}\n------\n"
                          f"Пожалуйста, проанализируй это расписание и текущее время, и ответь на последний вопрос пользователя, следуя основной инструкции и стилю Китката.")
        context_block_text_for_calendar = ""
        if MY_CHARACTER_DESCRIPTION: context_block_text_for_calendar += f"Напомню информацию обо мне ({MY_NAME_FOR_HISTORY}):\n{MY_CHARACTER_DESCRIPTION}\n\n"
        if interlocutor_description: context_block_text_for_calendar += f"Напомню информацию о собеседнике ({sender_name}, ID: {sender_id_str}):\n{interlocutor_description}\n\n"
        context_block_text_for_calendar += f"Текущее время в Саратове: {saratov_time_str}\n\n"
        if context_block_text_for_calendar.strip(): calendar_prompt_contents.append({"role": "model", "parts": [{"text": context_block_text_for_calendar.strip()}]})
        calendar_prompt_contents.append({"role": "user", "parts": [{"text": calendar_intro}]}); calendar_prompt_contents.extend(current_history)
        logger.debug("Attempting second Gemini call with calendar info...")
        gemini_response_from_api_calendar = await generate_gemini_response(calendar_prompt_contents)
        # --- ОЧИСТКА второго ответа ---
        gemini_response_raw = sanitize_gemini_response(gemini_response_from_api_calendar) if gemini_response_from_api_calendar else None
        if gemini_response_from_api_calendar and not gemini_response_raw:
             logger.warning(f"Sanitizer possibly removed the entire Gemini response (calendar). Original: '{gemini_response_from_api_calendar[:100]}'")
        if not gemini_response_raw: logger.error(f"Second Gemini call (with calendar) failed or was sanitized to empty for chat {chat_id}.")


    if gemini_response_raw and gemini_response_raw != "!fetchcalc":
        reply_uuid = str(uuid.uuid4())
        pending_replies[reply_uuid] = (gemini_response_raw, business_connection_id, chat_id) # Сохраняем УЖЕ ОЧИЩЕННЫЙ
        logger.debug(f"Stored final SANITIZED pending reply with UUID {reply_uuid}")
        preview_text = gemini_response_raw.replace("!NEWMSG!", "\n\n🔚\n\n")
        # ... (отправка превью как было) ...
        try:
            logger.info(f"Attempting to send suggestion preview to MY_TELEGRAM_ID: {MY_TELEGRAM_ID} (type: {type(MY_TELEGRAM_ID)})")
            if MY_TELEGRAM_ID is None: logger.error("CRITICAL: MY_TELEGRAM_ID is None before sending preview! Cannot send."); return
            safe_sender_name = html.escape(sender_name); escaped_preview_text = html.escape(preview_text)
            reply_text_html = (f"🤖 <b>Предложенный ответ для чата {html.escape(str(chat_id))}</b> (<i>{safe_sender_name}</i>):\n"
                               f"──────────────────\n<code>{escaped_preview_text}</code>")
            callback_data = f"send_{reply_uuid}"; keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить в чат", callback_data=callback_data)]])
            await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=reply_text_html, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            logger.info(f"Sent suggestion preview (UUID: {reply_uuid}) for target_chat {chat_id} to {MY_TELEGRAM_ID}")
        except TelegramError as e: logger.error(f"Failed to send suggestion preview (HTML) to MY_TELEGRAM_ID {MY_TELEGRAM_ID}: {e}", exc_info=True);
    elif gemini_response_raw == "!fetchcalc":
        logger.error(f"Gemini returned '!fetchcalc' even after providing calendar data for chat {chat_id} (or it was sanitized to it).")
    else:
        logger.warning(f"No response generated or response was sanitized to empty by Gemini for chat {chat_id} after debounce (final).")

    if chat_id in debounce_tasks: del debounce_tasks[chat_id]; logger.debug(f"Removed completed debounce task for chat {chat_id}")


# --- Остальные функции (handle_business_update, button_handler, post_init, __main__) БЕЗ ИЗМЕНЕНИЙ ---
# ... (код handle_business_update) ...
async def handle_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_to_process = update.business_message or update.edited_business_message
    if not message_to_process: return
    enriched_history_text = enrich_message_for_history(message_to_process)
    logger.debug(f"Chat {message_to_process.chat.id} | Enriched: '{enriched_history_text[:100]}...'")
    chat = message_to_process.chat; sender = message_to_process.from_user
    business_connection_id = getattr(message_to_process, 'business_connection_id', None)
    original_text_from_update = message_to_process.text or ""
    chat_id = chat.id; sender_id_str = str(sender.id) if sender else None; sender_name = "Unknown"
    if sender: sender_name = sender.first_name or sender.full_name or f"User_{sender_id_str}"
    if sender and sender.id == MY_TELEGRAM_ID and original_text_from_update.startswith("/v "):
        transcription = original_text_from_update[3:].strip()
        if transcription:
            final_voice_text_for_history = f"meta_content_type: voice_message (расшифровано_пользователем: {MY_NAME_FOR_HISTORY}) {transcription}"
            logger.info(f"Processing /v command in chat {chat_id}. History text: '{final_voice_text_for_history[:50]}...'")
            update_chat_history(chat_id, "user", final_voice_text_for_history)
            logger.info(f"Message with /v command in chat {chat_id} was not deleted (deletion disabled).")
            interlocutor_name_for_suggestion = chat.first_name or chat.full_name or f"Chat_{chat_id}"; interlocutor_id_for_description = str(chat.id)
            async def delayed_processing_for_v_command():
                try:
                    await asyncio.sleep(DEBOUNCE_DELAY)
                    logger.debug(f"Debounce for /v in chat {chat_id} finished. Starting processing.")
                    await process_chat_after_delay(chat_id, interlocutor_name_for_suggestion, interlocutor_id_for_description, business_connection_id, context)
                except asyncio.CancelledError: logger.info(f"Debounce task for /v in chat {chat_id} was cancelled.")
                except Exception as e: logger.error(f"Error in delayed /v processing for chat {chat_id}: {e}", exc_info=True)
            if chat_id in debounce_tasks:
                try: debounce_tasks[chat_id].cancel(); logger.debug(f"Cancelled previous debounce for chat {chat_id} due to /v.")
                except Exception: pass
            task = asyncio.create_task(delayed_processing_for_v_command()); debounce_tasks[chat_id] = task
            logger.info(f"Scheduled response generation for chat {chat_id} after /v command.")
        else: logger.warning(f"Received empty /v command from {MY_TELEGRAM_ID} in chat {chat_id}. Ignoring.")
        return
    is_outgoing = sender and sender.id == MY_TELEGRAM_ID
    if is_outgoing:
        logger.info(f"Processing OUTGOING business message in chat {chat_id} from {sender_id_str}")
        logger.debug(f"Outgoing message details: text='{message_to_process.text}', caption='{message_to_process.caption}', photo_present={message_to_process.photo is not None}, video_present={message_to_process.video is not None}")
        update_chat_history(chat_id, "model", enriched_history_text)
        if chat_id in debounce_tasks:
             logger.debug(f"Cancelling debounce task for chat {chat_id} due to outgoing message.")
             try: debounce_tasks[chat_id].cancel()
             except Exception as e: logger.error(f"Error cancelling task for chat {chat_id}: {e}")
             del debounce_tasks[chat_id]
        return
    if not sender: logger.warning(f"Incoming message in chat {chat_id} without sender info. Skipping."); return
    logger.info(f"Processing INCOMING business message from user {sender_id_str} in chat {chat_id} via ConnID: {business_connection_id}")
    update_chat_history(chat_id, "user", enriched_history_text)
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
                update_chat_history(target_chat_id_for_send, "model", part_text) # Сохраняем каждую отправленную часть
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