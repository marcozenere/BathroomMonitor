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
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #ffffff; color: #000000; margin: 0; padding: 1rem; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; text-align: center; }
            #debug-log { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.8); color: white; font-family: monospace; font-size: 11px; max-height: 200px; overflow-y: scroll; padding: 8px; z-index: 9999; border-top: 1px solid #4a4a4a;}
            #debug-log p { margin: 0; padding: 2px 0; border-bottom: 1px solid #333;}
            .spinner { width: 56px; height: 56px; border-radius: 50%; border: 5px solid #e5e7eb; border-top-color: #3b82f6; animation: spin 1s linear infinite; margin: 0 auto 1rem; }
            @keyframes spin { to { transform: rotate(360deg); } }
        </style>
    </head>
    <body>
        <div id="diagnostic-view">
            <div class="spinner"></div>
            <h1>Diagnostica in corso...</h1>
            <p style="margin-top: 1rem; color: #999999;">Controlla i messaggi nella console di debug in basso.</p>
        </div>
        
        <div id="debug-log"></div>

        <script>
            const debugLog = document.getElementById('debug-log');
            const diagnosticView = document.getElementById('diagnostic-view');

            function logDebug(message) {
                console.log(message);
                const p = document.createElement('p');
                p.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
                debugLog.appendChild(p);
                debugLog.scrollTop = debugLog.scrollHeight;
            }

            function endDiagnostics(success, message) {
                if (success) {
                    diagnosticView.innerHTML = `<h1>Diagnostica Completata con Successo!</h1><p>${message}</p><p>Ora puoi ripristinare il file main.py precedente.</p>`;
                } else {
                    diagnosticView.innerHTML = `<h1>Diagnostica Fallita</h1><p>${message}</p>`;
                }
            }

            try {
                logDebug("--- Inizio Diagnostica Script ---");

                if (window) {
                    logDebug("1. Oggetto 'window' trovato.");
                } else {
                    logDebug("1. ERRORE: Oggetto 'window' NON trovato.");
                    throw new Error("Ambiente base non valido: 'window' non esiste.");
                }

                if (window.Telegram) {
                    logDebug("2. Oggetto 'window.Telegram' trovato.");
                } else {
                    logDebug("2. ERRORE: Oggetto 'window.Telegram' NON trovato.");
                    throw new Error("'window.Telegram' non trovato. Lo script telegram-web-app.js non è stato caricato o è stato bloccato.");
                }
                
                if (window.Telegram.WebApp) {
                    logDebug("3. Oggetto 'window.Telegram.WebApp' trovato.");
                } else {
                    logDebug("3. ERRORE: Oggetto 'window.Telegram.WebApp' NON trovato.");
                    throw new Error("'window.Telegram.WebApp' non trovato. L'inizializzazione dello script di Telegram è fallita.");
                }
                
                const tg = window.Telegram.WebApp;

                // A volte initDataUnsafe può essere vuoto all'inizio
                if (tg.initDataUnsafe) {
                    logDebug("4. Oggetto 'tg.initDataUnsafe' trovato.");
                } else {
                    logDebug("4. ATTENZIONE: 'tg.initDataUnsafe' è vuoto. Questo può essere normale all'avvio.");
                }

                if (tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) {
                    logDebug("5. Oggetto 'tg.initDataUnsafe.user' e ID trovati.");
                    const userId = tg.initDataUnsafe.user.id;
                    logDebug(`6. SUCCESSO: ID Utente = ${userId}`);
                    endDiagnostics(true, "L'ambiente Telegram è stato caricato correttamente.");
                } else {
                     logDebug("5. ERRORE: 'tg.initDataUnsafe.user' o il suo ID non sono stati trovati.");
                     throw new Error("'tg.initDataUnsafe.user.id' non trovato. L'app non ha ricevuto i dati utente da Telegram.");
                }

                logDebug("--- Fine Diagnostica Script ---");

            } catch (e) {
                logDebug(`ERRORE CATTURATO: ${e.message}`);
                endDiagnostics(false, e.message);
            }
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    logger.info("Starting server for local development...")
    uvicorn.run(app, host="0.0.0.0", port=8000)