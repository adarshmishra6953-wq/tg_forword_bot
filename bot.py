#!/usr/bin/env python3
# bot.py - Render / Railway friendly forward bot (python-telegram-bot v13.15)

import os
import json
import re
import time
from datetime import datetime
from threading import Lock

# imghdr shim (our file) will be imported by telegram when needed,
# no direct import needed here.

from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# -------------------------
CONFIG_FILE = "config.json"
CONFIG_LOCK = Lock()
PENDING_MESSAGES = []
# -------------------------

def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_FILE):
            default = {
                "bot_token": "",
                "source_chat": "",
                "target_chat": "",
                "admin_id": None,
                "mode": "forward",
                "active": True,
                "header": "",
                "footer": "",
                "replace": {},
                "blacklist": [],
                "whitelist": [],
                "block_links": False,
                "block_usernames": False,
                "delay_seconds": 0,
                "autodelete_seconds": 0,
                "schedule_time": ""
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(default, f, indent=2, ensure_ascii=False)
            return default
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)

def save_config(cfg):
    with CONFIG_LOCK:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

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

def process_and_send(bot: Bot, cfg, message):
    try:
        target = cfg.get("target_chat")
        if not target:
            return None

        incoming_text = message.text or message.caption or ""
        if cfg.get("blacklist") and contains_any(incoming_text, cfg["blacklist"]):
            return None
        if cfg.get("whitelist") and not contains_any(incoming_text, cfg["whitelist"]):
            return None
        if cfg.get("block_links") and URL_RE.search(incoming_text):
            return None
        if cfg.get("block_usernames") and MENTION_RE.search(incoming_text):
            return None

        # if mode is forward and no header/footer/replace -> do real forward
        mode = cfg.get("mode", "forward")
        header = cfg.get("header", "") or ""
        footer = cfg.get("footer", "") or ""
        replace_map = cfg.get("replace", {}) or {}

        if mode == "forward" and not any([header, footer, replace_map]):
            return bot.forward_message(chat_id=target, from_chat_id=message.chat.id, message_id=message.message_id)

        # prepare caption/text
        final_text = apply_replacements(incoming_text, replace_map)
        caption = "\n".join(x for x in [header, final_text, footer] if x).strip()

        if message.photo:
            return bot.send_photo(chat_id=target, photo=message.photo[-1].file_id, caption=caption)
        if message.video:
            return bot.send_video(chat_id=target, video=message.video.file_id, caption=caption)
        if message.document:
            return bot.send_document(chat_id=target, document=message.document.file_id, caption=caption)
        if message.audio:
            return bot.send_audio(chat_id=target, audio=message.audio.file_id, caption=caption)
        if message.voice:
            return bot.send_voice(chat_id=target, voice=message.voice.file_id, caption=caption)
        if message.sticker:
            return bot.send_sticker(chat_id=target, sticker=message.sticker.file_id)
        if message.location:
            return bot.send_location(chat_id=target, latitude=message.location.latitude, longitude=message.location.longitude)
        if message.contact:
            v = message.contact
            info = f"Contact: {v.first_name or ''} {v.last_name or ''}\nPhone: {v.phone_number}"
            return bot.send_message(chat_id=target, text=(caption + "\n" + info).strip() if caption else info)
        if message.text:
            return bot.send_message(chat_id=target, text=caption or message.text)
        # fallback
        try:
            return bot.copy_message(chat_id=target, from_chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            return None
    except Exception as e:
        print("Send error:", e)
        return None

def message_router(update: Update, context: CallbackContext):
    cfg = load_config()
    if not cfg.get("active", True):
        return
    src = cfg.get("source_chat", "")
    if not src:
        return
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    chat = update.effective_chat
    # allow @username or id
    if src.startswith("@"):
        if (chat.username or "").lower() != src[1:].lower():
            return
    else:
        try:
            if int(src) != chat.id:
                return
        except Exception:
            if (chat.username or "").lower() != str(src).lower():
                return

    if cfg.get("schedule_time"):
        PENDING_MESSAGES.append(msg)
        return

    delay = int(cfg.get("delay_seconds", 0) or 0)
    if delay > 0:
        context.job_queue.run_once(lambda c: process_and_send(context.bot, cfg, msg), delay)
    else:
        process_and_send(context.bot, cfg, msg)

def schedule_checker(context: CallbackContext):
    cfg = load_config()
    sched = cfg.get("schedule_time", "")
    if not sched:
        return
    now = datetime.now().strftime("%H:%M")
    if now == sched and PENDING_MESSAGES:
        pending = list(PENDING_MESSAGES)
        PENDING_MESSAGES.clear()
        for msg in pending:
            process_and_send(context.bot, cfg, msg)

def is_admin(update: Update):
    cfg = load_config()
    adm = cfg.get("admin_id")
    if adm is None:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid == adm

# admin commands (kept minimal for stability)
def cmd_start(update: Update, context: CallbackContext):
    cfg = load_config()
    if cfg.get("admin_id") is None:
        cfg["admin_id"] = update.effective_user.id
        save_config(cfg)
    update.message.reply_text("बॉट चालू है। /help देखें।")

def cmd_help(update: Update, context: CallbackContext):
    txt = ("/set_source @channel_or_id\n/set_target @channel_or_id\n/mode forward|copy\n/pause\n/resume\n/status\n/config")
    update.message.reply_text(txt)

def cmd_status(update: Update, context: CallbackContext):
    cfg = load_config()
    st = "चालू" if cfg.get("active", True) else "बंद"
    summary = {"active": st, "source": cfg.get("source_chat"), "target": cfg.get("target_chat"), "mode": cfg.get("mode")}
    update.message.reply_text(json.dumps(summary, indent=2, ensure_ascii=False))

# webhook main
def run_webhook():
    cfg = load_config()
    token = os.environ.get("BOT_TOKEN") or cfg.get("bot_token")
    if not token:
        print("कृपया BOT_TOKEN env में डालें या config.json में bot_token भरें।")
        return

    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher
    jq = updater.job_queue

    # register handlers
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(MessageHandler(Filters.all, message_router))

    jq.run_repeating(schedule_checker, interval=60, first=30)

    port = int(os.environ.get("PORT", "8443"))
    public_url = os.environ.get("WEBHOOK_URL")  # e.g. https://<your-render-url>
    path = token

    updater.start_webhook(listen="0.0.0.0", port=port, url_path=path)
    if public_url:
        webhook_url = f"{public_url}/{path}"
        try:
            updater.bot.set_webhook(webhook_url)
            print("Webhook सेट कर दिया गया:", webhook_url)
        except Exception as e:
            print("Webhook सेट में error:", e)
    else:
        print("WEBHOOK_URL env var नहीं मिली — Render में सेट करो।")
    updater.idle()

if __name__ == "__main__":
    run_webhook()
