#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render-compatible Telegram Auto-Forward Bot (memory-based)
Usage on local: export BOT_TOKEN="123:ABC..." ; python app.py
On Render: set BOT_TOKEN and DEPLOY_WEBHOOK_URL environment variables.
"""

import os
import logging
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, abort
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
import asyncio

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # <-- DO NOT PUT TOKEN HERE. Set env var on Render.
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable before running.")

WEBHOOK_PATH = f"/{BOT_TOKEN}"
PORT = int(os.environ.get("PORT", "8443"))
DEPLOY_WEBHOOK_URL = os.environ.get("DEPLOY_WEBHOOK_URL")  # e.g., https://my-app.onrender.com

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------- In-memory storage (resets on restart) ----------
state = {
    "channels": {"source": None, "target": None},
    "replacements": [],   # list of dicts: {"id": int, "old": str, "new": str}
    "blocked": [],        # list of dicts: {"id": int, "value": str}
    "header": "",
    "footer": "",
    "enabled": True,
    "blacklist": [],      # list of values
    "whitelist": [],      # list of values
    "schedules": [],      # list of dicts: {"id": int, "chat_id": int, "text": str, "send_at": datetime}
    "forwarded_cache": {}, # unique_key -> timestamp (for duplicate prevention)
    "next_id": 1,
    "logging": False,
    "dup_check": True
}

def next_id():
    i = state["next_id"]
    state["next_id"] += 1
    return i

# ---------- Helper UI builders ----------
def make_main_keyboard():
    kb = [
        [InlineKeyboardButton("Source / Target", callback_data="menu_channels"),
         InlineKeyboardButton("Replacements", callback_data="menu_replacements")],
        [InlineKeyboardButton("Blocked", callback_data="menu_blocked"),
         InlineKeyboardButton("Header / Footer", callback_data="menu_header")],
        [InlineKeyboardButton("Blacklist/Whitelist", callback_data="menu_bw"),
         InlineKeyboardButton("Scheduling", callback_data="menu_schedule")],
        [InlineKeyboardButton("Bot Control", callback_data="menu_control"),
         InlineKeyboardButton("Advanced", callback_data="menu_advanced")],
        [InlineKeyboardButton("Toggle Logging", callback_data="toggle_logging")]
    ]
    return InlineKeyboardMarkup(kb)

def back_button(dest="main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"back|{dest}")]])

# ---------- Utilities ----------
def apply_replacements(text: str) -> str:
    for r in state["replacements"]:
        text = text.replace(r["old"], r["new"])
    return text

def is_blocked_message(text: str):
    for b in state["blocked"]:
        if b["value"] and b["value"] in text:
            return True, b["value"]
    return False, None

def add_forwarded_cache(key: str):
    state["forwarded_cache"][key] = time.time()

def forwarded_cache_exists(key: str, ttl_seconds=3600):
    # cleanup old
    now = time.time()
    to_delete = [k for k,t in state["forwarded_cache"].items() if now - t > ttl_seconds]
    for k in to_delete:
        del state["forwarded_cache"][k]
    return key in state["forwarded_cache"]

# ---------- Bot Handlers ----------
app = Flask(__name__)
application = None  # telegram Application (set in main)

# simple per-user flow state in-memory
user_flow = {}  # user_id -> dict e.g. {"expect":"source"} etc

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Auto-Forward Bot — Main Menu", reply_markup=make_main_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use inline buttons to configure. No commands required. हर जगह Back बटन है।")

# Callback query router
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Main menus
    if data == "menu_channels":
        kb = [
            [InlineKeyboardButton("Set Source (send @username or id)", callback_data="set_source"),
             InlineKeyboardButton("Set Target (send @username or id)", callback_data="set_target")],
            [InlineKeyboardButton("View Current", callback_data="view_channels")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Channels — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_replacements":
        kb = [
            [InlineKeyboardButton("Add Replacement", callback_data="add_replacement"),
             InlineKeyboardButton("List / Delete", callback_data="list_replacements")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Replacements — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_blocked":
        kb = [
            [InlineKeyboardButton("Add Block", callback_data="add_block"),
             InlineKeyboardButton("List / Delete", callback_data="list_blocked")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Blocked — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_header":
        kb = [
            [InlineKeyboardButton("Set Header", callback_data="set_header"),
             InlineKeyboardButton("Set Footer", callback_data="set_footer")],
            [InlineKeyboardButton("Remove Header/Footer", callback_data="remove_hf")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Header / Footer — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_bw":
        kb = [
            [InlineKeyboardButton("Add Blacklist", callback_data="add_blacklist"),
             InlineKeyboardButton("Add Whitelist", callback_data="add_whitelist")],
            [InlineKeyboardButton("View Both", callback_data="list_bw")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Blacklist / Whitelist — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_schedule":
        kb = [
            [InlineKeyboardButton("Schedule Message", callback_data="schedule_add"),
             InlineKeyboardButton("View / Delete", callback_data="schedule_list")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Scheduling — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_control":
        kb = [
            [InlineKeyboardButton("Start Forwarding", callback_data="control_start"),
             InlineKeyboardButton("Stop Forwarding", callback_data="control_stop")],
            [InlineKeyboardButton("Restart Bot", callback_data="control_restart")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Bot Control — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_advanced":
        kb = [
            [InlineKeyboardButton("Toggle Duplicate Check", callback_data="toggle_dup"),
             InlineKeyboardButton("Toggle Logging", callback_data="toggle_logging")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back|main")]
        ]
        await query.edit_message_text("Advanced — choose:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # Back nav
    if data.startswith("back|"):
        await query.edit_message_text("Main Menu", reply_markup=make_main_keyboard())
        return

    # Specific actions
    if data == "set_source":
        user_flow[query.from_user.id] = {"expect": "source"}
        await query.edit_message_text("Send source channel @username or numeric id now.", reply_markup=back_button("main"))
        return
    if data == "set_target":
        user_flow[query.from_user.id] = {"expect": "target"}
        await query.edit_message_text("Send target channel @username or numeric id now.", reply_markup=back_button("main"))
        return
    if data == "view_channels":
        s = state["channels"]["source"] or "None"
        t = state["channels"]["target"] or "None"
        await query.edit_message_text(f"Source: {s}\nTarget: {t}", reply_markup=back_button("main"))
        return

    if data == "add_replacement":
        user_flow[query.from_user.id] = {"expect": "replacement_old"}
        await query.edit_message_text("Send OLD text to replace (exact).", reply_markup=back_button("main"))
        return
    if data == "list_replacements":
        if not state["replacements"]:
            txt = "No replacements."
        else:
            txt = "\n".join([f'{r["id"]}. "{r["old"]}" -> "{r["new"]}"' for r in state["replacements"]])
        await query.edit_message_text(txt, reply_markup=back_button("main"))
        return

    if data == "add_block":
        user_flow[query.from_user.id] = {"expect": "block_value"}
        await query.edit_message_text("Send link/username/word to block (example: t.me/bad or @baduser or 'badword').", reply_markup=back_button("main"))
        return
    if data == "list_blocked":
        if not state["blocked"]:
            txt = "No blocked entries."
        else:
            txt = "\n".join([f'{b["id"]}. {b["value"]}' for b in state["blocked"]])
        await query.edit_message_text(txt, reply_markup=back_button("main"))
        return

    if data == "set_header":
        user_flow[query.from_user.id] = {"expect": "set_header"}
        await query.edit_message_text("Send header text (will be prepended). Send /none to cancel.", reply_markup=back_button("main"))
        return
    if data == "set_footer":
        user_flow[query.from_user.id] = {"expect": "set_footer"}
        await query.edit_message_text("Send footer text (will be appended). Send /none to cancel.", reply_markup=back_button("main"))
        return
    if data == "remove_hf":
        state["header"] = ""
        state["footer"] = ""
        await query.edit_message_text("Header and Footer removed.", reply_markup=back_button("main"))
        return

    if data == "add_blacklist":
        user_flow[query.from_user.id] = {"expect": "add_blacklist"}
        await query.edit_message_text("Send value to add to blacklist.", reply_markup=back_button("main"))
        return
    if data == "add_whitelist":
        user_flow[query.from_user.id] = {"expect": "add_whitelist"}
        await query.edit_message_text("Send value to add to whitelist.", reply_markup=back_button("main"))
        return
    if data == "list_bw":
        bl = "\n".join(state["blacklist"]) or "None"
        wl = "\n".join(state["whitelist"]) or "None"
        await query.edit_message_text(f"Blacklist:\n{bl}\n\nWhitelist:\n{wl}", reply_markup=back_button("main"))
        return

    if data == "schedule_add":
        user_flow[query.from_user.id] = {"expect": "schedule_when"}
        await query.edit_message_text("Send date-time in UTC (YYYY-MM-DD HH:MM), then send message text in next message.", reply_markup=back_button("main"))
        return
    if data == "schedule_list":
        if not state["schedules"]:
            txt = "No scheduled messages."
        else:
            txt = "\n".join([f'{s["id"]}. To:{s["chat_id"]} At:{s["send_at"].strftime("%Y-%m-%d %H:%M")} Text:{s["text"][:40]}...' for s in state["schedules"]])
        await query.edit_message_text(txt, reply_markup=back_button("main"))
        return

    if data == "control_start":
        state["enabled"] = True
        await query.edit_message_text("Forwarding started.", reply_markup=back_button("main"))
        return
    if data == "control_stop":
        state["enabled"] = False
        await query.edit_message_text("Forwarding stopped.", reply_markup=back_button("main"))
        return
    if data == "control_restart":
        state["enabled"] = False
        await query.edit_message_text("Restarting...", reply_markup=back_button("main"))
        # soft restart: clear caches
        state["forwarded_cache"].clear()
        time.sleep(0.5)
        state["enabled"] = True
        await query.edit_message_text("Bot restarted.", reply_markup=back_button("main"))
        return

    if data == "toggle_logging":
        state["logging"] = not state["logging"]
        await query.edit_message_text(f"Logging set to {state['logging']}", reply_markup=back_button("main"))
        return
    if data == "toggle_dup":
        state["dup_check"] = not state["dup_check"]
        await query.edit_message_text(f"Duplicate check set to {state['dup_check']}", reply_markup=back_button("main"))
        return

    await query.edit_message_text("Unknown action.", reply_markup=back_button("main"))

# Message handler to capture user flow inputs
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    # allow quick back
    if text.lower() in ("/back","back","⬅️ back","⬅️ Back"):
        user_flow.pop(uid, None)
        await update.message.reply_text("Back to main menu.", reply_markup=make_main_keyboard())
        return

    flow = user_flow.get(uid)
    if not flow:
        await update.message.reply_text("Use the menu:", reply_markup=make_main_keyboard())
        return

    expect = flow.get("expect")

    # Channels
    if expect == "source":
        state["channels"]["source"] = text
        user_flow.pop(uid, None)
        await update.message.reply_text(f"Source set to {text}", reply_markup=make_main_keyboard())
        return
    if expect == "target":
        state["channels"]["target"] = text
        user_flow.pop(uid, None)
        await update.message.reply_text(f"Target set to {text}", reply_markup=make_main_keyboard())
        return

    # Replacements
    if expect == "replacement_old":
        flow["old"] = text
        user_flow[uid] = {"expect": "replacement_new", "old": text}
        await update.message.reply_text("Send NEW text (replacement).", reply_markup=back_button("main"))
        return
    if expect == "replacement_new":
        rid = next_id()
        state["replacements"].append({"id": rid, "old": flow.get("old",""), "new": text})
        user_flow.pop(uid, None)
        await update.message.reply_text(f"Replacement added: '{flow.get('old')}' -> '{text}'", reply_markup=make_main_keyboard())
        return

    # Blocked
    if expect == "block_value":
        bid = next_id()
        state["blocked"].append({"id": bid, "value": text})
        user_flow.pop(uid, None)
        await update.message.reply_text(f"Blocked added: {text}", reply_markup=make_main_keyboard())
        return

    # Header/Footer
    if expect == "set_header":
        if text.lower() == "/none":
            user_flow.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=make_main_keyboard())
            return
        state["header"] = text
        user_flow.pop(uid, None)
        await update.message.reply_text("Header saved.", reply_markup=make_main_keyboard())
        return
    if expect == "set_footer":
        if text.lower() == "/none":
            user_flow.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=make_main_keyboard())
            return
        state["footer"] = text
        user_flow.pop(uid, None)
        await update.message.reply_text("Footer saved.", reply_markup=make_main_keyboard())
        return

    # Blacklist / Whitelist
    if expect == "add_blacklist":
        state["blacklist"].append(text)
        user_flow.pop(uid, None)
        await update.message.reply_text("Added to blacklist.", reply_markup=make_main_keyboard())
        return
    if expect == "add_whitelist":
        state["whitelist"].append(text)
        user_flow.pop(uid, None)
        await update.message.reply_text("Added to whitelist.", reply_markup=make_main_keyboard())
        return

    # Scheduling
    if expect == "schedule_when":
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            user_flow[uid] = {"expect": "schedule_text", "send_at": dt}
            await update.message.reply_text("Now send the message text to schedule.", reply_markup=back_button("main"))
            return
        except Exception:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM in UTC.", reply_markup=back_button("main"))
            return
    if expect == "schedule_text":
        send_at = flow.get("send_at")
        sched_id = next_id()
        state["schedules"].append({"id": sched_id, "chat_id": update.effective_chat.id, "text": text, "send_at": send_at})
        user_flow.pop(uid, None)
        await update.message.reply_text("Message scheduled.", reply_markup=make_main_keyboard())
        return

    # fallback
    await update.message.reply_text("Use the menu:", reply_markup=make_main_keyboard())

# Incoming messages / channel posts -> forwarding logic
async def handle_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["enabled"]:
        return

    msg = update.effective_message
    if not msg:
        return

    # get source/target
    src_cfg = state["channels"]["source"]
    tgt_cfg = state["channels"]["target"]
    if not src_cfg or not tgt_cfg:
        return

    # determine source match
    src_match = False
    # if message chat matches source
    try:
        if msg.chat and (str(msg.chat.id) == str(src_cfg) or ("@" + (msg.chat.username or "")) == str(src_cfg)):
            src_match = True
    except Exception:
        pass
    # or forwarded from_chat matches
    if not src_match and getattr(msg, "forward_from_chat", None):
        fc = msg.forward_from_chat
        if fc:
            if str(fc.id) == str(src_cfg) or ("@" + (fc.username or "")) == str(src_cfg):
                src_match = True

    if not src_match:
        return

    # text content
    text = msg.text or msg.caption or ""
    unique_key = f"{msg.chat.id}:{msg.message_id}"

    # duplicate check
    if state["dup_check"] and forwarded_cache_exists(unique_key):
        if state["logging"]:
            logger.info("Duplicate skip: %s", unique_key)
        return

    # blocked check
    isblk, blkval = is_blocked_message(text)
    if isblk:
        if state["logging"]:
            logger.info("Blocked due to %s", blkval)
        return

    # apply replacements
    final_text = apply_replacements(text or "")
    header = state["header"] or ""
    footer = state["footer"] or ""
    composed = "\n".join([part for part in (header, final_text, footer) if part]) or " "

    # forward (copy to target)
    try:
        if msg.photo or msg.video or msg.document or msg.audio:
            await msg.copy(chat_id=tgt_cfg, caption=composed)
        else:
            await context.bot.send_message(chat_id=tgt_cfg, text=composed, parse_mode=ParseMode.HTML)
        add_forwarded_cache(unique_key)
        if state["logging"]:
            logger.info("Forwarded %s -> %s", unique_key, tgt_cfg)
    except Exception as e:
        logger.exception("Forward error: %s", e)

# ---------- Scheduler thread ----------
def schedule_thread_func(loop, app):
    asyncio.set_event_loop(loop)
    while True:
        now = datetime.utcnow()
        due = [s for s in state["schedules"] if s["send_at"] <= now]
        for s in due:
            try:
                loop.create_task(app.bot.send_message(chat_id=int(s["chat_id"]), text=s["text"]))
            except Exception:
                logger.exception("Scheduled send failed")
            # remove after sending
            try:
                state["schedules"].remove(s)
            except ValueError:
                pass
        time.sleep(20)

# ---------- Flask webhook endpoint ----------
@appl = None
def create_webhook_route(app_obj):
    @app.route(WEBHOOK_PATH, methods=["POST"])
    def webhook():
        if request.method == "POST":
            update = Update.de_json(request.get_json(force=True), application.bot)
            # queue update
            application.create_task(application.update_queue.put(update))
            return "OK"
        abort(403)

# ---------- Main ----------
def main():
    global application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    application.add_handler(MessageHandler(filters.ALL, handle_incoming))

    # start scheduler thread
    loop = asyncio.get_event_loop()
    th = threading.Thread(target=schedule_thread_func, args=(loop, application), daemon=True)
    th.start()

    # set webhook if DEPLOY_WEBHOOK_URL provided
    if DEPLOY_WEBHOOK_URL:
        webhook_url = DEPLOY_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        logger.info("Setting webhook to %s", webhook_url)
        try:
            application.bot.set_webhook(webhook_url)
        except Exception:
            logger.exception("Failed to set webhook automatically. You can set it manually with Telegram API.")
    else:
        logger.warning("DEPLOY_WEBHOOK_URL not set; webhook won't be auto-configured.")

    # run Flask to accept webhook
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
