import os
import logging
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, MessageHandler, Filters, CallbackContext
from apscheduler.schedulers.background import BackgroundScheduler

# 🔹 Flask initialize
app = Flask(__name__)

# 🔹 Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 🔹 Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "0"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # your Render URL (without trailing slash)

if not BOT_TOKEN:
    raise ValueError("⚠️ BOT_TOKEN missing in environment variables!")

bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=0)


# 🔹 Forward message
def forward_message(update: Update, context: CallbackContext):
    try:
        if update.message and update.message.chat.id == SOURCE_CHAT_ID:
            update.message.forward(chat_id=TARGET_CHAT_ID)
            logger.info("✅ Message forwarded successfully.")
    except Exception as e:
        logger.error(f"❌ Forward Error: {e}")


dispatcher.add_handler(MessageHandler(Filters.all, forward_message))


# 🔹 Flask webhook route
@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok", 200


# 🔹 Keep-alive scheduler
scheduler = BackgroundScheduler()
scheduler.start()


@app.route("/")
def home():
    return "🤖 Bot is alive and running!", 200


if __name__ == "__main__":
    # Webhook set on startup
    if WEBHOOK_URL:
        bot.delete_webhook()
        bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info("🚀 Webhook set successfully!")
    else:
        logger.warning("⚠️ WEBHOOK_URL not found!")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
