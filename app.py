#!/usr/bin/env python3
"""
Multi-Rule Telegram Auto-Forward Bot (Render-ready)
Timezone: Asia/Kolkata for schedule checks.

ENV:
- BOT_TOKEN (required)
- DATABASE_URL (optional) -- if unset uses sqlite:///bot_rules.db
- WEBHOOK_URL (optional) -- if set, bot runs webhook mode
- PORT (optional, default 8080)
"""
import os
import logging
import time
import re
import json
import urllib.parse
from datetime import datetime
from typing import Optional, List
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, DateTime, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.mutable import MutableDict, MutableList

# ------------------ Logging ------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ Config ------------------
# Fixed admin ID (your ID)
FORCE_ADMIN_ID = 1695450646  # fixed admin as requested

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set. Exiting.")
    raise SystemExit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///bot_rules.db"

# Use Asia/Kolkata timezone for schedules
KOLKATA_TZ = ZoneInfo("Asia/Kolkata")

Engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# ------------------ DB Models ------------------
class ForwardRule(Base):
    __tablename__ = "forward_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String, default="unnamed_rule")
    source_chat_id = Column(String, nullable=False)    # e.g. -100123... or @channel
    destination_chat_id = Column(String, nullable=False)

    is_active = Column(Boolean, default=True)
    block_links = Column(Boolean, default=False)
    block_usernames = Column(Boolean, default=False)

    # Use Mutable containers so sqlalchemy tracks in-place changes
    blacklist_words = Column(MutableList.as_mutable(PickleType), default=list)     # list[str]
    whitelist_words = Column(MutableList.as_mutable(PickleType), default=list)     # list[str]
    text_replacements = Column(MutableDict.as_mutable(PickleType), default=dict)   # dict{find: replace}

    header_text = Column(String, nullable=True)
    footer_text = Column(String, nullable=True)

    forward_mode = Column(String, default="FORWARD")  # FORWARD or COPY
    forward_delay = Column(Integer, default=0)  # seconds

    schedule_start = Column(String, nullable=True)  # "HH:MM" (Asia/Kolkata)
    schedule_end = Column(String, nullable=True)    # "HH:MM"

    forwarded_count = Column(Integer, default=0)
    last_triggered = Column(DateTime, nullable=True)


class MetaConfig(Base):
    __tablename__ = "meta_config"
    id = Column(Integer, primary_key=True)
    admin_user_id = Column(Integer, default=FORCE_ADMIN_ID)


# ------------------ Auto DB-fix helper ------------------
def ensure_tables_and_columns():
    """
    Create tables if missing (create_all), then inspect existing columns and add any missing columns
    to forward_rules table automatically using ALTER TABLE (supports sqlite & postgresql).
    """
    inspector = inspect(Engine)

    # Ensure tables exist (will create missing tables)
    Base.metadata.create_all(Engine)

    # Now ensure forward_rules exists and its columns are present
    if not inspector.has_table("forward_rules"):
        logger.info("forward_rules table not present after create_all (unexpected).")
        return

    existing_cols = {col["name"] for col in inspector.get_columns("forward_rules")}
    logger.info(f"Existing forward_rules columns: {existing_cols}")

    # desired columns with SQL types per dialect
    dialect = Engine.dialect.name  # 'postgresql' or 'sqlite' etc.
    logger.info(f"DB dialect detected: {dialect}")

    # Map column name -> (postgres_type, sqlite_type)
    expected_columns = {
        "header_text": ("TEXT", "TEXT"),
        "footer_text": ("TEXT", "TEXT"),
        "text_replacements": ("BYTEA", "BLOB"),   # PickleType -> BYTEA (pg) / BLOB (sqlite)
        "blacklist_words": ("BYTEA", "BLOB"),
        "whitelist_words": ("BYTEA", "BLOB"),
        "forwarded_count": ("INTEGER", "INTEGER"),
        "last_triggered": ("TIMESTAMP", "DATETIME"),
    }

    missing = [c for c in expected_columns.keys() if c not in existing_cols]
    if not missing:
        logger.info("No missing columns in forward_rules.")
        return

    logger.info(f"Missing columns detected: {missing}")
    # Add missing columns safely
    with Engine.connect() as conn:
        trans = conn.begin()
        try:
            for col in missing:
                pg_type, sqlite_type = expected_columns[col]
                sql_type = pg_type if dialect.startswith("postgres") or dialect == "postgresql" else sqlite_type
                alter_sql = f'ALTER TABLE forward_rules ADD COLUMN "{col}" {sql_type};'
                logger.info(f"Adding column {col} with SQL: {alter_sql}")
                conn.execute(text(alter_sql))
            trans.commit()
            logger.info("Missing columns added successfully.")
        except Exception as e:
            trans.rollback()
            logger.exception(f"Failed to add missing columns automatically: {e}")
            return

# Run schema ensure on startup
try:
    ensure_tables_and_columns()
except Exception as e:
    logger.exception(f"Auto DB-fix failed on startup: {e}")

# ------------------ Helpers ------------------
def admin_check(user_id: Optional[int]) -> bool:
    """Only fixed admin allowed."""
    return user_id == FORCE_ADMIN_ID

def safe_str_join(lst):
    try:
        return ", ".join(lst or [])
    except Exception:
        return "None"

def format_rule_summary(rule: ForwardRule) -> str:
    start = rule.schedule_start or "Any"
    end = rule.schedule_end or "Any"
    return (
        f"Rule #{rule.id} — {rule.name}\n"
        f"Source: `{rule.source_chat_id}` → Dest: `{rule.destination_chat_id}`\n"
        f"Active: `{rule.is_active}` | Mode: `{rule.forward_mode}` | Delay: `{rule.forward_delay}s`\n"
        f"LinksBlocked: `{rule.block_links}` | UsernamesBlocked: `{rule.block_usernames}`\n"
        f"Blacklist: `{safe_str_join(rule.blacklist_words) or 'None'}` | Whitelist: `{safe_str_join(rule.whitelist_words) or 'None'}`\n"
        f"Header: `{(rule.header_text[:40] + '...') if rule.header_text else 'None'}` | Footer: `{(rule.footer_text[:40] + '...') if rule.footer_text else 'None'}`\n"
        f"Replacements: `{len(rule.text_replacements or {})} rules` | Schedule: `{start}-{end}`\n"
        f"Forwarded Count: `{rule.forwarded_count}`"
    )

# ------------------ Keyboards ------------------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ New Rule", callback_data="new_rule")],
        [InlineKeyboardButton("📜 List Rules", callback_data="list_rules")],
        [InlineKeyboardButton("🔁 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("⚙️ Global Info", callback_data="global_info")],
    ]
    return InlineKeyboardMarkup(keyboard)

def rule_action_keyboard(rule: ForwardRule):
    rid = rule.id
    keyboard = [
        [InlineKeyboardButton("▶️ Enable" if not rule.is_active else "⏸️ Disable", callback_data=f"toggle_active|{rid}")],
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"edit_name|{rid}"), InlineKeyboardButton("🗑 Delete", callback_data=f"delete_rule|{rid}")],
        [InlineKeyboardButton("🔧 Settings", callback_data=f"settings|{rid}")],
        [InlineKeyboardButton("📊 Stats", callback_data=f"stats|{rid}"), InlineKeyboardButton("🔁 Export", callback_data=f"export_rule|{rid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def rule_settings_keyboard(rule: ForwardRule):
    rid = rule.id
    keyboard = [
        [InlineKeyboardButton(f"Links: {'✅' if rule.block_links else '❌'}", callback_data=f"toggle_links|{rid}"), InlineKeyboardButton(f"Usernames: {'✅' if rule.block_usernames else '❌'}", callback_data=f"toggle_usernames|{rid}")],
        [InlineKeyboardButton(f"Mode: {rule.forward_mode}", callback_data=f"set_mode|{rid}"), InlineKeyboardButton(f"Delay: {rule.forward_delay}s", callback_data=f"set_delay|{rid}")],
        [InlineKeyboardButton("➕ Add Replace", callback_data=f"add_replace|{rid}"), InlineKeyboardButton("📄 View Replacements", callback_data=f"view_replace|{rid}")],
        [InlineKeyboardButton("➕ Blacklist Word", callback_data=f"add_blacklist|{rid}"), InlineKeyboardButton("📋 View Blacklist", callback_data=f"view_blacklist|{rid}")],
        [InlineKeyboardButton("➕ Whitelist Word", callback_data=f"add_whitelist|{rid}"), InlineKeyboardButton("📋 View Whitelist", callback_data=f"view_whitelist|{rid}")],
        [InlineKeyboardButton("🖊️ Edit Header", callback_data=f"edit_header|{rid}"), InlineKeyboardButton("🖊️ Edit Footer", callback_data=f"edit_footer|{rid}")],
        [InlineKeyboardButton("🕒 Set Schedule", callback_data=f"set_schedule|{rid}"), InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ------------------ Command Handler ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        await update.message.reply_text("Keval admin is bot ko use kar sakta hai.")
        return
    text = (
        "Namaste! Advanced Multi-Rule Forward Bot ready.\n\n"
        "Use buttons to create and manage forwarding rules.\n"
        "(All controls are button-driven — use /start if menu disappears.)"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())

# ------------------ Callback Handler ------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not admin_check(user.id):
        # avoid editing message if not authorized; send short notice
        try:
            await query.edit_message_text("Keval admin is bot ko use kar sakta hai.")
        except Exception:
            pass
        return

    data = query.data or ""
    logger.info(f"Callback data: {data} from {user.id}")

    session = Session()
    try:
        # simple navigation
        if data in ("main", "refresh"):
            await query.edit_message_text("Main Menu", reply_markup=main_menu_keyboard())
            return

        if data == "new_rule":
            context.user_data["creating_rule"] = {}
            await query.edit_message_text("Send Source Channel ID (e.g. -100123... or @channel)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data == "list_rules":
            rules = session.query(ForwardRule).all()
            if not rules:
                await query.edit_message_text("Koi rule nahi mila.", reply_markup=main_menu_keyboard())
                return
            buttons = [[InlineKeyboardButton(f"#{r.id} {r.name}", callback_data=f"rule_open|{r.id}")] for r in rules]
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main")])
            await query.edit_message_text("Rules:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        # open rule main
        if data.startswith("rule_open|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_action_keyboard(rule), parse_mode="Markdown")
            return

        # enable/disable
        if data.startswith("toggle_active|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.is_active = not rule.is_active
                session.commit()
                await query.edit_message_text(f"Rule #{rule.id} active={rule.is_active}", reply_markup=rule_action_keyboard(rule))
            return

        # delete rule
        if data.startswith("delete_rule|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                session.delete(rule)
                session.commit()
                await query.edit_message_text(f"Rule #{rid} deleted.", reply_markup=main_menu_keyboard())
            return

        # edit name start
        if data.startswith("edit_name|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_name_rule"] = int(rid)
            await query.edit_message_text("Send new name for the rule:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        # settings open
        if data.startswith("settings|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        # stats
        if data.startswith("stats|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                txt = f"Rule #{rule.id} Stats:\nForwarded Count: {rule.forwarded_count}\nLast Triggered: {rule.last_triggered or 'Never'}"
                await query.edit_message_text(txt, reply_markup=rule_action_keyboard(rule))
            return

        # export
        if data.startswith("export_rule|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
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
                    "header_text": rule.header_text,
                    "footer_text": rule.footer_text,
                    "forward_mode": rule.forward_mode,
                    "forward_delay": rule.forward_delay,
                    "schedule_start": rule.schedule_start,
                    "schedule_end": rule.schedule_end,
                }
                await query.edit_message_text("Export JSON:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="main")]]))
                await query.message.reply_text(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        # toggle links/usernames
        if data.startswith("toggle_links|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.block_links = not rule.block_links
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("toggle_usernames|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.block_usernames = not rule.block_usernames
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        # toggle mode
        if data.startswith("set_mode|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.forward_mode = "COPY" if rule.forward_mode == "FORWARD" else "FORWARD"
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        # set delay start
        if data.startswith("set_delay|"):
            _, rid = data.split("|", 1)
            context.user_data["set_delay_rule"] = int(rid)
            await query.edit_message_text("Send delay in seconds (0/5/15/30/60):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        # add replacement start
        if data.startswith("add_replace|"):
            _, rid = data.split("|", 1)
            context.user_data["add_replace_rule"] = int(rid)
            # ask for FIND text; flow continues in text handler
            await query.edit_message_text("Send FIND text (case sensitive):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        # view replacements -> show list with delete buttons
        if data.startswith("view_replace|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            replacements = rule.text_replacements or {}
            if not replacements:
                await query.edit_message_text("Koi replacement set nahi hai.", reply_markup=rule_settings_keyboard(rule))
                return
            # build buttons: each replacement shows delete button
            buttons = []
            for find, repl in replacements.items():
                key_enc = urllib.parse.quote_plus(find)
                buttons.append([InlineKeyboardButton(f"'{find}' → '{repl}'", callback_data="noop")])
                buttons.append([InlineKeyboardButton("❌ Delete", callback_data=f"del_replace|{rid}|{key_enc}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{rid}")])
            await query.edit_message_text("Replacements (click Delete to remove):", reply_markup=InlineKeyboardMarkup(buttons))
            return

        # delete replacement callback
        if data.startswith("del_replace|"):
            _, rid, key_enc = data.split("|", 2)
            find = urllib.parse.unquote_plus(key_enc)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                replacements = rule.text_replacements or {}
                if find in replacements:
                    replacements.pop(find)
                    # assign back to ensure DB change tracked (MutableDict usually tracks but reassign to be safe)
                    rule.text_replacements = replacements
                    session.commit()
                    await query.edit_message_text(f"Replacement '{find}' deleted.", reply_markup=rule_settings_keyboard(rule))
                else:
                    await query.edit_message_text("Replacement not found.", reply_markup=rule_settings_keyboard(rule))
            return

        # add blacklist start
        if data.startswith("add_blacklist|"):
            _, rid = data.split("|", 1)
            context.user_data["add_blacklist_rule"] = int(rid)
            await query.edit_message_text("Send word to ADD to blacklist (single word):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        # view blacklist with delete buttons
        if data.startswith("view_blacklist|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            bl = rule.blacklist_words or []
            if not bl:
                await query.edit_message_text("Blacklist empty.", reply_markup=rule_settings_keyboard(rule))
                return
            buttons = []
            for w in bl:
                w_enc = urllib.parse.quote_plus(w)
                buttons.append([InlineKeyboardButton(f"{w}", callback_data="noop")])
                buttons.append([InlineKeyboardButton("❌ Remove", callback_data=f"del_black|{rid}|{w_enc}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{rid}")])
            await query.edit_message_text("Blacklist (Remove to delete):", reply_markup=InlineKeyboardMarkup(buttons))
            return

        # delete blacklist item
        if data.startswith("del_black|"):
            _, rid, w_enc = data.split("|", 2)
            word = urllib.parse.unquote_plus(w_enc)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                bl = rule.blacklist_words or []
                if word in bl:
                    bl.remove(word)
                    rule.blacklist_words = bl
                    session.commit()
                    await query.edit_message_text(f"Blacklist item '{word}' removed.", reply_markup=rule_settings_keyboard(rule))
                else:
                    await query.edit_message_text("Item not found.", reply_markup=rule_settings_keyboard(rule))
            return

        # add whitelist start
        if data.startswith("add_whitelist|"):
            _, rid = data.split("|", 1)
            context.user_data["add_whitelist_rule"] = int(rid)
            await query.edit_message_text("Send word to ADD to whitelist (single word):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        # view whitelist
        if data.startswith("view_whitelist|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            wl = rule.whitelist_words or []
            if not wl:
                await query.edit_message_text("Whitelist empty.", reply_markup=rule_settings_keyboard(rule))
                return
            buttons = []
            for w in wl:
                w_enc = urllib.parse.quote_plus(w)
                buttons.append([InlineKeyboardButton(f"{w}", callback_data="noop")])
                buttons.append([InlineKeyboardButton("❌ Remove", callback_data=f"del_white|{rid}|{w_enc}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{rid}")])
            await query.edit_message_text("Whitelist (Remove to delete):", reply_markup=InlineKeyboardMarkup(buttons))
            return

        # delete whitelist item
        if data.startswith("del_white|"):
            _, rid, w_enc = data.split("|", 2)
            word = urllib.parse.unquote_plus(w_enc)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                wl = rule.whitelist_words or []
                if word in wl:
                    wl.remove(word)
                    rule.whitelist_words = wl
                    session.commit()
                    await query.edit_message_text(f"Whitelist item '{word}' removed.", reply_markup=rule_settings_keyboard(rule))
                else:
                    await query.edit_message_text("Item not found.", reply_markup=rule_settings_keyboard(rule))
            return

        # edit header/footer
        if data.startswith("edit_header|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_header_rule"] = int(rid)
            await query.edit_message_text("Send header text (this text will prepend forwarded messages):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("edit_footer|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_footer_rule"] = int(rid)
            await query.edit_message_text("Send footer text (this text will append to forwarded messages):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        # schedule
        if data.startswith("set_schedule|"):
            _, rid = data.split("|", 1)
            context.user_data["set_schedule_rule"] = int(rid)
            await query.edit_message_text("Send schedule as START_HH:MM END_HH:MM (Asia/Kolkata 24h) or 'any' to clear. Example: 09:00 21:30", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data == "global_info":
            await query.edit_message_text(f"Admin: {FORCE_ADMIN_ID}\nDB: {DATABASE_URL}\nTZ: Asia/Kolkata", reply_markup=main_menu_keyboard())
            return

        # noop for display-only buttons
        if data == "noop":
            return

    finally:
        session.close()

# ------------------ Text message handler (for flows) ------------------
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    session = Session()
    try:
        # Creating rule flow
        if "creating_rule" in context.user_data:
            state = context.user_data["creating_rule"]
            if "source" not in state:
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
                # explicit initialize lists/dicts to avoid legacy None
                rule = ForwardRule(
                    name=state["name"],
                    source_chat_id=state["source"],
                    destination_chat_id=state["dest"],
                    blacklist_words=[],
                    whitelist_words=[],
                    text_replacements={},
                )
                session.add(rule)
                session.commit()
                context.user_data.pop("creating_rule", None)
                await update.message.reply_text(f"Rule created:\n{format_rule_summary(rule)}", reply_markup=main_menu_keyboard(), parse_mode="Markdown")
                return

        # Edit name
        if "edit_name_rule" in context.user_data:
            rid = context.user_data.pop("edit_name_rule")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.name = text[:64]
                session.commit()
                await update.message.reply_text("Name updated.", reply_markup=main_menu_keyboard())
            return

        # Set delay
        if "set_delay_rule" in context.user_data:
            rid = context.user_data.pop("set_delay_rule")
            try:
                val = int(text)
            except ValueError:
                await update.message.reply_text("Please send an integer seconds value like 0,5,15,30,60")
                return
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.forward_delay = max(0, val)
                session.commit()
                await update.message.reply_text("Delay updated.", reply_markup=main_menu_keyboard())
            return

        # Add replacement flow (two-step)
        if "add_replace_rule" in context.user_data and "replace_find" not in context.user_data:
            rid = context.user_data["add_replace_rule"]
            context.user_data["replace_find"] = text
            await update.message.reply_text(f"Now send REPLACE text for '{text}'")
            return
        if "add_replace_rule" in context.user_data and "replace_find" in context.user_data:
            rid = context.user_data.pop("add_replace_rule")
            find = context.user_data.pop("replace_find")
            repl = text
            rule = session.get(ForwardRule, int(rid))
            if rule:
                replacements = rule.text_replacements or {}
                # Add/overwrite the find key (multiple pairs supported)
                replacements[find] = repl
                # Reassign to ensure change detection (MutableDict often handles in-place, but reassign to be safe)
                rule.text_replacements = replacements
                session.commit()
                await update.message.reply_text("Replacement saved.", reply_markup=rule_settings_keyboard(rule))
            return

        # Add blacklist word
        if "add_blacklist_rule" in context.user_data:
            rid = context.user_data.pop("add_blacklist_rule")
            word = text.lower().strip()
            rule = session.get(ForwardRule, int(rid))
            if rule:
                bl = rule.blacklist_words or []
                if word not in bl:
                    bl.append(word)
                    rule.blacklist_words = bl
                    session.commit()
                await update.message.reply_text("Blacklist updated.", reply_markup=rule_settings_keyboard(rule))
            return

        # Add whitelist word
        if "add_whitelist_rule" in context.user_data:
            rid = context.user_data.pop("add_whitelist_rule")
            word = text.lower().strip()
            rule = session.get(ForwardRule, int(rid))
            if rule:
                wl = rule.whitelist_words or []
                if word not in wl:
                    wl.append(word)
                    rule.whitelist_words = wl
                    session.commit()
                await update.message.reply_text("Whitelist updated.", reply_markup=rule_settings_keyboard(rule))
            return

        # Edit header
        if "edit_header_rule" in context.user_data:
            rid = context.user_data.pop("edit_header_rule")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.header_text = text
                session.commit()
                await update.message.reply_text("Header updated.", reply_markup=rule_settings_keyboard(rule))
            return

        # Edit footer
        if "edit_footer_rule" in context.user_data:
            rid = context.user_data.pop("edit_footer_rule")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.footer_text = text
                session.commit()
                await update.message.reply_text("Footer updated.", reply_markup=rule_settings_keyboard(rule))
            return

        # Set schedule
        if "set_schedule_rule" in context.user_data:
            rid = context.user_data.pop("set_schedule_rule")
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await update.message.reply_text("Rule not found.")
                return
            if text.strip().lower() == "any":
                rule.schedule_start = None
                rule.schedule_end = None
                session.commit()
                await update.message.reply_text("Schedule cleared.", reply_markup=rule_settings_keyboard(rule))
                return
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text("Invalid format. Send: START_HH:MM END_HH:MM or 'any' to clear.")
                return
            start, end = parts
            try:
                datetime.strptime(start, "%H:%M")
                datetime.strptime(end, "%H:%M")
            except Exception:
                await update.message.reply_text("Time format invalid. Use HH:MM in 24h.")
                return
            rule.schedule_start = start
            rule.schedule_end = end
            session.commit()
            await update.message.reply_text("Schedule saved.", reply_markup=rule_settings_keyboard(rule))
            return

    finally:
        session.close()

# ------------------ Schedule helper ------------------
def time_in_schedule(start: Optional[str], end: Optional[str]) -> bool:
    # Uses Asia/Kolkata timezone
    if not start or not end:
        return True
    now = datetime.now(KOLKATA_TZ).time()
    s = datetime.strptime(start, "%H:%M").time()
    e = datetime.strptime(end, "%H:%M").time()
    if s <= e:
        return s <= now <= e
    else:
        # overnight schedule
        return now >= s or now <= e

# ------------------ Forwarding logic ------------------
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if message is None:
        return

    session = Session()
    try:
        rules: List[ForwardRule] = session.query(ForwardRule).filter(ForwardRule.is_active == True).all()
        for rule in rules:
            if not rule.source_chat_id:
                continue

            # matching (id or @username or contains)
            matched = False
            try:
                msg_chat_id = str(message.chat.id)
                if rule.source_chat_id.startswith("-100") and msg_chat_id == rule.source_chat_id:
                    matched = True
                elif rule.source_chat_id.startswith("@"):
                    uname = getattr(message.chat, "username", "")
                    if uname and ("@" + uname).lower() == rule.source_chat_id.lower():
                        matched = True
                elif rule.source_chat_id.isdigit() and msg_chat_id == rule.source_chat_id:
                    matched = True
                else:
                    if msg_chat_id in rule.source_chat_id or (getattr(message.chat, "username", "") and ("@" + message.chat.username).lower() in rule.source_chat_id.lower()):
                        matched = True
            except Exception:
                matched = False

            if not matched:
                continue

            # schedule check
            if not time_in_schedule(rule.schedule_start, rule.schedule_end):
                continue

            # text/caption
            text_to_process = message.text or message.caption or ""
            text_lower = (text_to_process or "").lower()

            # filters
            if rule.block_links and (("http" in text_lower) or ("t.me" in text_lower)):
                continue
            if rule.block_usernames and re.search(r"@[a-zA-Z0-9_]+", text_to_process or ""):
                continue

            # blacklist
            skip = False
            if rule.blacklist_words:
                for w in (rule.blacklist_words or []):
                    if w and w in text_lower:
                        skip = True
                        break
            if skip:
                continue

            # whitelist (must contain at least one)
            if rule.whitelist_words:
                ok = False
                for w in (rule.whitelist_words or []):
                    if w and w in text_lower:
                        ok = True
                        break
                if not ok:
                    continue

            # apply replacements
            final_text = text_to_process
            text_modified = False
            if rule.text_replacements and final_text:
                # iterate over items (make list to avoid runtime mutation issues)
                for find, repl in list((rule.text_replacements or {}).items()):
                    if find and find in final_text:
                        final_text = final_text.replace(find, repl)
                        text_modified = True

            # prepend header / append footer
            if rule.header_text:
                final_text = f"{rule.header_text}\n\n{final_text}"
            if rule.footer_text:
                final_text = f"{final_text}\n\n{rule.footer_text}"

            # delay (synchronous)
            if rule.forward_delay and rule.forward_delay > 0:
                time.sleep(rule.forward_delay)

            force_copy = text_modified or (rule.forward_mode == "COPY")

            try:
                if force_copy:
                    # media -> copy_message with caption
                    if getattr(message, "photo", None) or getattr(message, "video", None) or getattr(message, "document", None) or getattr(message, "audio", None) or getattr(message, "sticker", None):
                        caption_to_send = final_text if final_text else ""
                        await context.bot.copy_message(chat_id=rule.destination_chat_id, from_chat_id=message.chat.id, message_id=message.message_id, caption=caption_to_send)
                    else:
                        if final_text and final_text.strip():
                            await context.bot.send_message(chat_id=rule.destination_chat_id, text=final_text)
                else:
                    await context.bot.forward_message(chat_id=rule.destination_chat_id, from_chat_id=message.chat.id, message_id=message.message_id)

                # stats
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

# ------------------ App setup ------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # watch for all messages (including channel posts)
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
