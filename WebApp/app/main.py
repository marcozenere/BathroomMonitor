import os
import asyncio
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
import uvicorn
from aiomqtt import Client as MQTTClient, MqttError
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------
# LOGGING CONFIG
# -------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# -------------------
# CONFIGURATION
# -------------------
# It's better to fail fast if essential configs are missing.
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    # Render provides this URL automatically. Fallback for local dev.
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    # Render provides a REDIS_URL when you create a Redis service.
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
except KeyError as e:
    raise RuntimeError(f"Missing essential environment variable: {e}") from e

# MQTT Config
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
DEVICE_ID = os.getenv("DEVICE_ID", "device1")
MQTT_SENSOR_TOPIC = f"esp32/{DEVICE_ID}/sensor"

# Webhook Config
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else "https://your.domain.com" + WEBHOOK_PATH

# Redis Keys for storing state
REDIS_KEY_STATE = "bathroom:state"
REDIS_KEY_SUBSCRIBERS = "bathroom:subscribers"

# -------------------
# GLOBAL CONTEXT (MANAGED BY LIFESPAN)
# -------------------
# This dictionary will hold our initialized clients and tasks
# so we can access them throughout the application's lifespan.
app_context = {}

async def notify_subscribers(state: str):
    """Notifies all subscribers about a state change and removes them."""
    if not app_context.get('bot_app') or not app_context.get('redis_client'):
        logger.error("Bot or Redis not initialized. Cannot notify.")
        return

    bot = app_context['bot_app'].bot
    redis_client = app_context['redis_client']

    # Use a pipeline for atomic operations if needed, but SMEMBERS is fine here.
    subscribers = await redis_client.smembers(REDIS_KEY_SUBSCRIBERS)
    if not subscribers:
        logger.info("State changed, but no subscribers to notify.")
        return

    logger.info(f"Notifying {len(subscribers)} subscribers...")
    message = "✅ Bagno libero! Corri." if state == "clear" else "⚠️ Qualcosa è andato storto."

    # Send all notifications concurrently
    tasks = [
        bot.send_message(chat_id=int(chat_id), text=message)
        for chat_id in subscribers
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for chat_id, result in zip(subscribers, results):
        if isinstance(result, Exception):
            logger.error(f"[Bot] Failed to notify {chat_id}: {result}")

    # Clear the subscriber list after notification
    await redis_client.delete(REDIS_KEY_SUBSCRIBERS)
    logger.info("Cleared subscriber list.")


async def mqtt_listener():
    """A background task that listens to MQTT messages and updates state."""
    reconnect_interval = 5  # seconds
    while True:
        try:
            async with MQTTClient(hostname=MQTT_BROKER, port=MQTT_PORT) as client:
                logger.info("[MQTT] Connected successfully.")
                await client.subscribe(MQTT_SENSOR_TOPIC)
                async for message in client.messages:
                    payload = message.payload.decode().strip()
                    logger.info(f"[MQTT] {message.topic}: {payload}")

                    new_state = "detected" if payload == "detected" else "clear"

                    # Get Redis client from context
                    redis_client = app_context.get('redis_client')
                    if not redis_client:
                        logger.error("[MQTT] Redis client not available in context.")
                        continue

                    # Check and update state in Redis
                    previous_state = await redis_client.get(REDIS_KEY_STATE)
                    previous_state = previous_state.decode() if previous_state else "unknown"

                    if new_state != previous_state:
                        await redis_client.set(REDIS_KEY_STATE, new_state)
                        logger.info(f"State changed from '{previous_state}' to '{new_state}'")
                        if new_state == "clear":
                            # Use asyncio.create_task to run notification in the background
                            asyncio.create_task(notify_subscribers(new_state))
                    else:
                        logger.info(f"State remained '{new_state}'. No change.")

        except MqttError as e:
            logger.error(f"[MQTT] Connection error: {e}. Reconnecting in {reconnect_interval}s...")
            await asyncio.sleep(reconnect_interval)
        except Exception as e:
            logger.error(f"[MQTT] An unexpected error occurred: {e}. Restarting listener...")
            await asyncio.sleep(reconnect_interval)


# -------------------
# FASTAPI LIFESPAN MANAGER
# -------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages application startup and shutdown events.
    This is the modern replacement for @app.on_event("startup").
    """
    # --- STARTUP ---
    logger.info("Application starting up...")

    # Initialize Redis
    redis_client = redis.from_url(REDIS_URL)
    await redis_client.ping()
    app_context['redis_client'] = redis_client
    logger.info("Redis connection established.")

    # Initialize Telegram Bot Application
    bot_app = Application.builder().token(BOT_TOKEN).build()
    app_context['bot_app'] = bot_app

    # Add command handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("checkavailability", checkavailability_command))
    bot_app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

    # Initialize bot and set webhook
    await bot_app.initialize()
    await bot_app.bot.delete_webhook()
    await bot_app.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to: {WEBHOOK_URL}")

    # Start the MQTT listener as a background task
    mqtt_task = asyncio.create_task(mqtt_listener())
    app_context['mqtt_task'] = mqtt_task
    logger.info("MQTT listener started as a background task.")

    yield  # Application runs here

    # --- SHUTDOWN ---
    logger.info("Application shutting down...")
    # Properly shutdown bot
    await app_context['bot_app'].shutdown()
    # Close Redis connection
    await app_context['redis_client'].close()
    # Cancel the background task
    app_context['mqtt_task'].cancel()
    try:
        await app_context['mqtt_task']
    except asyncio.CancelledError:
        logger.info("MQTT listener task cancelled successfully.")


# -------------------
# FASTAPI APP
# -------------------
app = FastAPI(lifespan=lifespan)

# -------------------
# TELEGRAM COMMAND HANDLERS
# -------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Ciao! Usa /checkavailability per controllare lo stato del bagno.")

async def checkavailability_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    redis_client = app_context['redis_client']
    state_bytes = await redis_client.get(REDIS_KEY_STATE)
    current_state = state_bytes.decode() if state_bytes else "unknown"

    if current_state == "detected":
        # SADD adds the item to a set, automatically handling duplicates.
        await redis_client.sadd(REDIS_KEY_SUBSCRIBERS, str(chat_id))
        await update.message.reply_text(
            "⚠️ Bagno occupato! Ti ho aggiunto alla lista di attesa. "
            "Riceverai una notifica non appena si libera."
        )
    elif current_state == "clear":
        await update.message.reply_text("✅ Bagno libero! Corri.")
    else:
        await update.message.reply_text(
            "❓ Errore nella lettura del sensore! Contatta l'amministratore se il problema persiste."
        )

async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    redis_client = app_context['redis_client']
    # SREM returns 1 if the item was removed, 0 otherwise.
    removed_count = await redis_client.srem(REDIS_KEY_SUBSCRIBERS, str(chat_id))
    if removed_count > 0:
        await update.message.reply_text("🛑 Non riceverai più notifiche.")
    else:
        await update.message.reply_text("ℹ️ Non eri iscritto alle notifiche.")


# -------------------
# WEBHOOK ENDPOINT
# -------------------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    """Handles incoming updates from Telegram."""
    try:
        bot_app = app_context.get('bot_app')
        if not bot_app:
            logger.error("Bot application not initialized.")
            raise HTTPException(status_code=500, detail="Bot not ready")

        data = await req.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1>Telegram Bot is running...</h1>"


# -------------------
# MAIN ENTRY POINT (for local development)
# -------------------
if __name__ == "__main__":
    # This block is for local testing. Render will use the start command in render.yaml
    logger.info("Starting server for local development...")
    uvicorn.run(app, host="0.0.0.0", port=8000)