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
            .action-button:active { transform: scale(0.98); }
            .action-button:disabled { background-color: #d1d5db; cursor: not-allowed; }
            .action-button svg { margin-right: 0.5rem; }
            .hidden { display: none; }
            #error-view { color: #ef4444; background-color: #ffebee; padding: 1rem; border-radius: 0.5rem; text-align: center; }
        </style>
    </head>
    <body>
        <div id="app-view" class="w-full max-w-md flex flex-col items-center">
            <div id="status-card" class="status-card unknown">
                <div id="status-icon" class="status-icon">
                    <svg class="text-gray-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>
                </div>
                <h1 id="status-text" class="status-text unknown">Caricamento...</h1>
                <p id="status-description" class="status-description">Recupero informazioni...</p>
            </div>
            <button id="subscribe-button" class="action-button hidden">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
                Avvisami quando è libero
            </button>
            <button id="unsubscribe-button" class="action-button hidden">
                 <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.7 3A6 6 0 0 1 18 8a21.3 21.3 0 0 0 3 9H3a21.3 21.3 0 0 0 3-9A6 6 0 0 1 8.7 3"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="m2 2 20 20"/></svg>
                Annulla notifica
            </button>
        </div>

        <div id="error-view" class="hidden">
            <h3>Si è verificato un errore</h3>
            <p id="error-message" style="margin-top: 0.5rem; font-family: monospace; font-size: 0.8rem;"></p>
        </div>

        <script>
            window.onerror = function(message, source, lineno, colno, error) {
                showError('Errore non gestito: ' + message);
                return true;
            };

            const deviceId = "device1";

            // DOM Elements
            const appView = document.getElementById('app-view');
            const errorView = document.getElementById('error-view');
            const errorMessage = document.getElementById('error-message');
            const statusCard = document.getElementById('status-card');
            const statusIcon = document.getElementById('status-icon');
            const statusText = document.getElementById('status-text');
            const statusDescription = document.getElementById('status-description');
            const subscribeButton = document.getElementById('subscribe-button');
            const unsubscribeButton = document.getElementById('unsubscribe-button');

            const icons = {
                clear: `<svg class="text-green-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 4h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><path d="M2 20h17"/><path d="M10 12v.01"/></svg>`,
                detected: `<svg class="text-red-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 20V6a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v14"/><path d="M2 20h20"/><path d="M14 12v.01"/></svg>`,
                unknown: `<svg class="text-gray-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>`
            };
            
            function showError(message) {
                appView.classList.add('hidden');
                errorView.classList.remove('hidden');
                errorMessage.textContent = message;
            }

            function updateUI(state, isSubscribed) {
                errorView.classList.add('hidden');
                appView.classList.remove('hidden');

                statusCard.className = 'status-card ' + state;
                statusIcon.innerHTML = icons[state] || icons.unknown;
                statusText.className = 'status-text ' + state;

                subscribeButton.classList.add('hidden');
                unsubscribeButton.classList.add('hidden');

                if (state === 'clear') {
                    statusText.textContent = 'Libero';
                    statusDescription.textContent = 'Il bagno è disponibile. Corri!';
                } else if (state === 'detected') {
                    statusText.textContent = 'Occupato';
                    if (isSubscribed) {
                        statusDescription.textContent = 'Sei in lista. Riceverai una notifica.';
                        unsubscribeButton.classList.remove('hidden');
                    } else {
                        statusDescription.textContent = 'Qualcuno è dentro. Vuoi essere avvisato?';
                        subscribeButton.classList.remove('hidden');
                    }
                } else {
                    statusText.textContent = 'Sconosciuto';
                    statusDescription.textContent = 'Non riesco a determinare lo stato del bagno.';
                }
            }

            async function fetchStatus() {
                const tg = window.Telegram.WebApp;
                const userId = tg.initDataUnsafe?.user?.id;
                if (!userId) {
                    throw new Error("ID utente non trovato in initDataUnsafe.");
                }
                
                const response = await fetch(`/api/status/${deviceId}/${userId}`);
                if (!response.ok) {
                    console.error(`Errore di rete: ${response.status} ${response.statusText}`);
                    return; 
                }
                const data = await response.json();
                updateUI(data.status, data.is_subscribed);
            }

            async function handleUserAction(endpoint) {
                const tg = window.Telegram.WebApp;
                const userId = tg.initDataUnsafe?.user?.id;
                tg.HapticFeedback.impactOccurred('light');
                subscribeButton.disabled = true;
                unsubscribeButton.disabled = true;
                try {
                    const response = await fetch(endpoint, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ user_id: userId, device_id: deviceId })
                    });
                    if (!response.ok) throw new Error('Azione fallita');
                    tg.HapticFeedback.notificationOccurred('success');
                    await fetchStatus();
                } catch (error) {
                    tg.HapticFeedback.notificationOccurred('error');
                    showError(`Fallimento azione: ${error.message}`);
                } finally {
                    subscribeButton.disabled = false;
                    unsubscribeButton.disabled = false;
                }
            }

            function initializeApp() {
                try {
                    const tg = window.Telegram.WebApp;

                    const userId = tg.initDataUnsafe.user?.id;
                    if (!userId) {
                        throw new Error("I dati utente (initDataUnsafe.user) non sono disponibili.");
                    }

                    tg.ready();
                    tg.expand();
                    
                    document.documentElement.style.setProperty('--telegram-bg-color', tg.themeParams.bg_color || '#ffffff');
                    document.documentElement.style.setProperty('--telegram-text-color', tg.themeParams.text_color || '#000000');
                    document.documentElement.style.setProperty('--telegram-hint-color', tg.themeParams.hint_color || '#999999');
                    document.documentElement.style.setProperty('--telegram-link-color', tg.themeParams.link_color || '#2481cc');
                    document.documentElement.style.setProperty('--telegram-button-color', tg.themeParams.button_color || '#2481cc');
                    document.documentElement.style.setProperty('--telegram-button-text-color', tg.themeParams.button_text_color || '#ffffff');
                    document.documentElement.style.setProperty('--telegram-secondary-bg-color', tg.themeParams.secondary_bg_color || '#f3f3f3');

                    subscribeButton.addEventListener('click', () => handleUserAction('/api/subscribe'));
                    unsubscribeButton.addEventListener('click', () => handleUserAction('/api/unsubscribe'));
                    
                    fetchStatus().catch(err => showError(`Fallimento caricamento iniziale: ${err.message}`));
                    
                    setInterval(() => {
                        fetchStatus().catch(err => console.error("Errore durante l'aggiornamento automatico:", err));
                    }, 5000);

                } catch(e) {
                    showError(`Errore durante l'inizializzazione: ${e.message}`);
                }
            }

            function waitForTelegram() {
                let attempts = 0;
                const maxAttempts = 100;

                const interval = setInterval(() => {
                    if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initDataUnsafe && window.Telegram.WebApp.initDataUnsafe.user) {
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