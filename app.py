#!/usr/bin/env python3
"""
Multi-Rule Telegram Auto-Forward Bot
Single-file, ready-to-run on Termux (polling) or Render (webhook).

REQUIREMENTS:
- Python 3.9+
- python-telegram-bot v20+ (pip install python-telegram-bot==20.*)
- SQLAlchemy

ENV VARS (required):
- BOT_TOKEN  -> Telegram bot token
- DATABASE_URL (optional) -> SQLAlchemy DB URL. If unset, sqlite file `bot_rules.db` will be used.
- WEBHOOK_URL (optional) -> if set, webhook mode will be used (suitable for Render). If not set, polling is used (suitable for Termux).

IMPORTANT: This file hardcodes a single fixed admin ID (FORCE_ADMIN_ID).
"""
import os
import logging
import time
import re
import json
from datetime import datetime
from typing import Optional, List

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError

# ------------------ Logging ------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ Conversation States (reserved if needed) ------------------
# (Defined but not used heavily here; reserved for future extension)
START_MENU, WAITING_RULE_SOURCE, WAITING_RULE_DEST, WAITING_RULE_NAME, WAITING_REPLACE_FIND, WAITING_REPLACE_REPL, WAITING_WORD_BLACKLIST, WAITING_WORD_WHITELIST, WAITING_SET_DELAY, WAITING_SET_SCHEDULE = range(10)

# ------------------ Configuration ------------------
# Replace this with your Telegram ID (fixed admin)
FORCE_ADMIN_ID = 1695450646

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set. Exiting.")
    raise SystemExit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    # default sqlite for Termux / local use
    DATABASE_URL = "sqlite:///bot_rules.db"

Engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# ------------------ Database Models ------------------
class ForwardRule(Base):
    __tablename__ = "forward_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String, default="unnamed_rule")
    source_chat_id = Column(String, nullable=False)  # e.g. -100123... or @channel
    destination_chat_id = Column(String, nullable=False)

    is_active = Column(Boolean, default=True)
    block_links = Column(Boolean, default=False)
    block_usernames = Column(Boolean, default=False)
    blacklist_words = Column(PickleType, default=list)
    whitelist_words = Column(PickleType, default=list)
    text_replacements = Column(PickleType, default=dict)  # {find: replace}

    forward_mode = Column(String, default="FORWARD")  # FORWARD or COPY
    forward_delay = Column(Integer, default=0)  # seconds

    # Optional schedule (store as "HH:MM" strings or None)
    schedule_start = Column(String, nullable=True)  # HH:MM
    schedule_end = Column(String, nullable=True)    # HH:MM

    # Stats
    forwarded_count = Column(Integer, default=0)
    last_triggered = Column(DateTime, nullable=True)


class MetaConfig(Base):
    __tablename__ = "meta_config"
    id = Column(Integer, primary_key=True)
    admin_user_id = Column(Integer, default=FORCE_ADMIN_ID)


# Create tables
try:
    Base.metadata.create_all(Engine)
    logger.info("Database initialized")
except OperationalError as e:
    logger.error(f"DB init error: {e}")
    raise

# ------------------ Helper Utilities ------------------
def admin_check(chat_id: int) -> bool:
    """Only fixed admin allowed."""
    return chat_id == FORCE_ADMIN_ID


def format_rule_summary(rule: ForwardRule) -> str:
    start = rule.schedule_start or "Any"
    end = rule.schedule_end or "Any"
    return (
        f"Rule #{rule.id} — {rule.name}\n"
        f"Source: `{rule.source_chat_id}` → Dest: `{rule.destination_chat_id}`\n"
        f"Active: `{rule.is_active}` | Mode: `{rule.forward_mode}` | Delay: `{rule.forward_delay}s`\n"
        f"LinksBlocked: `{rule.block_links}` | UsernamesBlocked: `{rule.block_usernames}`\n"
        f"Blacklist: `{', '.join(rule.blacklist_words or []) or 'None'}` | Whitelist: `{', '.join(rule.whitelist_words or []) or 'None'}`\n"
        f"Replacements: `{len(rule.text_replacements or {})} rules` | Schedule: `{start}-{end}`\n"
        f"Forwarded Count: `{rule.forwarded_count}`"
    )


# ------------------ Telegram Keyboards ------------------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ New Rule", callback_data="new_rule")],
        [InlineKeyboardButton("📜 List Rules", callback_data="list_rules")],
        [InlineKeyboardButton("🔁 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("⚙️ Global Info", callback_data="global_info")],
    ]
    return InlineKeyboardMarkup(keyboard)


def rule_action_keyboard(rule_id: int, rule: ForwardRule):
    keyboard = [
        [InlineKeyboardButton("▶️ Enable" if not rule.is_active else "⏸️ Disable", callback_data=f"toggle_active|{rule_id}")],
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"edit_name|{rule_id}"), InlineKeyboardButton("🗑 Delete", callback_data=f"delete_rule|{rule_id}")],
        [InlineKeyboardButton("🔧 Settings", callback_data=f"settings|{rule_id}")],
        [InlineKeyboardButton("📊 Stats", callback_data=f"stats|{rule_id}"), InlineKeyboardButton("🔁 Export", callback_data=f"export_rule|{rule_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def rule_settings_keyboard(rule_id: int, rule: ForwardRule):
    keyboard = [
        [InlineKeyboardButton(f"Links: {'✅' if rule.block_links else '❌'}", callback_data=f"toggle_links|{rule_id}"), InlineKeyboardButton(f"Usernames: {'✅' if rule.block_usernames else '❌'}", callback_data=f"toggle_usernames|{rule_id}")],
        [InlineKeyboardButton(f"Mode: {rule.forward_mode}", callback_data=f"set_mode|{rule_id}"), InlineKeyboardButton(f"Delay: {rule.forward_delay}s", callback_data=f"set_delay|{rule_id}")],
        [InlineKeyboardButton("➕ Add Replace", callback_data=f"add_replace|{rule_id}"), InlineKeyboardButton("📄 View Replacements", callback_data=f"view_replace|{rule_id}")],
        [InlineKeyboardButton("➕ Blacklist Word", callback_data=f"add_blacklist|{rule_id}"), InlineKeyboardButton("➕ Whitelist Word", callback_data=f"add_whitelist|{rule_id}")],
        [InlineKeyboardButton("🕒 Set Schedule", callback_data=f"set_schedule|{rule_id}"), InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ------------------ Command Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        await update.message.reply_text("Keval admin is bot ko use kar sakta hai.")
        return

    text = (
        "Namaste! Advanced Multi-Rule Forward Bot ready.\n\n"
        "Use buttons to create and manage forwarding rules.\n"
        "(All controls are button-driven — no slash commands required beyond /start.)"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


# ------------------ CallbackQuery Handler (Main Navigation) ------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not admin_check(user.id):
        await query.edit_message_text("Keval admin is bot ko use kar sakta hai.")
        return

    data = query.data
    logger.info(f"Callback data: {data} from {user.id}")

    session = Session()
    try:
        if data == "main" or data == "refresh":
            await query.edit_message_text("Main Menu", reply_markup=main_menu_keyboard())
            return

        if data == "new_rule":
            # Start create flow
            context.user_data["creating_rule"] = {}
            await query.edit_message_text("Send Source Channel ID (e.g. -100123... or @channel)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data == "list_rules":
            rules = session.query(ForwardRule).all()
            if not rules:
                await query.edit_message_text("Koi rule nahi mila.", reply_markup=main_menu_keyboard())
                return
            buttons = []
            for r in rules:
                buttons.append([InlineKeyboardButton(f"#{r.id} {r.name}", callback_data=f"rule_open|{r.id}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main")])
            await query.edit_message_text("Rules:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("rule_open|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_action_keyboard(rule.id), parse_mode="Markdown")
            return

        if data.startswith("toggle_active|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                rule.is_active = not rule.is_active
                session.commit()
                await query.edit_message_text(f"Rule #{rule.id} active={rule.is_active}", reply_markup=rule_action_keyboard(rule.id))
            return

        if data.startswith("delete_rule|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                session.delete(rule)
                session.commit()
                await query.edit_message_text(f"Rule #{rid} deleted.", reply_markup=main_menu_keyboard())
            return

        if data.startswith("edit_name|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_name_rule"] = int(rid)
            await query.edit_message_text("Send new name for the rule:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("settings|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule.id), parse_mode="Markdown")
            return

        if data.startswith("stats|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                txt = f"Rule #{rule.id} Stats:\nForwarded Count: {rule.forwarded_count}\nLast Triggered: {rule.last_triggered or 'Never'}"
                await query.edit_message_text(txt, reply_markup=rule_action_keyboard(rule.id))
            return

        if data.startswith("export_rule|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                payload = {
                    "id": rule.id,
                    "name": rule.name,
                    "source_chat_id": rule.source_chat_id,
                    "destination_chat_id": rule.destination_chat_id,
                    "is_active": rule.is_active,
                    "block_links": rule.block_links,
                    "block_usernames": rule.block_usernames,
                    "blacklist_words": rule.blacklist_words,
                    "whitelist_words": rule.whitelist_words,
                    "text_replacements": rule.text_replacements,
                    "forward_mode": rule.forward_mode,
                    "forward_delay": rule.forward_delay,
                    "schedule_start": rule.schedule_start,
                    "schedule_end": rule.schedule_end,
                }
                await query.edit_message_text("Export JSON:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="main")]]))
                await query.message.reply_text(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if data.startswith("toggle_links|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                rule.block_links = not rule.block_links
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule.id), parse_mode="Markdown")
            return

        if data.startswith("toggle_usernames|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                rule.block_usernames = not rule.block_usernames
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule.id), parse_mode="Markdown")
            return

        if data.startswith("set_mode|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                rule.forward_mode = "COPY" if rule.forward_mode == "FORWARD" else "FORWARD"
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule.id), parse_mode="Markdown")
            return

        if data.startswith("set_delay|"):
            _, rid = data.split("|", 1)
            context.user_data["set_delay_rule"] = int(rid)
            await query.edit_message_text("Send delay in seconds (0/5/15/30/60):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("add_replace|"):
            _, rid = data.split("|", 1)
            context.user_data["add_replace_rule"] = int(rid)
            await query.edit_message_text("Send FIND text (case sensitive):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("view_replace|"):
            _, rid = data.split("|", 1)
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                if not rule.text_replacements:
                    await query.edit_message_text("No replacements set.", reply_markup=rule_settings_keyboard(rule.id))
                else:
                    txt = "\n".join([f"'{k}' → '{v}'" for k, v in (rule.text_replacements or {}).items()])
                    await query.edit_message_text(f"Replacements:\n{txt}", reply_markup=rule_settings_keyboard(rule.id))
            return

        if data.startswith("add_blacklist|"):
            _, rid = data.split("|", 1)
            context.user_data["add_blacklist_rule"] = int(rid)
            await query.edit_message_text("Send word to ADD to blacklist (single word):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("add_whitelist|"):
            _, rid = data.split("|", 1)
            context.user_data["add_whitelist_rule"] = int(rid)
            await query.edit_message_text("Send word to ADD to whitelist (single word):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("set_schedule|"):
            _, rid = data.split("|", 1)
            context.user_data["set_schedule_rule"] = int(rid)
            await query.edit_message_text("Send schedule as START-HH:MM END-HH:MM (24h) or 'any' to clear. Example: 09:00 21:30", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data == "global_info":
            await query.edit_message_text(f"Admin: {FORCE_ADMIN_ID}\nDB: {DATABASE_URL}", reply_markup=main_menu_keyboard())
            return

    finally:
        session.close()


# ------------------ Message Handlers for Conversation Inputs ------------------
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        return

    text = update.message.text.strip()
    session = Session()
    try:
        # Creating rule flow
        if "creating_rule" in context.user_data:
            state = context.user_data["creating_rule"]
            if "source" not in state:
                # first message is source
                if not (text.startswith("-100") or text.startswith("@") or text.isdigit()):
                    await update.message.reply_text("Format galat. Use -100... or @username or numeric chat id.")
                    return
                state["source"] = text
                await update.message.reply_text("Now send Destination Channel ID (e.g. -100... or @channel)")
                return
            if "dest" not in state:
                if not (text.startswith("-100") or text.startswith("@") or text.isdigit()):
                    await update.message.reply_text("Format galat. Use -100... or @username or numeric chat id.")
                    return
                state["dest"] = text
                await update.message.reply_text("Now send a friendly name for this rule (e.g. Sales -> ChannelA)")
                return
            if "name" not in state:
                state["name"] = text[:64]
                # create rule in DB
                rule = ForwardRule(
                    name=state["name"],
                    source_chat_id=state["source"],
                    destination_chat_id=state["dest"],
                )
                session.add(rule)
                session.commit()
                context.user_data.pop("creating_rule", None)
                await update.message.reply_text(f"Rule created:\n{format_rule_summary(rule)}", reply_markup=main_menu_keyboard(), parse_mode="Markdown")
                return

        # Edit rule name flow
        if "edit_name_rule" in context.user_data:
            rid = context.user_data.pop("edit_name_rule")
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                rule.name = text[:64]
                session.commit()
                await update.message.reply_text("Name updated.", reply_markup=main_menu_keyboard())
            return

        # Set delay flow
        if "set_delay_rule" in context.user_data:
            rid = context.user_data.pop("set_delay_rule")
            try:
                val = int(text)
            except ValueError:
                await update.message.reply_text("Please send an integer seconds value like 0,5,15,30,60")
                return
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                rule.forward_delay = max(0, val)
                session.commit()
                await update.message.reply_text("Delay updated.", reply_markup=main_menu_keyboard())
            return

        # Add replace find
        if "add_replace_rule" in context.user_data and "replace_find" not in context.user_data:
            rid = context.user_data["add_replace_rule"]
            context.user_data["replace_find"] = text
            await update.message.reply_text(f"Now send REPLACE text for '{text}'")
            return
        if "add_replace_rule" in context.user_data and "replace_find" in context.user_data:
            rid = context.user_data.pop("add_replace_rule")
            find = context.user_data.pop("replace_find")
            repl = text
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                replacements = rule.text_replacements or {}
                replacements[find] = repl
                rule.text_replacements = replacements
                session.commit()
                await update.message.reply_text("Replacement saved.", reply_markup=main_menu_keyboard())
            return

        # Add blacklist
        if "add_blacklist_rule" in context.user_data:
            rid = context.user_data.pop("add_blacklist_rule")
            word = text.lower().strip()
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                bl = rule.blacklist_words or []
                if word not in bl:
                    bl.append(word)
                    rule.blacklist_words = bl
                    session.commit()
                await update.message.reply_text("Blacklist updated.", reply_markup=main_menu_keyboard())
            return

        # Add whitelist
        if "add_whitelist_rule" in context.user_data:
            rid = context.user_data.pop("add_whitelist_rule")
            word = text.lower().strip()
            rule = session.query(ForwardRule).get(int(rid))
            if rule:
                wl = rule.whitelist_words or []
                if word not in wl:
                    wl.append(word)
                    rule.whitelist_words = wl
                    session.commit()
                await update.message.reply_text("Whitelist updated.", reply_markup=main_menu_keyboard())
            return

        # Set schedule
        if "set_schedule_rule" in context.user_data:
            rid = context.user_data.pop("set_schedule_rule")
            rule = session.query(ForwardRule).get(int(rid))
            if not rule:
                await update.message.reply_text("Rule not found.")
                return
            if text.strip().lower() == "any":
                rule.schedule_start = None
                rule.schedule_end = None
                session.commit()
                await update.message.reply_text("Schedule cleared.", reply_markup=main_menu_keyboard())
                return
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text("Invalid format. Send: START_HH:MM END_HH:MM or 'any' to clear.")
                return
            start, end = parts
            # basic validation
            try:
                datetime.strptime(start, "%H:%M")
                datetime.strptime(end, "%H:%M")
            except Exception:
                await update.message.reply_text("Time format invalid. Use HH:MM in 24h.")
                return
            rule.schedule_start = start
            rule.schedule_end = end
            session.commit()
            await update.message.reply_text("Schedule saved.", reply_markup=main_menu_keyboard())
            return

    finally:
        session.close()


# ------------------ Core Forwarding Logic ------------------
def time_in_schedule(start: Optional[str], end: Optional[str]) -> bool:
    if not start or not end:
        return True
    now = datetime.utcnow().time()
    s = datetime.strptime(start, "%H:%M").time()
    e = datetime.strptime(end, "%H:%M").time()
    if s <= e:
        return s <= now <= e
    else:
        # overnight schedule
        return now >= s or now <= e


async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handles both channel_post and message
    message = update.channel_post or update.message
    if message is None:
        return

    session = Session()
    try:
        # find all active rules
        rules: List[ForwardRule] = session.query(ForwardRule).filter(ForwardRule.is_active == True).all()
        for rule in rules:
            if not rule.source_chat_id:
                continue

            # Matching: allow exact id or username or substring-contains
            matched = False
            try:
                msg_chat_id = str(message.chat.id)
                if rule.source_chat_id.startswith("-100") and msg_chat_id == rule.source_chat_id:
                    matched = True
                elif rule.source_chat_id.startswith("@"):
                    user_name = getattr(message.chat, "username", "")
                    if user_name and ("@" + user_name).lower() == rule.source_chat_id.lower():
                        matched = True
                elif rule.source_chat_id.isdigit() and msg_chat_id == rule.source_chat_id:
                    matched = True
                else:
                    # fallback contains check
                    if msg_chat_id in rule.source_chat_id or (getattr(message.chat, "username", "") and ("@" + message.chat.username).lower() in rule.source_chat_id.lower()):
                        matched = True
            except Exception:
                matched = False

            if not matched:
                continue

            # schedule check
            if not time_in_schedule(rule.schedule_start, rule.schedule_end):
                continue

            # read text/caption
            text_to_process = message.text or message.caption or ""
            text_lower = (text_to_process or "").lower()

            # filters
            if rule.block_links and (("http" in text_lower) or ("t.me" in text_lower)):
                continue
            if rule.block_usernames and re.search(r"@[a-zA-Z0-9_]+", text_to_process or ""):
                continue

            skip = False
            if rule.blacklist_words:
                for w in rule.blacklist_words:
                    if w and w in text_lower:
                        skip = True
                        break
            if skip:
                continue

            if rule.whitelist_words:
                ok = False
                for w in rule.whitelist_words:
                    if w and w in text_lower:
                        ok = True
                        break
                if not ok:
                    continue

            # apply replacements (case-sensitive)
            final_text = text_to_process
            text_modified = False
            if rule.text_replacements and final_text:
                for find, repl in (rule.text_replacements or {}).items():
                    if find in final_text:
                        final_text = final_text.replace(find, repl)
                        text_modified = True

            # delay
            if rule.forward_delay and rule.forward_delay > 0:
                time.sleep(rule.forward_delay)

            # decide send mode
            force_copy = text_modified or (rule.forward_mode == "COPY")

            try:
                if force_copy:
                    # if media present, use copy_message to keep media and set new caption
                    if getattr(message, "photo", None) or getattr(message, "video", None) or getattr(message, "document", None) or getattr(message, "audio", None):
                        caption_to_send = final_text if final_text else ""
                        await context.bot.copy_message(chat_id=rule.destination_chat_id, from_chat_id=message.chat.id, message_id=message.message_id, caption=caption_to_send)
                    else:
                        # plain text
                        if final_text and final_text.strip():
                            await context.bot.send_message(chat_id=rule.destination_chat_id, text=final_text)
                else:
                    # forward original
                    await context.bot.forward_message(chat_id=rule.destination_chat_id, from_chat_id=message.chat.id, message_id=message.message_id)

                # update stats
                rule.forwarded_count = (rule.forwarded_count or 0) + 1
                rule.last_triggered = datetime.utcnow()
                session.commit()

            except Exception as e:
                logger.error(f"Forward error for rule {rule.id}: {e}")
                try:
                    await context.bot.send_message(FORCE_ADMIN_ID, f"Error forwarding for rule {rule.id}: {e}")
                except Exception:
                    logger.exception("Failed to notify admin")

    finally:
        session.close()


# ------------------ Application Setup ------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # This handler watches for all incoming messages (including channel posts)
    application.add_handler(MessageHandler(filters.ALL, forward_message))

    PORT = int(os.environ.get("PORT", "8080"))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        logger.info("Starting webhook mode")
        application.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        logger.info("Starting polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
