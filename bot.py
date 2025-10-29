#!/usr/bin/env python3
# webhook-ready advanced forward bot for python-telegram-bot
# (copy this entire file into ~/tg_forward_bot/bot.py)

import os
import json
import re
import time
from datetime import datetime
from threading import Lock
# import imghdr  ← यह लाइन हटा दी गई है
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

# ------------------------------------------
CONFIG_FILE = "config.json"
CONFIG_LOCK = Lock()
PENDING_MESSAGES = []
# ------------------------------------------

def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_FILE):
            default = {
                "bot_token": "",
                "source_chat": "",
                "admin_id": None,
                "mode": "forward",
                "header": "",
                "footer": "",
                "replace_text": "",
                "block_links": False,
                "block_usernames": False,
                "delay_seconds": 0,
                "autodelete_seconds": 0
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(default, f, indent=4)
            return default
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)

def save_config(config):
    with CONFIG_LOCK:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)

def start(update, context):
    update.message.reply_text("🤖 Bot is running successfully!")

def main():
    config = load_config()
    bot_token = config.get("bot_token")

    if not bot_token:
        print("❌ Please set your bot_token in config.json")
        return

    updater = Updater(bot_token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))

    print("✅ Bot started successfully!")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
