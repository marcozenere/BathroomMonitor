from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
from collections import deque
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------
# CONFIG
# -------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Telegram bot token not found in environment variables!")

MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
DEVICE_ID = os.getenv("DEVICE_ID", "device1")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"  # unique path
WEBHOOK_URL = f"https://<your-render-url>{WEBHOOK_PATH}"

# -------------------
# FASTAPI
# -------------------
app = FastAPI()

# Safe static mounting
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Safe templates setup
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir) if os.path.isdir(templates_dir) else None

# -------------------
# MQTT SETUP
# -------------------
mqtt_client = mqtt.Client()
current_state = "unknown"   # "detected" / "clear"
subscribers = deque()       # FIFO queue of chat IDs

def on_connect(client, userdata, flags, rc):
    print("[MQTT] Connected with result code", rc)
    client.subscribe(f"esp32/{DEVICE_ID}/sensor")

def on_message(client, userdata, msg):
    global current_state
    payload = msg.payload.decode().strip()
    print(f"[MQTT] {msg.topic}: {payload}")

    new_state = "detected" if payload == "detected" else "clear"

    if new_state != current_state:
        current_state = new_state
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(notify_subscribers(new_state))
        except RuntimeError:
            asyncio.run(notify_subscribers(new_state))

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

# -------------------
# TELEGRAM BOT SETUP
# -------------------
bot_app = Application.builder().token(BOT_TOKEN).build()

# ----- COMMAND HANDLERS -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Ciao! Usa il tasto menu per iniziare.")

async def checkavailability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    print(f"[DEBUG] /status received from chat {chat_id}")

    if current_state == "detected":
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Bagno occupato! Posso inviarti una notifica nel momento in cui si libera. Procedo?"
        )
        if chat_id not in subscribers:
            subscribers.append(chat_id)
    elif current_state == "clear":
        await context.bot.send_message(chat_id=chat_id, text="✅ Bagno libero! Corri.")
    else:
        await context.bot.send_message(chat_id=chat_id, text="❓ Errore nella lettura del sensore! Contatta l'amministratore nel caso l'errore persistesse.")

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        await update.message.reply_text("🛑 Non riceverai più notifiche.")
    else:
        await update.message.reply_text("ℹ️ Non eri iscritto alle notifiche.")

async def notifyme(state: str):
    message = "✅ Bagno libero! Corri. " if state == "clear" else "⚠️ Bagno occupato! Ti avviserò quando sarà libero."
    while subscribers:
        chat_id = subscribers.popleft()
        try:
            await bot_app.bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            print(f"[Bot] Failed to notify {chat_id}: {e}")

# Add command handlers
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("checkavailability", checkavailability))
bot_app.add_handler(CommandHandler("notifyme", notifyme))
bot_app.add_handler(CommandHandler("unsubscribe", unsubscribe))

# -------------------
# WEBHOOK ENDPOINT
# -------------------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.update_queue.put(update)
    return {"ok": True}

# -------------------
# SET WEBHOOK ON STARTUP
# -------------------
@app.on_event("startup")
async def setup_webhook():
    await bot_app.initialize()
    # Remove any previous webhook
    await bot_app.bot.delete_webhook()
    # Set new webhook
    await bot_app.bot.set_webhook(WEBHOOK_URL)
    print(f"✅ Webhook set: {WEBHOOK_URL}")