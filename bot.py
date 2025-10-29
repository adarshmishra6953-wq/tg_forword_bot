#!/usr/bin/env python3
# webhook-ready Telegram forward/copy bot
# compatible with python-telegram-bot==13.15  (Render/Railway ready)

import os
import json
import re
import time
from datetime import datetime
from threading import Lock
import imghdr
from telegram import Bot, Update
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, CallbackContext
)

CONFIG_FILE = "config.json"
CONFIG_LOCK = Lock()
PENDING_MESSAGES = []

# -------------------------------------------------------
def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_FILE):
            default = {
                "bot_token": "", "source_chat": "", "target_chat": "",
                "admin_id": None, "mode": "forward", "active": True,
                "header": "", "footer": "", "replace": {}, "blacklist": [],
                "whitelist": [], "block_links": False, "block_usernames": False,
                "delay_seconds": 0, "autodelete_seconds": 0, "schedule_time": ""
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(default, f, indent=4, ensure_ascii=False)
            return default
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)

def save_config(cfg):
    with CONFIG_LOCK:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)

# -------------------------------------------------------
URL_RE = re.compile(r"https?://\S+|www\.\S+")
MENTION_RE = re.compile(r"@\w+")

def contains_any(text, words):
    if not text or not words:
        return False
    t = text.lower()
    for w in words:
        if w.strip().lower() in t:
            return True
    return False

def apply_replacements(text, replace_map):
    if not text or not replace_map:
        return text
    out = text
    for old, new in replace_map.items():
        out = out.replace(old, new)
    return out

# -------------------------------------------------------
def process_and_send(bot: Bot, cfg, message):
    try:
        target = cfg.get("target_chat")
        if not target:
            return
        mode = cfg.get("mode", "forward")
        header = cfg.get("header", "") or ""
        footer = cfg.get("footer", "") or ""
        replace_map = cfg.get("replace", {}) or {}

        incoming_text = message.text or message.caption or ""
        if cfg.get("blacklist") and contains_any(incoming_text, cfg["blacklist"]):
            return
        if cfg.get("whitelist") and not contains_any(incoming_text, cfg["whitelist"]):
            return
        if cfg.get("block_links") and URL_RE.search(incoming_text):
            return
        if cfg.get("block_usernames") and MENTION_RE.search(incoming_text):
            return

        if mode == "forward" and not any([header, footer, replace_map]):
            bot.forward_message(chat_id=target, from_chat_id=message.chat.id,
                                message_id=message.message_id)
            return

        final_text = apply_replacements(incoming_text, replace_map)
        caption = "\n".join(x for x in [header, final_text, footer] if x).strip()

        if message.photo:
            bot.send_photo(target, photo=message.photo[-1].file_id, caption=caption)
        elif message.video:
            bot.send_video(target, video=message.video.file_id, caption=caption)
        elif message.document:
            bot.send_document(target, document=message.document.file_id, caption=caption)
        elif message.audio:
            bot.send_audio(target, audio=message.audio.file_id, caption=caption)
        elif message.voice:
            bot.send_voice(target, voice=message.voice.file_id, caption=caption)
        elif message.sticker:
            bot.send_sticker(target, sticker=message.sticker.file_id)
        elif message.text:
            bot.send_message(target, text=caption)
        else:
            bot.copy_message(target, from_chat_id=message.chat.id,
                             message_id=message.message_id)
    except Exception as e:
        print("Send error:", e)

# -------------------------------------------------------
def message_router(update: Update, context: CallbackContext):
    cfg = load_config()
    if not cfg.get("active", True):
        return
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    src = cfg.get("source_chat", "")
    chat = update.effective_chat

    if src:
        try:
            if str(chat.id) != str(src).lstrip("@"):
                if chat.username and f"@{chat.username.lower()}" != src.lower():
                    return
        except:
            return

    if cfg.get("schedule_time"):
        PENDING_MESSAGES.append(msg)
        return

    delay = int(cfg.get("delay_seconds", 0))
    if delay > 0:
        context.job_queue.run_once(lambda c: process_and_send(context.bot, cfg, msg), delay)
    else:
        process_and_send(context.bot, cfg, msg)

# -------------------------------------------------------
def schedule_checker(context: CallbackContext):
    cfg = load_config()
    sched = cfg.get("schedule_time")
    if not sched:
        return
    now = datetime.now().strftime("%H:%M")
    if now == sched:
        for msg in list(PENDING_MESSAGES):
            process_and_send(context.bot, cfg, msg)
        PENDING_MESSAGES.clear()

# -------------------------------------------------------
def is_admin(update: Update):
    cfg = load_config()
    adm = cfg.get("admin_id")
    uid = update.effective_user.id if update.effective_user else None
    return adm is None or uid == adm

def cmd_start(update, context):
    cfg = load_config()
    if cfg.get("admin_id") is None:
        cfg["admin_id"] = update.effective_user.id
        save_config(cfg)
    update.message.reply_text("✅ बॉट चालू है। /help देखें।")

def cmd_help(update, context):
    update.message.reply_text("कमांड्स:\n/set_source\n/set_target\n/mode forward|copy\n/pause\n/resume\n/status")

def cmd_status(update, context):
    cfg = load_config()
    update.message.reply_text(json.dumps(cfg, indent=2, ensure_ascii=False))

# -------------------------------------------------------
def run_webhook():
    cfg = load_config()
    token = os.environ.get("BOT_TOKEN") or cfg.get("bot_token")
    if not token:
        print("❌ BOT_TOKEN env या config.json में नहीं मिला!")
        return

    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher
    jq = updater.job_queue

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(MessageHandler(Filters.all, message_router))

    jq.run_repeating(schedule_checker, 60, first=30)

    port = int(os.environ.get("PORT", "8443"))
    public_url = os.environ.get("WEBHOOK_URL")
    path = token

    updater.start_webhook(listen="0.0.0.0", port=port, url_path=path)
    if public_url:
        webhook_url = f"{public_url}/{path}"
        updater.bot.set_webhook(webhook_url)
        print("✅ Webhook सेट:", webhook_url)
    else:
        print("⚠️ WEBHOOK_URL env नहीं मिला!")
    updater.idle()

if __name__ == "__main__":
    run_webhook()
