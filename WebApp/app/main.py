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
                        topic_match = topic_regex.match(str(message.topic))
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
    
    connected_to_redis = False
    for attempt in range(5):
        try:
            await redis_client.ping()
            connected_to_redis = True
            logger.info("Redis connection established.")
            break
        except RedisConnectionError:
            logger.warning(f"Redis connection attempt {attempt + 1} failed. Retrying in 3 seconds...")
            await asyncio.sleep(3)

    if not connected_to_redis:
        logger.error("Could not establish connection to Redis after multiple attempts.")
        raise RuntimeError("Failed to connect to Redis during startup.")

    app_context['redis_client'] = redis_client
    
    await redis_client.setnx(redis_key_state("device1"), "unknown")
    
    bot_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    app_context['bot_app'] = bot_app
    
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("checkavailability", checkavailability_command))
    bot_app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    
    await bot_app.initialize()
    if WEBHOOK_URL:
        await bot_app.bot.delete_webhook()
        await bot_app.bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook set to: {WEBHOOK_URL}")

        menu_button = MenuButtonWebApp(
            text="Apri App Bagno",
            web_app=WebAppInfo(url=RENDER_EXTERNAL_URL)
        )
        await bot_app.bot.set_chat_menu_button(menu_button=menu_button)
        logger.info("Chat menu button set to launch the Mini App.")
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
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Ciao! Clicca il tasto 'Menu' per avviare l'app.")

async def checkavailability_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    device_id = "device1"
    redis_client = app_context['redis_client']
    current_state = await redis_client.get(redis_key_state(device_id)) or "unknown"

    if current_state == "detected":
        await redis_client.sadd(redis_key_subscribers(device_id), str(chat_id))
        await update.message.reply_text(f"⚠️ Bagno '{device_id}' occupato! Riceverai una notifica.")
    elif current_state == "clear":
        await update.message.reply_text(f"✅ Bagno '{device_id}' libero! Corri.")
    else:
        await update.message.reply_text(f"❓ Stato del bagno '{device_id}' sconosciuto.")
    
async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    device_id = "device1" 
    redis_client = app_context['redis_client']
    removed_count = await redis_client.srem(redis_key_subscribers(device_id), str(chat_id))
    if removed_count > 0:
        await update.message.reply_text("🛑 Non riceverai più notifiche.")
    else:
        await update.message.reply_text("ℹ️ Non eri iscritto alle notifiche.")

# -------------------
# WEBHOOK ENDPOINT
# -------------------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
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
    return """
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Stato Bagno</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; 
                background-color: #f0f0f0; 
                color: #000000; 
                margin: 0; 
                padding: 1rem; 
                display: flex; 
                align-items: center; 
                justify-content: center; 
                min-height: 100vh; 
                text-align: center;
            }
            #status-container {
                padding: 2rem;
                border-radius: 1rem;
                background-color: #ffffff;
                box-shadow: 0 4px 20px rgba(0,0,0,0.1);
            }
            #status-text {
                font-size: 2.5rem;
                font-weight: 700;
            }
            #status-description {
                font-size: 1rem;
                color: #666666;
                margin-top: 0.5rem;
            }
        </style>
    </head>
    <body>
        <div id="status-container">
            <h1 id="status-text">Caricamento...</h1>
            <p id="status-description">In attesa di dati dal server.</p>
        </div>

        <script>
            function showError(message) {
                const statusText = document.getElementById('status-text');
                const statusDescription = document.getElementById('status-description');
                statusText.textContent = "Errore";
                statusDescription.textContent = message;
                statusText.style.color = '#ef4444';
            }

            async function initializeApp() {
                try {
                    const tg = window.Telegram.WebApp;
                    if (!tg || !tg.initDataUnsafe) {
                        throw new Error("Ambiente Telegram non pronto.");
                    }

                    const userId = tg.initDataUnsafe.user?.id;
                    if (!userId) {
                        throw new Error("ID utente non disponibile.");
                    }
                    
                    tg.ready();
                    tg.expand();

                    const response = await fetch(`/api/status/device1/${userId}`);
                    if (!response.ok) {
                        throw new Error(`Errore di rete: ${response.status}`);
                    }
                    
                    const data = await response.json();
                    
                    const statusText = document.getElementById('status-text');
                    const statusDescription = document.getElementById('status-description');
                    
                    if (data.status === 'clear') {
                        statusText.textContent = 'Libero';
                        statusText.style.color = '#22c55e';
                        statusDescription.textContent = 'Il bagno è disponibile.';
                    } else if (data.status === 'detected') {
                        statusText.textContent = 'Occupato';
                        statusText.style.color = '#ef4444';
                        statusDescription.textContent = 'Il bagno è attualmente occupato.';
                    } else {
                        statusText.textContent = 'Sconosciuto';
                        statusText.style.color = '#6b7280';
                        statusDescription.textContent = 'Lo stato del sensore non è noto.';
                    }

                } catch (e) {
                    showError(e.message);
                }
            }

            function waitForTelegram() {
                let attempts = 0;
                const maxAttempts = 100; // 5 secondi di timeout

                const interval = setInterval(() => {
                    if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initDataUnsafe) {
                        clearInterval(interval);
                        initializeApp();
                    } else {
                        attempts++;
                        if (attempts >= maxAttempts) {
                            clearInterval(interval);
                            showError("Timeout: Impossibile caricare i dati da Telegram.");
                        }
                    }
                }, 50);
            }

            waitForTelegram();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    logger.info("Starting server for local development...")
    uvicorn.run(app, host="0.0.0.0", port=8000)