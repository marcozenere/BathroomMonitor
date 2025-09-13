import os
import asyncio
import logging
from contextlib import asynccontextmanager
import re

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
import uvicorn
from aiomqtt import Client as MQTTClient, MqttError
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telegram import Update, MenuButtonWebApp, WebAppInfo
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
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
except KeyError as e:
    raise RuntimeError(f"Missing essential environment variable: {e}") from e

logger.info(f"Attempting to connect to Redis at: {REDIS_URL}")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_BASE_TOPIC = "esp32/+/sensor" # Wildcard to listen to all devices

# Webhook Config
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else None

# -------------------
# REDIS KEY HELPERS
# -------------------
def redis_key_state(device_id: str) -> str:
    return f"bathroom:state:{device_id}"

def redis_key_subscribers(device_id: str) -> str:
    return f"bathroom:subscribers:{device_id}"

# -------------------
# GLOBAL CONTEXT
# -------------------
app_context = {}


async def notify_subscribers(device_id: str, state: str):
    """Notifies subscribers of a specific device about a state change."""
    if not all(k in app_context for k in ['bot_app', 'redis_client']):
        logger.error("Bot or Redis not initialized. Cannot notify.")
        return

    bot = app_context['bot_app'].bot
    redis_client = app_context['redis_client']
    key_sub = redis_key_subscribers(device_id)

    subscribers = await redis_client.smembers(key_sub)
    if not subscribers:
        logger.info(f"State changed for {device_id}, but no subscribers to notify.")
        return

    logger.info(f"Notifying {len(subscribers)} subscribers for device {device_id}...")
    message = f"✅ Bagno '{device_id}' libero! Corri." if state == "clear" else f"⚠️ Qualcosa è andato storto con il bagno '{device_id}'."

    tasks = [bot.send_message(chat_id=int(chat_id), text=message) for chat_id in subscribers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for chat_id, result in zip(subscribers, results):
        if isinstance(result, Exception):
            logger.error(f"[Bot] Failed to notify {chat_id} for {device_id}: {result}")

    await redis_client.delete(key_sub)
    logger.info(f"Cleared subscriber list for {device_id}.")


async def mqtt_listener():
    """Listens to MQTT messages from all devices and updates their state."""
    reconnect_interval = 5
    topic_regex = re.compile(r"esp32/([^/]+)/sensor")

    while True:
        try:
            async with MQTTClient(hostname=MQTT_BROKER, port=MQTT_PORT) as client:
                logger.info("[MQTT] Connected successfully.")
                await client.subscribe(MQTT_BASE_TOPIC)
                async with client.messages() as messages:
                    async for message in messages:
                        topic_match = topic_regex.match(message.topic.string)
                        if not topic_match:
                            continue
                        
                        device_id = topic_match.group(1)
                        payload = message.payload.decode().strip()
                        logger.info(f"[MQTT] Device '{device_id}': {payload}")

                        new_state = "detected" if payload == "detected" else "clear"
                        redis_client = app_context.get('redis_client')
                        if not redis_client:
                            logger.error("[MQTT] Redis client not available.")
                            continue

                        key_state = redis_key_state(device_id)
                        previous_state = await redis_client.get(key_state) or "unknown"

                        if new_state != previous_state:
                            await redis_client.set(key_state, new_state)
                            logger.info(f"State for '{device_id}' changed from '{previous_state}' to '{new_state}'")
                            if new_state == "clear":
                                asyncio.create_task(notify_subscribers(device_id, new_state))
                        
        except MqttError as e:
            logger.error(f"[MQTT] Connection error: {e}. Reconnecting in {reconnect_interval}s...")
            await asyncio.sleep(reconnect_interval)
        except Exception as e:
            logger.error(f"[MQTT] Unexpected error: {e}. Restarting listener...")
            await asyncio.sleep(reconnect_interval)


# -------------------
# FASTAPI LIFESPAN MANAGER
# -------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages application startup and shutdown events."""
    logger.info("Application starting up...")
    
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    # ... (retry logic for Redis connection) ...
    app_context['redis_client'] = redis_client
    
    # Set an initial state for the default device if one doesn't exist
    await redis_client.setnx(redis_key_state("device1"), "unknown")
    
    bot_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    app_context['bot_app'] = bot_app
    # ... (add handlers) ...
    
    await bot_app.initialize()
    if WEBHOOK_URL:
        # ... (webhook and menu button setup) ...
        logger.info("Webhook and menu button setup complete.")
    else:
        logger.warning("RENDER_EXTERNAL_URL not set. Skipping setup.")

    mqtt_task = asyncio.create_task(mqtt_listener())
    app_context['mqtt_task'] = mqtt_task
    logger.info("MQTT listener started.")

    yield

    logger.info("Application shutting down...")
    await app_context['bot_app'].shutdown()
    await app_context['redis_client'].close()
    app_context['mqtt_task'].cancel()
    try:
        await app_context['mqtt_task']
    except asyncio.CancelledError:
        logger.info("MQTT listener task cancelled.")

# -------------------
# FASTAPI APP & MODELS
# -------------------
app = FastAPI(lifespan=lifespan)

class UserAction(BaseModel):
    user_id: int
    device_id: str

# -------------------
# TELEGRAM COMMAND HANDLERS
# -------------------
# (Handlers remain largely the same, but would need logic for multiple devices in the future)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Ciao! Clicca il tasto 'Menu' per avviare l'app.")

async def checkavailability_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This command would need updating to ask the user WHICH bathroom they want to check
    # For now, it defaults to "device1"
    chat_id = update.effective_chat.id
    device_id = "device1"
    redis_client = app_context['redis_client']
    current_state = await redis_client.get(redis_key_state(device_id)) or "unknown"
    if current_state == "detected":
        await redis_client.sadd(redis_key_subscribers(device_id), str(chat_id))
        await update.message.reply_text(f"⚠️ Bagno '{device_id}' occupato! Riceverai una notifica.")
    # ... other states
    
async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Similarly, this needs to know which subscription to remove
    chat_id = update.effective_chat.id
    device_id = "device1" 
    redis_client = app_context['redis_client']
    removed_count = await redis_client.srem(redis_key_subscribers(device_id), str(chat_id))
    # ... response logic

# -------------------
# WEBHOOK ENDPOINT
# -------------------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    # ... (webhook processing logic) ...
    return {"ok": True}

# -------------------
# API ENDPOINTS FOR WEB APP
# -------------------
@app.get("/api/status/{device_id}/{user_id}")
async def get_status(device_id: str, user_id: int):
    redis_client = app_context['redis_client']
    pipe = redis_client.pipeline()
    pipe.get(redis_key_state(device_id))
    pipe.sismember(redis_key_subscribers(device_id), str(user_id))
    results = await pipe.execute()
    current_state = results[0] or "unknown"
    is_subscribed = bool(results[1])
    return {"status": current_state, "is_subscribed": is_subscribed}

@app.post("/api/subscribe")
async def subscribe_user(user_action: UserAction):
    redis_client = app_context['redis_client']
    await redis_client.sadd(redis_key_subscribers(user_action.device_id), str(user_action.user_id))
    return {"success": True}

@app.post("/api/unsubscribe")
async def unsubscribe_user(user_action: UserAction):
    redis_client = app_context['redis_client']
    await redis_client.srem(redis_key_subscribers(user_action.device_id), str(user_action.user_id))
    return {"success": True}

# -------------------
# ROOT ENDPOINT - SERVES THE MINI APP
# -------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    # Note: The HTML/JS is now much longer and includes the new refresh button logic.
    return """
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Stato Bagno</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            /* Styles remain largely the same, with minor adjustments for new elements */
            :root {
                --telegram-bg-color: #ffffff;
                --telegram-text-color: #000000;
                --telegram-hint-color: #999999;
                --telegram-link-color: #2481cc;
                --telegram-button-color: #2481cc;
                --telegram-button-text-color: #ffffff;
                --telegram-secondary-bg-color: #f3f3f3;
            }
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: var(--telegram-bg-color); color: var(--telegram-text-color); margin: 0; padding: 1rem; display: flex; flex-direction: column; align-items: center; min-height: 100vh; box-sizing: border-box; }
            .status-card { width: 100%; max-width: 400px; border-radius: 1rem; padding: 2rem; text-align: center; transition: all 0.3s ease; margin-bottom: 1rem; }
            .status-card.clear { background-color: #e0f8e9; box-shadow: 0 4px 20px rgba(45, 212, 111, 0.2); }
            .status-card.detected { background-color: #ffebee; box-shadow: 0 4px 20px rgba(239, 83, 80, 0.2); }
            .status-card.unknown { background-color: #f3f4f6; box-shadow: 0 4px 20px rgba(156, 163, 175, 0.2); }
            .status-icon { width: 64px; height: 64px; margin: 0 auto 1rem; }
            .status-text { font-size: 2rem; font-weight: 700; margin-bottom: 0.5rem; }
            .status-text.clear { color: #22c55e; }
            .status-text.detected { color: #ef4444; }
            .status-text.unknown { color: #6b7280; }
            .status-description { color: var(--telegram-hint-color); min-height: 20px; transition: color 0.3s; }
            .action-button { display: flex; align-items: center; justify-content: center; width: 100%; max-width: 400px; padding: 0.8rem 1rem; margin-top: 0.5rem; border: none; border-radius: 0.75rem; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.2s; background-color: var(--telegram-button-color); color: var(--telegram-button-text-color); }
            .refresh-button { background-color: var(--telegram-secondary-bg-color); color: var(--telegram-link-color); }
            .action-button:active { transform: scale(0.98); }
            .action-button:disabled { background-color: #d1d5db; cursor: not-allowed; }
            .action-button svg { margin-right: 0.5rem; }
            .hidden { display: none; }
        </style>
    </head>
    <body>
        <!-- Views for loading, app, and error states -->
        <div id="loading-view">...</div>
        <div id="app-view" class="hidden">
            <div id="status-card">...</div>
            <button id="refresh-button" class="action-button refresh-button">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>
                Aggiorna Stato
            </button>
            <button id="subscribe-button" class="action-button hidden">...</button>
            <button id="unsubscribe-button" class="action-button hidden">...</button>
        </div>
        <div id="error-view" class="hidden">...</div>

        <script>
            const tg = window.Telegram.WebApp;
            const userId = tg.initDataUnsafe?.user?.id;
            // For now, we hardcode the device we are controlling.
            // In the future, this could come from a dropdown menu.
            const deviceId = "device1";

            // DOM Elements
            const loadingView = document.getElementById('loading-view');
            const appView = document.getElementById('app-view');
            const statusDescription = document.getElementById('status-description');
            const refreshButton = document.getElementById('refresh-button');
            // ... other elements

            const icons = { /* ... icon SVGs ... */ };

            function updateUI(state, isSubscribed) {
                // ... logic to update card, text, and icons ...
                // ... logic to show/hide subscribe/unsubscribe based on state and isSubscribed ...
            }

            async function fetchStatus(isManualRefresh = false) {
                if(isManualRefresh) {
                    tg.HapticFeedback.impactOccurred('light');
                    statusDescription.textContent = 'Aggiornamento in corso...';
                }
                try {
                    const response = await fetch(`/api/status/${deviceId}/${userId}`);
                    if (!response.ok) throw new Error('Network response was not ok');
                    const data = await response.json();
                    updateUI(data.status, data.is_subscribed);
                    if(isManualRefresh) {
                         statusDescription.textContent = 'Stato aggiornato!';
                    }
                } catch (error) {
                    console.error('Failed to fetch status:', error);
                    updateUI('unknown', false);
                     if(isManualRefresh) {
                         statusDescription.textContent = 'Errore durante l'aggiornamento.';
                    }
                }
            }

            async function handleSubscribe() {
                // ... fetch POST to /api/subscribe with deviceId and userId ...
            }
            
            async function handleUnsubscribe() {
                // ... fetch POST to /api/unsubscribe with deviceId and userId ...
            }

            function initializeApp() {
                if (!userId) { /* Show error */ return; }
                tg.ready();
                tg.expand();
                
                // Set theme colors from Telegram
                // ...
                
                refreshButton.addEventListener('click', () => fetchStatus(true));
                // ... subscribe/unsubscribe button listeners ...

                fetchStatus(); // Initial fetch on load
            }

            window.addEventListener('load', initializeApp);
        </script>
    </body>
    </html>
    """

# (Simplified Python logic for brevity in this response)
if __name__ == "__main__":
    logger.info("Starting server for local development...")
    uvicorn.run(app, host="0.0.0.0", port=8000)