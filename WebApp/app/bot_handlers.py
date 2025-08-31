
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from . import settings

def build_bot_app(send_command_callable):
    """
    Factory that returns a python-telegram-bot Application with handlers wired
    to the MQTT 'send_command_callable' bridge.
    """
    if not settings.BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")

    app = Application.builder().token(settings.BOT_TOKEN).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Hi! Available commands:\n"
            "/led_on - turn LED on\n"
            "/led_off - turn LED off\n"
            "/ping - test command"
        )

    async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("pong")

    async def led_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
        send_command_callable("led_on")
        await update.message.reply_text("Command sent: led_on")

    async def led_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
        send_command_callable("led_off")
        await update.message.reply_text("Command sent: led_off")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("led_on", led_on))
    app.add_handler(CommandHandler("led_off", led_off))

    return app
