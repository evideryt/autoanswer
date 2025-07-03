import asyncio
import httpx # Понадобится для асинхронных HTTP-запросов

# ... (в начале файла, где остальные импорты) ...

SELF_PING_INTERVAL_SECONDS = 4 * 60  # Пинговать каждые 4 минуты
PING_TARGET_URL = None # Будет инициализироваться из WEBHOOK_URL и BOT_TOKEN

async def self_pinger():
    """Периодически пингует сам себя, чтобы сервис не засыпал."""
    global PING_TARGET_URL
    if not PING_TARGET_URL:
        logger.warning("Self-pinger: PING_TARGET_URL is not set. Cannot start pinger.")
        return

    logger.info(f"Self-pinger started for URL: {PING_TARGET_URL}. Interval: {SELF_PING_INTERVAL_SECONDS}s")
    await asyncio.sleep(30) # Начальная задержка перед первым пингом

    async with httpx.AsyncClient(timeout=20.0) as client: # Устанавливаем таймаут
        while True:
            try:
                logger.info(f"Self-pinger: Sending ping to {PING_TARGET_URL}")
                response = await client.get(PING_TARGET_URL)
                # Нам не важен ответ, главное, чтобы запрос дошел
                logger.info(f"Self-pinger: Ping successful, status: {response.status_code}")
            except httpx.TimeoutException:
                logger.warning(f"Self-pinger: Ping to {PING_TARGET_URL} timed out.")
            except httpx.RequestError as e:
                logger.error(f"Self-pinger: Error pinging {PING_TARGET_URL}: {e}")
            except Exception as e:
                logger.error(f"Self-pinger: Unexpected error: {e}", exc_info=True)
            
            await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)

# ... (внутри `if __name__ == "__main__":`) ...
if __name__ == "__main__":
    logger.info("Initializing Telegram Business Bot with Gemini...")
    # ... (парсинг конфига, инициализация БД) ...

    # --- Инициализация PING_TARGET_URL для self-pinger ---
    if WEBHOOK_URL and BOT_TOKEN:
        PING_TARGET_URL = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
    else:
        logger.error("CRITICAL: WEBHOOK_URL or BOT_TOKEN missing, cannot set PING_TARGET_URL for self-pinger.")
        # Можно решить, останавливать ли бота, если пингер не может запуститься.
        # exit() 

    # ... (инициализация Gemini, создание Application) ...
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    # ... (добавление хендлеров) ...

    logger.info("Application built. Starting webhook listener...")
    
    # --- Запуск self-pinger как фоновой задачи ---
    # Важно: это очень упрощенный запуск. В реальном приложении нужно
    # предусмотреть корректную отмену этой задачи при остановке приложения.
    if PING_TARGET_URL:
        # asyncio.create_task(self_pinger()) # Если запускать внутри run_webhook
        pass # Пока не будем добавлять, чтобы не усложнять без необходимости

    try:
        # Если бы мы хотели запустить пингер параллельно с run_webhook,
        # это потребовало бы более сложного управления asyncio loop.
        # Для простоты, сейчас внешний пингер - лучший вариант.
        # Если все же делать self-ping, его можно интегрировать в post_init или
        # управлять им через asyncio.gather вместе с application.run_webhook,
        # но это уже более продвинутая тема asyncio.
        
        async def main_with_pinger():
            if PING_TARGET_URL:
                 # Запускаем пингер как фоновую задачу
                 pinger_task = asyncio.create_task(self_pinger())
                 logger.info("Self-pinger task created.")
            
            # Запускаем вебхук
            await application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=BOT_TOKEN,
                webhook_url=PING_TARGET_URL # Используем тот же URL, что и для пингера
            )
            # Если run_webhook завершится, отменяем пингер (если он был запущен)
            if PING_TARGET_URL and 'pinger_task' in locals() and not pinger_task.done():
                logger.info("Main webhook loop finished, cancelling pinger task...")
                pinger_task.cancel()
                try:
                    await pinger_task
                except asyncio.CancelledError:
                    logger.info("Pinger task successfully cancelled.")

        # Заменяем простой asyncio.run на запуск main_with_pinger
        # asyncio.run(application.run_webhook(...))
        asyncio.run(main_with_pinger())

    except ValueError as e: logger.critical(f"CRITICAL ERROR asyncio.run: {e}", exc_info=True)
    except Exception as e: logger.critical(f"CRITICAL ERROR Webhook server: {e}", exc_info=True)
    finally: logger.info("Webhook server shut down.")