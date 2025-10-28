#!/usr/bin/env python3
# webhook-ready advanced forward bot for python-telegram-bot v13.15
# (copy this entire file into ~/tg_forward_bot/bot.py)
# updated for imghdr fix
import os
import json
import re
import time
from datetime import datetimeR
from threading import Lock
import imghdr
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# --------------------------------
CONFIG_FILE = "config.json"
CONFIG_LOCK = Lock()
PENDING_MESSAGES = []
# --------------------------------

def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_FILE):
            default = {
                "bot_token": "", "source_chat": "", "target_chat": "",
                "admin_id": None, "mode": "forward", "active": True,
                "header": "", "footer": "", "replace": {}, "blacklist": [], "whitelist": [],
                "block_links": False, "block_usernames": False,
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
    target = cfg.get("target_chat")
    if not target:
        return None
    mode = cfg.get("mode", "forward")
    header = cfg.get("header", "") or ""
    footer = cfg.get("footer", "") or ""
    replace_map = cfg.get("replace", {}) or {}

    incoming_text = message.text or message.caption or ""
    if cfg.get("blacklist"):
        if contains_any(incoming_text, cfg.get("blacklist")):
            return None
    if cfg.get("whitelist"):
        if not contains_any(incoming_text, cfg.get("whitelist")):
            return None
    if cfg.get("block_links") and URL_RE.search(incoming_text):
        return None
    if cfg.get("block_usernames") and MENTION_RE.search(incoming_text):
        return None

    try:
        if mode == "forward" and not header and not footer and not replace_map:
            return bot.forward_message(chat_id=target, from_chat_id=message.chat.id, message_id=message.message_id)

        final_text = incoming_text
        if isinstance(final_text, str) and replace_map:
            final_text = apply_replacements(final_text, replace_map)
        caption = ""
        if final_text:
            caption = ("\n".join([header, final_text, footer]).strip()).strip()
        else:
            caption = ("\n".join([header, footer]).strip()).strip()
        if caption == "":
            caption = None

        if message.photo:
            file_id = message.photo[-1].file_id
            return bot.send_photo(chat_id=target, photo=file_id, caption=caption)
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
        if message.contact:
            v = message.contact
            info = f"Contact: {v.first_name or ''} {v.last_name or ''}\nPhone: {v.phone_number}"
            return bot.send_message(chat_id=target, text=(caption + "\n" + info).strip() if caption else info)
        if message.location:
            return bot.send_location(chat_id=target, latitude=message.location.latitude, longitude=message.location.longitude)
        if message.text:
            return bot.send_message(chat_id=target, text=caption or message.text)
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

    schedule_time = cfg.get("schedule_time", "") or ""
    if schedule_time:
        PENDING_MESSAGES.append(msg)
        return

    delay = int(cfg.get("delay_seconds", 0) or 0)
    if delay > 0:
        context.job_queue.run_once(lambda c: scheduled_send(c, msg), delay)
    else:
        sent = process_and_send(context.bot, cfg, msg)
        if sent and int(cfg.get("autodelete_seconds", 0) or 0) > 0:
            ad = int(cfg.get("autodelete_seconds", 0))
            context.job_queue.run_once(lambda c, s=sent: c.bot.delete_message(chat_id=s.chat.id, message_id=s.message_id), ad)

def scheduled_send(context: CallbackContext, message):
    cfg = load_config()
    sent = process_and_send(context.bot, cfg, message)
    if sent and int(cfg.get("autodelete_seconds", 0) or 0) > 0:
        ad = int(cfg.get("autodelete_seconds", 0))
        context.job_queue.run_once(lambda c, s=sent: c.bot.delete_message(chat_id=s.chat.id, message_id=s.message_id), ad)

def schedule_checker(context: CallbackContext):
    cfg = load_config()
    sched = cfg.get("schedule_time", "")
    if not sched:
        return
    now = datetime.now().strftime("%H:%M")
    if now == sched:
        global PENDING_MESSAGES
        pending = list(PENDING_MESSAGES)
        PENDING_MESSAGES = []
        for msg in pending:
            d = int(cfg.get("delay_seconds", 0) or 0)
            if d > 0:
                time.sleep(d)
            sent = process_and_send(context.bot, cfg, msg)
            if sent and int(cfg.get("autodelete_seconds", 0) or 0) > 0:
                ad = int(cfg.get("autodelete_seconds", 0))
                context.job_queue.run_once(lambda c, s=sent: c.bot.delete_message(chat_id=s.chat.id, message_id=s.message_id), ad)

def is_admin(update: Update):
    cfg = load_config()
    adm = cfg.get("admin_id")
    if adm is None:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid == adm

# Command handlers (same as earlier; mutating commands auto-save)
def cmd_start(update: Update, context: CallbackContext):
    cfg = load_config()
    if cfg.get("admin_id") is None:
        cfg["admin_id"] = update.effective_user.id
        save_config(cfg)
    update.message.reply_text("बॉट चालू है। /help देखें।")

def cmd_help(update: Update, context: CallbackContext):
    txt = ("कमांड्स (admin only):\n"
           "/set_source @channel_or_id\n"
           "/set_target @channel_or_id\n"
           "/mode forward|copy\n"
           "/set_header <text>\n"
           "/set_footer <text>\n"
           "/replace old|new\n"
           "/replace_clear\n"
           "/blacklist w1,w2\n"
           "/whitelist w1,w2\n"
           "/block_links on|off\n"
           "/block_usernames on|off\n"
           "/delay seconds\n"
           "/autodelete seconds\n"
           "/setschedule HH:MM\n"
           "/pause\n"
           "/resume\n"
           "/config\n"
           "/status\n")
    update.message.reply_text(txt)

def cmd_status(update: Update, context: CallbackContext):
    cfg = load_config()
    st = "चालू" if cfg.get("active", True) else "बंद"
    summary = {"active": st, "source": cfg.get("source_chat"), "target": cfg.get("target_chat"),
               "mode": cfg.get("mode"), "header_set": bool(cfg.get("header")), "delay_sec": cfg.get("delay_seconds"),
               "autodelete_sec": cfg.get("autodelete_seconds"), "schedule": cfg.get("schedule_time")}
    update.message.reply_text(json.dumps(summary, indent=2, ensure_ascii=False))

def cmd_config(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    cfg = load_config()
    show = dict(cfg)
    if show.get("bot_token"): show["bot_token"] = "HIDDEN"
    update.message.reply_text("Current config:\n" + json.dumps(show, indent=2, ensure_ascii=False))

def cmd_set_source(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /set_source @channel_or_id"); return
    cfg = load_config(); cfg["source_chat"] = context.args[0]
    if cfg.get("admin_id") is None: cfg["admin_id"] = update.effective_user.id
    save_config(cfg); update.message.reply_text("OK")

def cmd_set_target(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /set_target @channel_or_id"); return
    cfg = load_config(); cfg["target_chat"] = context.args[0]
    if cfg.get("admin_id") is None: cfg["admin_id"] = update.effective_user.id
    save_config(cfg); update.message.reply_text("OK")

def cmd_mode(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /mode forward|copy"); return
    m = context.args[0].lower()
    if m not in ("forward", "copy"): update.message.reply_text("Mode 'forward' या 'copy' होना चाहिए"); return
    cfg = load_config(); cfg["mode"] = m; save_config(cfg); update.message.reply_text("OK")

def cmd_set_header(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    header = " ".join(context.args) if context.args else ""
    cfg = load_config(); cfg["header"] = header; save_config(cfg); update.message.reply_text("OK")

def cmd_set_footer(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    footer = " ".join(context.args) if context.args else ""
    cfg = load_config(); cfg["footer"] = footer; save_config(cfg); update.message.reply_text("OK")

def cmd_replace(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    text = " ".join(context.args)
    if "|" not in text: update.message.reply_text("उपयोग: /replace पुराना|नया"); return
    old, new = [x.strip() for x in text.split("|",1)]
    cfg = load_config(); rep = cfg.get("replace", {}) or {}; rep[old] = new; cfg["replace"] = rep; save_config(cfg); update.message.reply_text("OK")

def cmd_replace_clear(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    cfg = load_config(); cfg["replace"] = {}; save_config(cfg); update.message.reply_text("OK")

def cmd_blacklist(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    text = " ".join(context.args)
    if not text: cfg = load_config(); cfg["blacklist"] = []; save_config(cfg); update.message.reply_text("Blacklist cleared"); return
    words = [w.strip() for w in text.split(",") if w.strip()]; cfg = load_config(); cfg["blacklist"] = words; save_config(cfg); update.message.reply_text("OK")

def cmd_whitelist(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    text = " ".join(context.args)
    if not text: cfg = load_config(); cfg["whitelist"] = []; save_config(cfg); update.message.reply_text("Whitelist cleared"); return
    words = [w.strip() for w in text.split(",") if w.strip()]; cfg = load_config(); cfg["whitelist"] = words; save_config(cfg); update.message.reply_text("OK")

def cmd_block_links(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /block_links on|off"); return
    val = context.args[0].lower() in ("on","true","1","yes"); cfg = load_config(); cfg["block_links"] = val; save_config(cfg); update.message.reply_text("OK")

def cmd_block_usernames(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /block_usernames on|off"); return
    val = context.args[0].lower() in ("on","true","1","yes"); cfg = load_config(); cfg["block_usernames"] = val; save_config(cfg); update.message.reply_text("OK")

def cmd_delay(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /delay seconds"); return
    try: sec = int(context.args[0])
    except: update.message.reply_text("seconds integer होनी चाहिए"); return
    cfg = load_config(); cfg["delay_seconds"] = sec; save_config(cfg); update.message.reply_text("OK")

def cmd_autodelete(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /autodelete seconds"); return
    try: sec = int(context.args[0])
    except: update.message.reply_text("seconds integer होनी चाहिए"); return
    cfg = load_config(); cfg["autodelete_seconds"] = sec; save_config(cfg); update.message.reply_text("OK")

def cmd_setschedule(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    if not context.args: update.message.reply_text("उपयोग: /setschedule HH:MM  (या /setschedule off)"); return
    arg = context.args[0]; cfg = load_config()
    if arg.lower() in ("off","none","disable"): cfg["schedule_time"] = ""; save_config(cfg); update.message.reply_text("OK"); return
    try: datetime.strptime(arg, "%H:%M")
    except: update.message.reply_text("Time format HH:MM (24h) होना चाहिए"); return
    cfg["schedule_time"] = arg; save_config(cfg); update.message.reply_text("OK")

def cmd_pause(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    cfg = load_config(); cfg["active"] = False; save_config(cfg); update.message.reply_text("OK")

def cmd_resume(update: Update, context: CallbackContext):
    if not is_admin(update): update.message.reply_text("आप अधिकृत नहीं हैं."); return
    cfg = load_config(); cfg["active"] = True; save_config(cfg); update.message.reply_text("OK")

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

    # register handlers (same as earlier)
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("status", cmd_status))
    dp.add_handler(CommandHandler("config", cmd_config))
    dp.add_handler(CommandHandler("set_source", cmd_set_source))
    dp.add_handler(CommandHandler("set_target", cmd_set_target))
    dp.add_handler(CommandHandler("mode", cmd_mode))
    dp.add_handler(CommandHandler("set_header", cmd_set_header))
    dp.add_handler(CommandHandler("set_footer", cmd_set_footer))
    dp.add_handler(CommandHandler("replace", cmd_replace))
    dp.add_handler(CommandHandler("replace_clear", cmd_replace_clear))
    dp.add_handler(CommandHandler("blacklist", cmd_blacklist))
    dp.add_handler(CommandHandler("whitelist", cmd_whitelist))
    dp.add_handler(CommandHandler("block_links", cmd_block_links))
    dp.add_handler(CommandHandler("block_usernames", cmd_block_usernames))
    dp.add_handler(CommandHandler("delay", cmd_delay))
    dp.add_handler(CommandHandler("autodelete", cmd_autodelete))
    dp.add_handler(CommandHandler("setschedule", cmd_setschedule))
    dp.add_handler(CommandHandler("pause", cmd_pause))
    dp.add_handler(CommandHandler("resume", cmd_resume))
    dp.add_handler(MessageHandler(Filters.all, message_router))

    # schedule checker
    jq.run_repeating(schedule_checker, interval=60, first=30)

    port = int(os.environ.get("PORT", "8443"))
    public_url = os.environ.get("WEBHOOK_URL")  # e.g. https://your-app.up.railway.app
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
        print("WEBHOOK_URL env var नहीं मिली — Railway में सेट करो।")
    updater.idle()

if __name__ == "__main__":
    run_webhook()
