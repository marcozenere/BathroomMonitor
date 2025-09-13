import os
import asyncio
import logging
from contextlib import asynccontextmanager

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
# Fail fast if essential configs are missing.
try:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    # Render provides the REDIS_URL when you create a Redis service.
    # Fallback to localhost for local development.
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
except KeyError as e:
    raise RuntimeError(f"Missing essential environment variable: {e}") from e

# This is a critical debugging step to see which URL is being used.
logger.info(f"Attempting to connect to Redis at: {REDIS_URL}")

# Render provides this URL automatically.
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

# MQTT Config
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
DEVICE_ID = os.getenv("DEVICE_ID", "device1")
MQTT_SENSOR_TOPIC = f"esp32/{DEVICE_ID}/sensor"

# Webhook Config
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else None

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
                # Use a nested context manager for messages to fix the __aiter__ error.
                async with client.messages() as messages:
                    async for message in messages:
                        payload = message.payload.decode().strip()
                        logger.info(f"[MQTT] {message.topic}: {payload}")

                        new_state = "detected" if payload == "detected" else "clear"

                        redis_client = app_context.get('redis_client')
                        if not redis_client:
                            logger.error("[MQTT] Redis client not available in context.")
                            continue

                        # Because decode_responses=True, Redis returns strings, not bytes.
                        previous_state = await redis_client.get(REDIS_KEY_STATE) or "unknown"

                        if new_state != previous_state:
                            await redis_client.set(REDIS_KEY_STATE, new_state)
                            logger.info(f"State changed from '{previous_state}' to '{new_state}'")
                            if new_state == "clear":
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
    """Manages application startup and shutdown events."""
    logger.info("Application starting up...")

    # Initialize Redis Client ONCE and store in context
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    # Add a retry mechanism for the initial connection to handle startup delays.
    connected_to_redis = False
    for attempt in range(5):  # Try to connect 5 times
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
    
    # Set an initial state if one doesn't exist, to avoid a null state on first run
    await redis_client.setnx(REDIS_KEY_STATE, "unknown")
    logger.info("Ensured initial Redis state is set.")

    # Explicitly disable the Updater, as it's not needed for a webhook bot.
    # This prevents the AttributeError during initialization on Render.
    bot_app = Application.builder().token(BOT_TOKEN).updater(None).build()
    app_context['bot_app'] = bot_app

    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("checkavailability", checkavailability_command))
    bot_app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

    # Initialize bot and set webhook
    await bot_app.initialize()
    if WEBHOOK_URL:
        await bot_app.bot.delete_webhook()
        await bot_app.bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook set to: {WEBHOOK_URL}")

        # Configure the menu button to launch the Mini App
        menu_button = MenuButtonWebApp(
            text="Apri App Bagno",  # Text that appears on the button
            web_app=WebAppInfo(url=RENDER_EXTERNAL_URL) # URL of your web app
        )
        await bot_app.bot.set_chat_menu_button(menu_button=menu_button)
        logger.info("Chat menu button set to launch the Mini App.")
    else:
        logger.warning("RENDER_EXTERNAL_URL not set. Skipping webhook and menu button setup.")


    # Start the MQTT listener as a background task
    mqtt_task = asyncio.create_task(mqtt_listener())
    app_context['mqtt_task'] = mqtt_task
    logger.info("MQTT listener started as a background task.")

    yield  # Application runs here

    logger.info("Application shutting down...")
    await app_context['bot_app'].shutdown()
    await app_context['redis_client'].close()
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
# Pydantic Models for API
# -------------------
class UserAction(BaseModel):
    user_id: int

# -------------------
# TELEGRAM COMMAND HANDLERS
# -------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Ciao! Clicca il tasto 'Menu' per avviare l'app.")

async def checkavailability_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    redis_client = app_context['redis_client']
    # decode_responses=True means this is a string
    current_state = await redis_client.get(REDIS_KEY_STATE) or "unknown"

    if current_state == "detected":
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

# -------------------
# API ENDPOINTS FOR WEB APP
# -------------------
@app.get("/api/status/{user_id}")
async def get_status(user_id: int):
    redis_client = app_context['redis_client']
    # Use a pipeline for efficiency, getting both values in one Redis roundtrip.
    pipe = redis_client.pipeline()
    pipe.get(REDIS_KEY_STATE)
    pipe.sismember(REDIS_KEY_SUBSCRIBERS, str(user_id))
    results = await pipe.execute()
    
    current_state = results[0] or "unknown"
    is_subscribed = bool(results[1])

    return {"status": current_state, "is_subscribed": is_subscribed}


@app.post("/api/subscribe")
async def subscribe_user(user_action: UserAction):
    redis_client = app_context['redis_client']
    await redis_client.sadd(REDIS_KEY_SUBSCRIBERS, str(user_action.user_id))
    logger.info(f"User {user_action.user_id} subscribed via web app.")
    return {"success": True, "message": "Subscribed successfully."}

@app.post("/api/unsubscribe")
async def unsubscribe_user(user_action: UserAction):
    redis_client = app_context['redis_client']
    await redis_client.srem(REDIS_KEY_SUBSCRIBERS, str(user_action.user_id))
    logger.info(f"User {user_action.user_id} unsubscribed via web app.")
    return {"success": True, "message": "Unsubscribed successfully."}


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
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--telegram-bg-color);
                color: var(--telegram-text-color);
                margin: 0;
                padding: 1rem;
                display: flex;
                flex-direction: column;
                align-items: center;
                min-height: 100vh;
                box-sizing: border-box;
            }
            .status-card {
                width: 100%;
                max-width: 400px;
                border-radius: 1rem;
                padding: 2rem;
                text-align: center;
                transition: background-color 0.3s ease, box-shadow 0.3s ease;
                margin-bottom: 2rem;
            }
            .status-card.clear {
                background-color: #e0f8e9;
                box-shadow: 0 4px 20px rgba(45, 212, 111, 0.2);
            }
            .status-card.detected {
                background-color: #ffebee;
                box-shadow: 0 4px 20px rgba(239, 83, 80, 0.2);
            }
            .status-card.unknown {
                background-color: #f3f4f6;
                box-shadow: 0 4px 20px rgba(156, 163, 175, 0.2);
            }
            .status-icon {
                width: 64px;
                height: 64px;
                margin: 0 auto 1rem;
            }
            .status-text {
                font-size: 2rem;
                font-weight: 700;
                margin-bottom: 0.5rem;
            }
            .status-text.clear { color: #22c55e; }
            .status-text.detected { color: #ef4444; }
            .status-text.unknown { color: #6b7280; }
            .status-description {
                color: var(--telegram-hint-color);
                min-height: 20px;
            }
            .action-button {
                display: flex;
                align-items: center;
                justify-content: center;
                width: 100%;
                max-width: 400px;
                padding: 0.8rem 1rem;
                margin-top: 1rem;
                border: none;
                border-radius: 0.75rem;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: background-color 0.2s, transform 0.1s;
                background-color: var(--telegram-button-color);
                color: var(--telegram-button-text-color);
            }
            .action-button:active {
                transform: scale(0.98);
            }
            .action-button:disabled {
                background-color: #d1d5db;
                cursor: not-allowed;
            }
            .action-button svg {
                margin-right: 0.5rem;
            }
            .hidden { display: none; }
            #error-view {
                text-align: center;
                color: #ef4444;
            }
             #loading-view {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                height: 80vh;
            }
            .spinner {
                width: 56px;
                height: 56px;
                border-radius: 50%;
                border: 5px solid #e5e7eb;
                border-top-color: #3b82f6;
                animation: spin 1s linear infinite;
            }
            @keyframes spin {
                to { transform: rotate(360deg); }
            }
        </style>
    </head>
    <body>
        <div id="loading-view">
            <div class="spinner"></div>
            <p style="margin-top: 1rem; color: var(--telegram-hint-color);">Caricamento...</p>
        </div>

        <div id="app-view" class="hidden">
            <div id="status-card" class="status-card">
                <div id="status-icon" class="status-icon">
                    <!-- SVG Icon will be injected here -->
                </div>
                <h1 id="status-text" class="status-text"></h1>
                <p id="status-description" class="status-description"></p>
            </div>

            <button id="subscribe-button" class="action-button hidden">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
                Avvisami quando è libero
            </button>
            <button id="unsubscribe-button" class="action-button hidden">
                 <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.7 3A6 6 0 0 1 18 8a21.3 21.3 0 0 0 3 9H3a21.3 21.3 0 0 0 3-9A6 6 0 0 1 8.7 3"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="m2 2 20 20"/></svg>
                Annulla notifica
            </button>
        </div>

        <div id="error-view" class="hidden">
            <h1>Oops!</h1>
            <p>Questa web app può essere utilizzata solo all'interno di Telegram.</p>
        </div>

        <script>
            const tg = window.Telegram.WebApp;

            // DOM Elements
            const loadingView = document.getElementById('loading-view');
            const appView = document.getElementById('app-view');
            const errorView = document.getElementById('error-view');
            const statusCard = document.getElementById('status-card');
            const statusIcon = document.getElementById('status-icon');
            const statusText = document.getElementById('status-text');
            const statusDescription = document.getElementById('status-description');
            const subscribeButton = document.getElementById('subscribe-button');
            const unsubscribeButton = document.getElementById('unsubscribe-button');
            
            const userId = tg.initDataUnsafe?.user?.id;

            const icons = {
                clear: `<svg class="text-green-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 4h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><path d="M2 20h17"/><path d="M10 12v.01"/></svg>`,
                detected: `<svg class="text-red-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 20V6a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v14"/><path d="M2 20h20"/><path d="M14 12v.01"/></svg>`,
                unknown: `<svg class="text-gray-500" xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>`
            };

            function updateUI(state, isSubscribed) {
                loadingView.classList.add('hidden');
                appView.classList.remove('hidden');

                // Update Card
                statusCard.className = 'status-card ' + state;
                statusIcon.innerHTML = icons[state] || icons.unknown;
                statusText.className = 'status-text ' + state;

                // Update Text and Buttons
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
                try {
                    const response = await fetch(`/api/status/${userId}`);
                    if (!response.ok) throw new Error('Network response was not ok');
                    const data = await response.json();
                    updateUI(data.status, data.is_subscribed);
                } catch (error) {
                    console.error('Failed to fetch status:', error);
                    updateUI('unknown', false);
                }
            }

            async function handleSubscribe() {
                tg.HapticFeedback.impactOccurred('light');
                subscribeButton.disabled = true;
                try {
                    const response = await fetch('/api/subscribe', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ user_id: userId })
                    });
                    if (!response.ok) throw new Error('Subscription failed');
                    tg.HapticFeedback.notificationOccurred('success');
                    await fetchStatus(); // Refresh UI immediately
                } catch (error) {
                    tg.HapticFeedback.notificationOccurred('error');
                } finally {
                     subscribeButton.disabled = false;
                }
            }
            
            async function handleUnsubscribe() {
                tg.HapticFeedback.impactOccurred('light');
                unsubscribeButton.disabled = true;
                try {
                    const response = await fetch('/api/unsubscribe', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ user_id: userId })
                    });
                    if (!response.ok) throw new Error('Unsubscription failed');
                    tg.HapticFeedback.notificationOccurred('success');
                    await fetchStatus(); // Refresh UI immediately
                } catch (error) {
                    tg.HapticFeedback.notificationOccurred('error');
                } finally {
                     unsubscribeButton.disabled = false;
                }
            }

            function initializeApp() {
                if (!userId) {
                    loadingView.classList.add('hidden');
                    appView.classList.add('hidden');
                    errorView.classList.remove('hidden');
                    return;
                }

                tg.ready();
                tg.expand();
                
                // Set theme colors
                document.documentElement.style.setProperty('--telegram-bg-color', tg.themeParams.bg_color || '#ffffff');
                document.documentElement.style.setProperty('--telegram-text-color', tg.themeParams.text_color || '#000000');
                document.documentElement.style.setProperty('--telegram-hint-color', tg.themeParams.hint_color || '#999999');
                document.documentElement.style.setProperty('--telegram-link-color', tg.themeParams.link_color || '#2481cc');
                document.documentElement.style.setProperty('--telegram-button-color', tg.themeParams.button_color || '#2481cc');
                document.documentElement.style.setProperty('--telegram-button-text-color', tg.themeParams.button_text_color || '#ffffff');
                document.documentElement.style.setProperty('--telegram-secondary-bg-color', tg.themeParams.secondary_bg_color || '#f3f3f3');


                subscribeButton.addEventListener('click', handleSubscribe);
                unsubscribeButton.addEventListener('click', handleUnsubscribe);

                fetchStatus();
                setInterval(fetchStatus, 5000); // Poll for status every 5 seconds
            }

            window.addEventListener('load', initializeApp);
        </script>
    </body>
    </html>
    """


# -------------------
# MAIN ENTRY POINT (for local development)
# -------------------
if __name__ == "__main__":
    logger.info("Starting server for local development... (Webhook will not be set)")
    uvicorn.run(app, host="0.0.0.0", port=8000)