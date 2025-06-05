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
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    MessageHandler,
    filters, # Будем использовать фильтры активнее
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest

# --- Настройки и переменные ---
# ... (все переменные как были, включая GEMINI_MODEL_NAME) ...
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
# ... (логи psycopg и httpx) ...
logger = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN"); WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443)); MY_TELEGRAM_ID_STR = os.environ.get("MY_TELEGRAM_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY"); CONFIG_FILE = "adp.txt"
DATABASE_URL = os.environ.get("DATABASE_URL"); CALENDAR_FILE = "calc.txt"

# --- НОВЫЕ ПЕРЕМЕННЫЕ ДЛЯ ГРУППЫ ---
TARGET_GROUP_CHAT_ID_STR = os.environ.get("TARGET_GROUP_CHAT_ID") # ID твоего @evider_chat
MY_BOT_USERNAME = os.environ.get("MY_BOT_USERNAME") # @username твоего бота

TARGET_GROUP_CHAT_ID = None
if TARGET_GROUP_CHAT_ID_STR:
    try: TARGET_GROUP_CHAT_ID = int(TARGET_GROUP_CHAT_ID_STR)
    except ValueError: logger.error(f"TARGET_GROUP_CHAT_ID ('{TARGET_GROUP_CHAT_ID_STR}') is not a valid integer.")
# Если MY_BOT_USERNAME не задан, теги работать не будут, но ответы на reply могут
if not MY_BOT_USERNAME: logger.warning("MY_BOT_USERNAME is not set. Tag-based replies in group will not work.")


MAX_HISTORY_PER_CHAT = 1000 # <--- Увеличено для группы (и бизнес-чатов тоже, если хочешь)
DEBOUNCE_DELAY = 15; MY_NAME_FOR_HISTORY = "киткат"; MESSAGE_SPLIT_DELAY = 0.7
GEMINI_MODEL_NAME = "gemini-2.0-flash"
BASE_SYSTEM_PROMPT = ""; MY_CHARACTER_DESCRIPTION = ""; TOOLS_PROMPT = ""; CHAR_DESCRIPTIONS = {}
debounce_tasks = {}; pending_replies = {}; gemini_model = None; MY_TELEGRAM_ID = None
if not BOT_TOKEN: logger.critical("CRITICAL: Missing BOT_TOKEN"); exit()
# ... (остальные проверки, включая MY_TELEGRAM_ID) ...
if MY_TELEGRAM_ID_STR:
    try: MY_TELEGRAM_ID = int(MY_TELEGRAM_ID_STR)
    except ValueError: logger.critical(f"CRITICAL: MY_TELEGRAM_ID ('{MY_TELEGRAM_ID_STR}') is not valid."); exit()
else: logger.critical("CRITICAL: Missing MY_TELEGRAM_ID."); exit()
if not DATABASE_URL: logger.critical("CRITICAL: Missing DATABASE_URL"); exit()


# --- Функция sanitize_gemini_response (без изменений) ---
# ... (код sanitize_gemini_response) ...
def sanitize_gemini_response(text: str) -> str:
    if not text: return ""
    text = re.sub(r"meta_reply_to:\s*[\w\d_()«»\s.:-]+?\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*meta_[\w_]+:.*?(?=\w{3,}|$)", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"meta_[\w_]+:\s*[\w\d_().,;:\"'«»<>?!#@$%^&*\-+=/\s]+?(?=\s*\w{2,}|$)", "", text).strip()
    text = re.sub(r"meta_[\w-]+:\s*[^ ]+\s*", "", text).strip()
    text = re.sub(r"\s*meta_[\w-]+\s*", " ", text).strip()
    if text.lower().startswith("meta_"):
        match = re.search(r"(?:meta_[\w\s:().,«»\"'-]+)+(.+)", text, re.IGNORECASE)
        if match and match.group(1).strip(): text = match.group(1).strip(); logger.info(f"Sanitizer: Extracted text after meta_: '{text[:100]}'")
        else: return ""
    text = re.sub(r"\s{2,}", " ", text).strip(); logger.debug(f"Sanitized Gemini response: '{text[:100]}...'")
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

# --- ИЗМЕНЕННАЯ Функция для обогащения текста сообщения ---
def enrich_message_for_history(message: Message, bot_id: int | None = None) -> str:
    """
    Формирует обогащенное текстовое представление сообщения для истории.
    `bot_id` нужен, чтобы корректно определить, является ли сообщение от "model" (бота).
    """
    meta_parts = [] 
    text_parts = []   
    
    sender_display_name = "Неизвестный_отправитель"
    if message.from_user:
        # Для группы, если это сообщение от бота, оно будет иметь from_user.id == bot_id
        # Если это от другого пользователя, то его ID.
        # Если это анонимный админ, from_user может быть None, но sender_chat может быть заполнен.
        if bot_id and message.from_user.id == bot_id:
            sender_display_name = MY_NAME_FOR_HISTORY # Это ответ бота в группе
        else:
            sender_display_name = message.from_user.first_name or message.from_user.full_name or f"User_{message.from_user.id}"
    elif message.sender_chat: # Сообщение от имени канала или анонимного админа
        sender_display_name = message.sender_chat.title or f"Chat_{message.sender_chat.id}"
    
    # Добавляем имя отправителя в мета, если это не наш бот отвечает (для истории)
    # В истории роль 'user' или 'model' уже определяет, кто это.
    # Но для Gemini может быть полезно видеть имя отправителя в тексте 'user' сообщения.
    # Решим, нужно ли это. Пока оставим как есть, роль 'user'/'model' должна быть достаточной.

    if message.reply_to_message:
        reply_to = message.reply_to_message
        reply_sender_name = "сообщению_собеседника"
        if reply_to.from_user:
            if bot_id and reply_to.from_user.id == bot_id:
                reply_sender_name = f"сообщению_бота_({MY_NAME_FOR_HISTORY})"
            else:
                reply_sender_name = reply_to.from_user.first_name or reply_to.from_user.full_name or f"User_{reply_to.from_user.id}"
        elif reply_to.sender_chat:
            reply_sender_name = f"сообщению_от_канала_({reply_to.sender_chat.title or f'Chat_{reply_to.sender_chat.id}'})"
        
        replied_message_snippet = (reply_to.text or reply_to.caption or "медиа_без_текста")[:30].replace('\n', ' ').replace(':', ';')
        meta_parts.append(f"meta_reply_to: {reply_sender_name} (текст_ответа_начинался_с: «{replied_message_snippet}»)")

    # ... (остальная логика fwd, media, text остается как в прошлом варианте) ...
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
    global gemini_model; # ... (остальной код функции без изменений) ...
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

# --- Функция обработки чата ПОСЛЕ задержки (для бизнес-чатов, без изменений) ---
# ... (код process_chat_after_delay) ...
async def process_chat_after_delay(chat_id: int, sender_name: str, sender_id_str: str, business_connection_id: str | None, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Debounce timer expired for BUSINESS chat {chat_id} with sender {sender_id_str}. Processing...")
    current_history = get_formatted_history(chat_id); saratov_time_str = get_saratov_datetime_info(); initial_contents = []; context_block_text = ""
    if MY_CHARACTER_DESCRIPTION: context_block_text += f"Немного информации обо мне ({MY_NAME_FOR_HISTORY}):\n{MY_CHARACTER_DESCRIPTION}\n\n"
    interlocutor_description = CHAR_DESCRIPTIONS.get(sender_id_str)
    if interlocutor_description: context_block_text += f"Информация о текущем собеседнике ({sender_name}, ID: {sender_id_str}):\n{interlocutor_description}\n\n"
    context_block_text += f"Текущее время в Саратове (где находится Киткат): {saratov_time_str}\n\n"
    if TOOLS_PROMPT: context_block_text += f"Инструкции по инструментам:\n{TOOLS_PROMPT}\n\n"
    if context_block_text.strip(): initial_contents.append({"role": "model", "parts": [{"text": context_block_text.strip()}]})
    initial_contents.extend(current_history); logger.debug("Attempting initial Gemini call for business chat...")
    gemini_response_from_api = await generate_gemini_response(initial_contents)
    gemini_response_raw = sanitize_gemini_response(gemini_response_from_api) if gemini_response_from_api else None
    if gemini_response_from_api and not gemini_response_raw: logger.warning(f"Sanitizer possibly removed the entire Gemini response. Original: '{gemini_response_from_api[:100]}'")
    if gemini_response_raw == "!fetchcalc":
        logger.info(f"Received '!fetchcalc' signal for business chat {chat_id}. Fetching calendar info...")
        calendar_content = "Информация из календаря недоступна." # ... (логика чтения календаря) ...
        try:
            with open(CALENDAR_FILE, 'r', encoding='utf-8') as f: calendar_content = f.read().strip()
            if not calendar_content: logger.warning(f"Calendar file '{CALENDAR_FILE}' is empty."); calendar_content = "Файл календаря пуст."
            else: logger.info(f"Successfully read calendar file '{CALENDAR_FILE}'.")
        except FileNotFoundError: logger.error(f"Calendar file '{CALENDAR_FILE}' not found!")
        except Exception as e: logger.error(f"Error reading calendar file '{CALENDAR_FILE}': {e}")
        calendar_prompt_contents = []; calendar_intro = (f"Для ответа на предыдущий вопрос пользователя требуется информация из его расписания.\n" # ... (формирование промпта с календарем) ...
                          f"Текущая дата и время в Саратове (где находится пользователь Киткат): {saratov_time_str}\n"
                          f"Вот предоставленное пользователем расписание (содержимое файла {CALENDAR_FILE}):\n------\n{calendar_content}\n------\n"
                          f"Пожалуйста, проанализируй это расписание и текущее время, и ответь на последний вопрос пользователя, следуя основной инструкции и стилю Китката.")
        context_block_text_for_calendar = ""
        if MY_CHARACTER_DESCRIPTION: context_block_text_for_calendar += f"Напомню информацию обо мне ({MY_NAME_FOR_HISTORY}):\n{MY_CHARACTER_DESCRIPTION}\n\n"
        if interlocutor_description: context_block_text_for_calendar += f"Напомню информацию о собеседнике ({sender_name}, ID: {sender_id_str}):\n{interlocutor_description}\n\n"
        context_block_text_for_calendar += f"Текущее время в Саратове: {saratov_time_str}\n\n"
        if context_block_text_for_calendar.strip(): calendar_prompt_contents.append({"role": "model", "parts": [{"text": context_block_text_for_calendar.strip()}]})
        calendar_prompt_contents.append({"role": "user", "parts": [{"text": calendar_intro}]}); calendar_prompt_contents.extend(current_history)
        logger.debug("Attempting second Gemini call with calendar info fo