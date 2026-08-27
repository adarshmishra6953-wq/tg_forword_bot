#!/usr/bin/env python3
"""
Advanced Multi-Rule Telegram Auto-Forward Bot - Render ready

Added:
1) Media Album Support (preserves Telegram media groups when possible)
2) Full Backup / Restore through Telegram buttons
3) Global Forwarding ON/OFF

Existing features retained:
- Multiple independent source -> destination rules
- Enable/disable per rule
- FORWARD/COPY mode
- Delay
- Link/username blocking
- Blacklist/whitelist
- Text replacements
- Header/footer
- Schedule
- Per-rule stats
- JSON export
- SQLite/PostgreSQL support
"""

import os
import logging
import time
import re
import json
import urllib.parse
import tempfile
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

try:
    # Newer python-telegram-bot versions
    from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
except ImportError:
    # Compatibility with versions where FSInputFile is not exported
    from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
    FSInputFile = InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, DateTime, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.mutable import MutableDict, MutableList

# ------------------ Logging ------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------ Config ------------------
FORCE_ADMIN_ID = 1695450646
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set. Exiting.")
    raise SystemExit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///bot_rules.db"

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")

Engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# ------------------ DB Models ------------------
class ForwardRule(Base):
    __tablename__ = "forward_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String, default="unnamed_rule")
    source_chat_id = Column(String, nullable=False)
    destination_chat_id = Column(String, nullable=False)

    is_active = Column(Boolean, default=True)
    block_links = Column(Boolean, default=False)
    block_usernames = Column(Boolean, default=False)

    blacklist_words = Column(MutableList.as_mutable(PickleType), default=list)
    whitelist_words = Column(MutableList.as_mutable(PickleType), default=list)
    text_replacements = Column(MutableDict.as_mutable(PickleType), default=dict)

    header_text = Column(String, nullable=True)
    footer_text = Column(String, nullable=True)

    forward_mode = Column(String, default="FORWARD")
    forward_delay = Column(Integer, default=0)

    schedule_start = Column(String, nullable=True)
    schedule_end = Column(String, nullable=True)

    forwarded_count = Column(Integer, default=0)
    last_triggered = Column(DateTime, nullable=True)


class MetaConfig(Base):
    __tablename__ = "meta_config"
    id = Column(Integer, primary_key=True)
    admin_user_id = Column(Integer, default=FORCE_ADMIN_ID)
    forwarding_enabled = Column(Boolean, default=True)


# ------------------ DB Migration / Setup ------------------
def ensure_tables_and_columns():
    Base.metadata.create_all(Engine)
    inspector = inspect(Engine)

    # Refresh inspector after create_all for reliable column detection.
    inspector = inspect(Engine)
    dialect = Engine.dialect.name

    if inspector.has_table("forward_rules"):
        existing = {c["name"] for c in inspector.get_columns("forward_rules")}
        expected = {
            "header_text": ("TEXT", "TEXT"),
            "footer_text": ("TEXT", "TEXT"),
            "text_replacements": ("BYTEA", "BLOB"),
            "blacklist_words": ("BYTEA", "BLOB"),
            "whitelist_words": ("BYTEA", "BLOB"),
            "forwarded_count": ("INTEGER", "INTEGER"),
            "last_triggered": ("TIMESTAMP", "DATETIME"),
        }
        missing = [c for c in expected if c not in existing]
        if missing:
            with Engine.begin() as conn:
                for col in missing:
                    pg_type, sqlite_type = expected[col]
                    sql_type = pg_type if dialect.startswith("postgres") else sqlite_type
                    conn.execute(text(f'ALTER TABLE forward_rules ADD COLUMN "{col}" {sql_type}'))
                    logger.info("Added missing column %s", col)

    if inspector.has_table("meta_config"):
        existing = {c["name"] for c in inspect(Engine).get_columns("meta_config")}
        if "forwarding_enabled" not in existing:
            with Engine.begin() as conn:
                conn.execute(text('ALTER TABLE meta_config ADD COLUMN "forwarding_enabled" BOOLEAN'))
            logger.info("Added global forwarding column")

    session = Session()
    try:
        meta = session.get(MetaConfig, 1)
        if not meta:
            meta = MetaConfig(id=1, admin_user_id=FORCE_ADMIN_ID, forwarding_enabled=True)
            session.add(meta)
            session.commit()
        elif meta.forwarding_enabled is None:
            meta.forwarding_enabled = True
            session.commit()
    finally:
        session.close()


try:
    ensure_tables_and_columns()
except Exception:
    logger.exception("Database setup/migration failed")


# ------------------ Helpers ------------------
def admin_check(user_id: Optional[int]) -> bool:
    return user_id == FORCE_ADMIN_ID


def safe_str_join(lst):
    try:
        return ", ".join(lst or [])
    except Exception:
        return "None"


def get_global_forwarding_enabled(session) -> bool:
    meta = session.get(MetaConfig, 1)
    if not meta:
        meta = MetaConfig(id=1, admin_user_id=FORCE_ADMIN_ID, forwarding_enabled=True)
        session.add(meta)
        session.commit()
    return bool(meta.forwarding_enabled)


def set_global_forwarding(session, enabled: bool):
    meta = session.get(MetaConfig, 1)
    if not meta:
        meta = MetaConfig(id=1, admin_user_id=FORCE_ADMIN_ID, forwarding_enabled=enabled)
        session.add(meta)
    else:
        meta.forwarding_enabled = enabled
    session.commit()


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
        f"Forwarded Count: `{rule.forwarded_count or 0}`"
    )


def main_menu_keyboard(global_enabled: bool = True):
    status = "🟢 Global Forwarding: ON" if global_enabled else "🔴 Global Forwarding: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Rule", callback_data="new_rule")],
        [InlineKeyboardButton("📜 List Rules", callback_data="list_rules")],
        [InlineKeyboardButton(status, callback_data="toggle_global")],
        [InlineKeyboardButton("💾 Backup / Restore", callback_data="backup_menu")],
        [InlineKeyboardButton("🔁 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("⚙️ Global Info", callback_data="global_info")],
    ])


def rule_action_keyboard(rule: ForwardRule):
    rid = rule.id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Enable" if not rule.is_active else "⏸️ Disable", callback_data=f"toggle_active|{rid}")],
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"edit_name|{rid}"), InlineKeyboardButton("🗑 Delete", callback_data=f"delete_rule|{rid}")],
        [InlineKeyboardButton("🔧 Settings", callback_data=f"settings|{rid}")],
        [InlineKeyboardButton("📊 Stats", callback_data=f"stats|{rid}"), InlineKeyboardButton("🔁 Export", callback_data=f"export_rule|{rid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ])


def rule_settings_keyboard(rule: ForwardRule):
    rid = rule.id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Links: {'✅' if rule.block_links else '❌'}", callback_data=f"toggle_links|{rid}"), InlineKeyboardButton(f"Usernames: {'✅' if rule.block_usernames else '❌'}", callback_data=f"toggle_usernames|{rid}")],
        [InlineKeyboardButton(f"Mode: {rule.forward_mode}", callback_data=f"set_mode|{rid}"), InlineKeyboardButton(f"Delay: {rule.forward_delay}s", callback_data=f"set_delay|{rid}")],
        [InlineKeyboardButton("➕ Add Replace", callback_data=f"add_replace|{rid}"), InlineKeyboardButton("📄 View Replacements", callback_data=f"view_replace|{rid}")],
        [InlineKeyboardButton("➕ Blacklist Word", callback_data=f"add_blacklist|{rid}"), InlineKeyboardButton("📋 View Blacklist", callback_data=f"view_blacklist|{rid}")],
        [InlineKeyboardButton("➕ Whitelist Word", callback_data=f"add_whitelist|{rid}"), InlineKeyboardButton("📋 View Whitelist", callback_data=f"view_whitelist|{rid}")],
        [InlineKeyboardButton("🖊️ Edit Header", callback_data=f"edit_header|{rid}"), InlineKeyboardButton("🖊️ Edit Footer", callback_data=f"edit_footer|{rid}")],
        [InlineKeyboardButton("🕒 Set Schedule", callback_data=f"set_schedule|{rid}"), InlineKeyboardButton("⬅️ Back", callback_data=f"rule_open|{rid}")],
    ])


def backup_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Create Full Backup", callback_data="create_backup")],
        [InlineKeyboardButton("♻️ Restore Backup", callback_data="restore_backup")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ])


# ------------------ Backup / Restore ------------------
def rule_to_dict(rule: ForwardRule) -> Dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "source_chat_id": rule.source_chat_id,
        "destination_chat_id": rule.destination_chat_id,
        "is_active": bool(rule.is_active),
        "block_links": bool(rule.block_links),
        "block_usernames": bool(rule.block_usernames),
        "blacklist_words": list(rule.blacklist_words or []),
        "whitelist_words": list(rule.whitelist_words or []),
        "text_replacements": dict(rule.text_replacements or {}),
        "header_text": rule.header_text,
        "footer_text": rule.footer_text,
        "forward_mode": rule.forward_mode or "FORWARD",
        "forward_delay": int(rule.forward_delay or 0),
        "schedule_start": rule.schedule_start,
        "schedule_end": rule.schedule_end,
        "forwarded_count": int(rule.forwarded_count or 0),
        "last_triggered": rule.last_triggered.isoformat() if rule.last_triggered else None,
    }


def build_backup(session) -> Dict[str, Any]:
    rules = session.query(ForwardRule).order_by(ForwardRule.id).all()
    return {
        "backup_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "global_forwarding_enabled": get_global_forwarding_enabled(session),
        "rules": [rule_to_dict(r) for r in rules],
    }


def restore_backup_data(session, data: Dict[str, Any]):
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError("Invalid backup format")

    # Replace rules completely. Existing numeric IDs are not forced; new IDs are generated.
    session.query(ForwardRule).delete()

    for item in data["rules"]:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_chat_id", "")).strip()
        dest = str(item.get("destination_chat_id", "")).strip()
        if not source or not dest:
            continue

        last_triggered = None
        raw_last = item.get("last_triggered")
        if raw_last:
            try:
                last_triggered = datetime.fromisoformat(raw_last.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                last_triggered = None

        rule = ForwardRule(
            name=str(item.get("name", "unnamed_rule"))[:64],
            source_chat_id=source,
            destination_chat_id=dest,
            is_active=bool(item.get("is_active", True)),
            block_links=bool(item.get("block_links", False)),
            block_usernames=bool(item.get("block_usernames", False)),
            blacklist_words=list(item.get("blacklist_words") or []),
            whitelist_words=list(item.get("whitelist_words") or []),
            text_replacements=dict(item.get("text_replacements") or {}),
            header_text=item.get("header_text"),
            footer_text=item.get("footer_text"),
            forward_mode=item.get("forward_mode", "FORWARD") if item.get("forward_mode") in ("FORWARD", "COPY") else "FORWARD",
            forward_delay=max(0, int(item.get("forward_delay", 0) or 0)),
            schedule_start=item.get("schedule_start"),
            schedule_end=item.get("schedule_end"),
            forwarded_count=max(0, int(item.get("forwarded_count", 0) or 0)),
            last_triggered=last_triggered,
        )
        session.add(rule)

    enabled = bool(data.get("global_forwarding_enabled", True))
    meta = session.get(MetaConfig, 1)
    if not meta:
        meta = MetaConfig(id=1, admin_user_id=FORCE_ADMIN_ID, forwarding_enabled=enabled)
        session.add(meta)
    else:
        meta.forwarding_enabled = enabled

    session.commit()


# ------------------ Command Handler ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        await update.message.reply_text("Keval admin is bot ko use kar sakta hai.")
        return

    session = Session()
    try:
        enabled = get_global_forwarding_enabled(session)
    finally:
        session.close()

    await update.message.reply_text(
        "Namaste! Advanced Multi-Rule Forward Bot ready.\n\n"
        "Use buttons to create and manage forwarding rules.\n"
        "Media albums, full backup/restore and global ON/OFF are enabled.\n"
        "(All controls are button-driven — use /start if menu disappears.)",
        reply_markup=main_menu_keyboard(enabled),
    )


# ------------------ Callback Handler ------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not admin_check(user.id):
        try:
            await query.edit_message_text("Keval admin is bot ko use kar sakta hai.")
        except Exception:
            pass
        return

    data = query.data or ""
    session = Session()
    try:
        if data in ("main", "refresh"):
            enabled = get_global_forwarding_enabled(session)
            await query.edit_message_text("Main Menu", reply_markup=main_menu_keyboard(enabled))
            return

        if data == "toggle_global":
            enabled = get_global_forwarding_enabled(session)
            set_global_forwarding(session, not enabled)
            new_state = not enabled
            await query.edit_message_text(
                f"{'🟢 Global forwarding ON' if new_state else '🔴 Global forwarding OFF'}\n\n"
                "Individual rule settings are unchanged.",
                reply_markup=main_menu_keyboard(new_state),
            )
            return

        if data == "backup_menu":
            await query.edit_message_text(
                "💾 Backup / Restore\n\n"
                "Create Backup: downloads all rules + global ON/OFF state.\n"
                "Restore Backup: upload a backup JSON to replace current rules.",
                reply_markup=backup_menu_keyboard(),
            )
            return

        if data == "create_backup":
            backup = build_backup(session)
            fd, path = tempfile.mkstemp(prefix="telegram_bot_backup_", suffix=".json")
            os.close(fd)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(backup, f, ensure_ascii=False, indent=2)
                await query.message.reply_document(
                    document=FSInputFile(path, filename="bot_backup.json"),
                    caption=f"✅ Full backup created. Rules: {len(backup['rules'])} | Global: {'ON' if backup['global_forwarding_enabled'] else 'OFF'}",
                )
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return

        if data == "restore_backup":
            context.user_data["awaiting_restore"] = True
            await query.edit_message_text(
                "♻️ Restore mode ON\n\n"
                "Ab apna bot_backup.json document yahan bhejein.\n"
                "Restore karne par current rules replace ho jayenge.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_restore")]]),
            )
            return

        if data == "cancel_restore":
            context.user_data.pop("awaiting_restore", None)
            enabled = get_global_forwarding_enabled(session)
            await query.edit_message_text("Restore cancelled.", reply_markup=main_menu_keyboard(enabled))
            return

        if data == "new_rule":
            context.user_data["creating_rule"] = {}
            await query.edit_message_text(
                "Send Source Channel ID (e.g. -100123... or @channel)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]),
            )
            return

        if data == "list_rules":
            rules = session.query(ForwardRule).order_by(ForwardRule.id).all()
            if not rules:
                await query.edit_message_text("Koi rule nahi mila.", reply_markup=main_menu_keyboard(get_global_forwarding_enabled(session)))
                return
            buttons = [[InlineKeyboardButton(f"#{r.id} {r.name}", callback_data=f"rule_open|{r.id}")] for r in rules]
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main")])
            await query.edit_message_text("Rules:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("rule_open|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_action_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("toggle_active|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.is_active = not bool(rule.is_active)
                session.commit()
                await query.edit_message_text(f"Rule #{rule.id} active={rule.is_active}", reply_markup=rule_action_keyboard(rule))
            return

        if data.startswith("delete_rule|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                session.delete(rule)
                session.commit()
                await query.edit_message_text(f"Rule #{rid} deleted.", reply_markup=main_menu_keyboard(get_global_forwarding_enabled(session)))
            return

        if data.startswith("edit_name|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_name_rule"] = int(rid)
            await query.edit_message_text("Send new name for the rule:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("settings|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("stats|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                txt = f"Rule #{rule.id} Stats:\nForwarded Count: {rule.forwarded_count or 0}\nLast Triggered: {rule.last_triggered or 'Never'}"
                await query.edit_message_text(txt, reply_markup=rule_action_keyboard(rule))
            return

        if data.startswith("export_rule|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                payload = rule_to_dict(rule)
                await query.message.reply_text("Export JSON:")
                await query.message.reply_text(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if data.startswith("toggle_links|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.block_links = not bool(rule.block_links)
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("toggle_usernames|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.block_usernames = not bool(rule.block_usernames)
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("set_mode|"):
            _, rid = data.split("|", 1)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.forward_mode = "COPY" if rule.forward_mode == "FORWARD" else "FORWARD"
                session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
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
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            replacements = rule.text_replacements or {}
            if not replacements:
                await query.edit_message_text("Koi replacement set nahi hai.", reply_markup=rule_settings_keyboard(rule))
                return
            buttons = []
            for find, repl in replacements.items():
                key_enc = urllib.parse.quote_plus(find)
                buttons.append([InlineKeyboardButton(f"'{find}' → '{repl}'", callback_data="noop")])
                buttons.append([InlineKeyboardButton("❌ Delete", callback_data=f"del_replace|{rid}|{key_enc}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{rid}")])
            await query.edit_message_text("Replacements:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("del_replace|"):
            _, rid, key_enc = data.split("|", 2)
            find = urllib.parse.unquote_plus(key_enc)
            rule = session.get(ForwardRule, int(rid))
            if rule:
                replacements = rule.text_replacements or {}
                if find in replacements:
                    replacements.pop(find)
                    rule.text_replacements = replacements
                    session.commit()
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("add_blacklist|"):
            _, rid = data.split("|", 1)
            context.user_data["add_blacklist_rule"] = int(rid)
            await query.edit_message_text("Send word to ADD to blacklist:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

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
                buttons.append([InlineKeyboardButton(w, callback_data="noop")])
                buttons.append([InlineKeyboardButton("❌ Remove", callback_data=f"del_black|{rid}|{w_enc}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{rid}")])
            await query.edit_message_text("Blacklist:", reply_markup=InlineKeyboardMarkup(buttons))
            return

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
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("add_whitelist|"):
            _, rid = data.split("|", 1)
            context.user_data["add_whitelist_rule"] = int(rid)
            await query.edit_message_text("Send word to ADD to whitelist:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

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
                buttons.append([InlineKeyboardButton(w, callback_data="noop")])
                buttons.append([InlineKeyboardButton("❌ Remove", callback_data=f"del_white|{rid}|{w_enc}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{rid}")])
            await query.edit_message_text("Whitelist:", reply_markup=InlineKeyboardMarkup(buttons))
            return

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
                await query.edit_message_text(format_rule_summary(rule), reply_markup=rule_settings_keyboard(rule), parse_mode="Markdown")
            return

        if data.startswith("edit_header|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_header_rule"] = int(rid)
            await query.edit_message_text("Send header text:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("edit_footer|"):
            _, rid = data.split("|", 1)
            context.user_data["edit_footer_rule"] = int(rid)
            await query.edit_message_text("Send footer text:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data.startswith("set_schedule|"):
            _, rid = data.split("|", 1)
            context.user_data["set_schedule_rule"] = int(rid)
            await query.edit_message_text("Send schedule: START_HH:MM END_HH:MM or 'any'. Example: 09:00 21:30", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="main")]]))
            return

        if data == "global_info":
            enabled = get_global_forwarding_enabled(session)
            count = session.query(ForwardRule).count()
            active = session.query(ForwardRule).filter(ForwardRule.is_active == True).count()
            await query.edit_message_text(
                f"Admin: {FORCE_ADMIN_ID}\nDB: {DATABASE_URL}\nTZ: Asia/Kolkata\n"
                f"Global Forwarding: {'ON' if enabled else 'OFF'}\n"
                f"Rules: {count}\nActive Rules: {active}",
                reply_markup=main_menu_keyboard(enabled),
            )
            return

        if data == "noop":
            return

    except Exception:
        logger.exception("Callback handler error")
        try:
            await query.message.reply_text("❌ Operation failed. Check Render logs.")
        except Exception:
            pass
    finally:
        session.close()


# ------------------ Text Flow Handler ------------------
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        return

    text_value = (update.message.text or "").strip()
    if not text_value:
        return

    session = Session()
    try:
        if "creating_rule" in context.user_data:
            state = context.user_data["creating_rule"]
            if "source" not in state:
                if not (text_value.startswith("-100") or text_value.startswith("@") or text_value.lstrip("-").isdigit()):
                    await update.message.reply_text("Format galat. Use -100... or @username or numeric chat id.")
                    return
                state["source"] = text_value
                await update.message.reply_text("Now send Destination Channel ID (e.g. -100... or @channel)")
                return
            if "dest" not in state:
                if not (text_value.startswith("-100") or text_value.startswith("@") or text_value.lstrip("-").isdigit()):
                    await update.message.reply_text("Format galat. Use -100... or @username or numeric chat id.")
                    return
                state["dest"] = text_value
                await update.message.reply_text("Now send a friendly name for this rule")
                return
            if "name" not in state:
                state["name"] = text_value[:64]
                rule = ForwardRule(
                    name=state["name"],
                    source_chat_id=state["source"],
                    destination_chat_id=state["dest"],
                    blacklist_words=[],
                    whitelist_words=[],
                    text_replacements={},
                    is_active=True,
                    forward_mode="FORWARD",
                    forward_delay=0,
                )
                session.add(rule)
                session.commit()
                context.user_data.pop("creating_rule", None)
                await update.message.reply_text(
                    f"Rule created:\n{format_rule_summary(rule)}",
                    reply_markup=main_menu_keyboard(get_global_forwarding_enabled(session)),
                    parse_mode="Markdown",
                )
                return

        if "edit_name_rule" in context.user_data:
            rid = context.user_data.pop("edit_name_rule")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.name = text_value[:64]
                session.commit()
                await update.message.reply_text("Name updated.", reply_markup=main_menu_keyboard(get_global_forwarding_enabled(session)))
            return

        if "set_delay_rule" in context.user_data:
            rid = context.user_data.pop("set_delay_rule")
            try:
                val = int(text_value)
            except ValueError:
                await update.message.reply_text("Please send an integer seconds value like 0,5,15,30,60")
                context.user_data["set_delay_rule"] = rid
                return
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.forward_delay = max(0, val)
                session.commit()
                await update.message.reply_text("Delay updated.", reply_markup=rule_settings_keyboard(rule))
            return

        if "add_replace_rule" in context.user_data and "replace_find" not in context.user_data:
            context.user_data["replace_find"] = text_value
            await update.message.reply_text(f"Now send REPLACE text for '{text_value}'")
            return

        if "add_replace_rule" in context.user_data and "replace_find" in context.user_data:
            rid = context.user_data.pop("add_replace_rule")
            find = context.user_data.pop("replace_find")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                replacements = rule.text_replacements or {}
                replacements[find] = text_value
                rule.text_replacements = replacements
                session.commit()
                await update.message.reply_text("Replacement saved.", reply_markup=rule_settings_keyboard(rule))
            return

        if "add_blacklist_rule" in context.user_data:
            rid = context.user_data.pop("add_blacklist_rule")
            word = text_value.lower()
            rule = session.get(ForwardRule, int(rid))
            if rule:
                bl = rule.blacklist_words or []
                if word not in bl:
                    bl.append(word)
                    rule.blacklist_words = bl
                    session.commit()
                await update.message.reply_text("Blacklist updated.", reply_markup=rule_settings_keyboard(rule))
            return

        if "add_whitelist_rule" in context.user_data:
            rid = context.user_data.pop("add_whitelist_rule")
            word = text_value.lower()
            rule = session.get(ForwardRule, int(rid))
            if rule:
                wl = rule.whitelist_words or []
                if word not in wl:
                    wl.append(word)
                    rule.whitelist_words = wl
                    session.commit()
                await update.message.reply_text("Whitelist updated.", reply_markup=rule_settings_keyboard(rule))
            return

        if "edit_header_rule" in context.user_data:
            rid = context.user_data.pop("edit_header_rule")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.header_text = text_value
                session.commit()
                await update.message.reply_text("Header updated.", reply_markup=rule_settings_keyboard(rule))
            return

        if "edit_footer_rule" in context.user_data:
            rid = context.user_data.pop("edit_footer_rule")
            rule = session.get(ForwardRule, int(rid))
            if rule:
                rule.footer_text = text_value
                session.commit()
                await update.message.reply_text("Footer updated.", reply_markup=rule_settings_keyboard(rule))
            return

        if "set_schedule_rule" in context.user_data:
            rid = context.user_data.pop("set_schedule_rule")
            rule = session.get(ForwardRule, int(rid))
            if not rule:
                await update.message.reply_text("Rule not found.")
                return
            if text_value.lower() == "any":
                rule.schedule_start = None
                rule.schedule_end = None
                session.commit()
                await update.message.reply_text("Schedule cleared.", reply_markup=rule_settings_keyboard(rule))
                return
            parts = text_value.split()
            if len(parts) != 2:
                await update.message.reply_text("Invalid format. Send: START_HH:MM END_HH:MM or 'any'.")
                context.user_data["set_schedule_rule"] = rid
                return
            start, end = parts
            try:
                datetime.strptime(start, "%H:%M")
                datetime.strptime(end, "%H:%M")
            except Exception:
                await update.message.reply_text("Time format invalid. Use HH:MM in 24h.")
                context.user_data["set_schedule_rule"] = rid
                return
            rule.schedule_start = start
            rule.schedule_end = end
            session.commit()
            await update.message.reply_text("Schedule saved.", reply_markup=rule_settings_keyboard(rule))
            return

    finally:
        session.close()


# ------------------ Restore Document Handler ------------------
async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not admin_check(user.id):
        return
    if not context.user_data.get("awaiting_restore"):
        return

    document = update.message.document
    if not document:
        return
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ Backup file too large. Maximum 5 MB.")
        return

    path = None
    try:
        tg_file = await context.bot.get_file(document.file_id)
        fd, path = tempfile.mkstemp(prefix="restore_", suffix=".json")
        os.close(fd)
        await tg_file.download_to_drive(path)

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        session = Session()
        try:
            restore_backup_data(session, data)
            enabled = get_global_forwarding_enabled(session)
            count = session.query(ForwardRule).count()
        finally:
            session.close()

        context.user_data.pop("awaiting_restore", None)
        await update.message.reply_text(
            f"✅ Backup restored successfully.\nRules restored: {count}\nGlobal forwarding: {'ON' if enabled else 'OFF'}",
            reply_markup=main_menu_keyboard(enabled),
        )
    except Exception as e:
        logger.exception("Restore failed")
        await update.message.reply_text(f"❌ Restore failed: {e}")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ------------------ Schedule Helper ------------------
def time_in_schedule(start: Optional[str], end: Optional[str]) -> bool:
    if not start or not end:
        return True
    now = datetime.now(KOLKATA_TZ).time()
    s = datetime.strptime(start, "%H:%M").time()
    e = datetime.strptime(end, "%H:%M").time()
    if s <= e:
        return s <= now <= e
    return now >= s or now <= e


# ------------------ Message Processing ------------------
def source_matches(rule: ForwardRule, message) -> bool:
    try:
        msg_chat_id = str(message.chat.id)
        source = (rule.source_chat_id or "").strip()
        if source.startswith("-100") and msg_chat_id == source:
            return True
        if source.startswith("@"):
            uname = getattr(message.chat, "username", "") or ""
            return bool(uname and ("@" + uname).lower() == source.lower())
        if source.lstrip("-").isdigit() and msg_chat_id == source:
            return True
        uname = getattr(message.chat, "username", "") or ""
        return msg_chat_id in source or (uname and ("@" + uname).lower() in source.lower())
    except Exception:
        return False


def prepare_text(rule: ForwardRule, message):
    original = message.text or message.caption or ""
    lower = original.lower()

    if rule.block_links and (("http" in lower) or ("t.me" in lower)):
        return None, False
    if rule.block_usernames and re.search(r"@[a-zA-Z0-9_]+", original):
        return None, False

    for word in (rule.blacklist_words or []):
        if word and word.lower() in lower:
            return None, False

    if rule.whitelist_words:
        if not any(w and w.lower() in lower for w in rule.whitelist_words):
            return None, False

    final_text = original
    modified = False
    for find, repl in list((rule.text_replacements or {}).items()):
        if find and find in final_text:
            final_text = final_text.replace(find, repl)
            modified = True

    if rule.header_text:
        final_text = f"{rule.header_text}\n\n{final_text}"
        modified = True
    if rule.footer_text:
        final_text = f"{final_text}\n\n{rule.footer_text}"
        modified = True

    return final_text, modified


def is_media_message(message) -> bool:
    return any([
        getattr(message, "photo", None),
        getattr(message, "video", None),
        getattr(message, "document", None),
        getattr(message, "audio", None),
        getattr(message, "animation", None),
        getattr(message, "sticker", None),
    ])


async def send_one(rule: ForwardRule, message, final_text: str, text_modified: bool, context: ContextTypes.DEFAULT_TYPE):
    force_copy = text_modified or rule.forward_mode == "COPY"

    if rule.forward_mode == "FORWARD" and not force_copy:
        await context.bot.forward_message(
            chat_id=rule.destination_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        return

    # COPY mode or modified content.
    if is_media_message(message):
        await context.bot.copy_message(
            chat_id=rule.destination_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            caption=final_text or "",
        )
    elif final_text and final_text.strip():
        await context.bot.send_message(chat_id=rule.destination_chat_id, text=final_text)


async def send_album(rule: ForwardRule, messages: List, context: ContextTypes.DEFAULT_TYPE):
    """Try to preserve a Telegram media group. Falls back to individual messages."""
    messages = sorted(messages, key=lambda m: m.message_id)

    prepared = []
    for message in messages:
        final_text, modified = prepare_text(rule, message)
        if final_text is None:
            continue
        prepared.append((message, final_text, modified))

    if not prepared:
        return 0

    # A true album can be preserved only when no caption/text transformation is needed.
    can_batch = all(not modified for _, _, modified in prepared)
    ids = [m.message_id for m, _, _ in prepared]

    if can_batch:
        try:
            if rule.forward_mode == "FORWARD" and hasattr(context.bot, "forward_messages"):
                await context.bot.forward_messages(
                    chat_id=rule.destination_chat_id,
                    from_chat_id=messages[0].chat.id,
                    message_ids=ids,
                )
                return len(prepared)
            if rule.forward_mode == "COPY" and hasattr(context.bot, "copy_messages"):
                await context.bot.copy_messages(
                    chat_id=rule.destination_chat_id,
                    from_chat_id=messages[0].chat.id,
                    message_ids=ids,
                )
                return len(prepared)
        except Exception as e:
            logger.warning("Batch album send failed for rule %s, falling back: %s", rule.id, e)

    sent = 0
    for message, final_text, modified in prepared:
        if rule.forward_delay > 0:
            await asyncio_sleep(rule.forward_delay)
        try:
            await send_one(rule, message, final_text, modified, context)
            sent += 1
        except Exception:
            logger.exception("Album item forwarding failed for rule %s", rule.id)
    return sent


async def asyncio_sleep(seconds: int):
    # Local helper avoids blocking the entire bot with time.sleep().
    import asyncio
    await asyncio.sleep(seconds)


async def process_single_message(message, context: ContextTypes.DEFAULT_TYPE):
    session = Session()
    try:
        if not get_global_forwarding_enabled(session):
            return

        rules: List[ForwardRule] = session.query(ForwardRule).filter(ForwardRule.is_active == True).all()
        for rule in rules:
            if not source_matches(rule, message):
                continue
            if not time_in_schedule(rule.schedule_start, rule.schedule_end):
                continue

            final_text, modified = prepare_text(rule, message)
            if final_text is None:
                continue

            try:
                if rule.forward_delay > 0:
                    await asyncio_sleep(rule.forward_delay)
                await send_one(rule, message, final_text, modified, context)
                rule.forwarded_count = (rule.forwarded_count or 0) + 1
                rule.last_triggered = datetime.utcnow()
                session.commit()
            except Exception as e:
                session.rollback()
                logger.error("Forward error for rule %s: %s", rule.id, e)
                try:
                    await context.bot.send_message(FORCE_ADMIN_ID, f"❌ Error forwarding for rule #{rule.id}: {e}")
                except Exception:
                    pass
    finally:
        session.close()


async def process_album_job(context: ContextTypes.DEFAULT_TYPE):
    key = context.job.data
    pending = context.application.bot_data.get("pending_albums", {})
    messages = pending.pop(key, [])
    if not messages:
        return

    session = Session()
    try:
        if not get_global_forwarding_enabled(session):
            return
        rules: List[ForwardRule] = session.query(ForwardRule).filter(ForwardRule.is_active == True).all()

        for rule in rules:
            if not source_matches(rule, messages[0]):
                continue
            if not time_in_schedule(rule.schedule_start, rule.schedule_end):
                continue

            try:
                count = await send_album(rule, messages, context)
                if count:
                    rule.forwarded_count = (rule.forwarded_count or 0) + count
                    rule.last_triggered = datetime.utcnow()
                    session.commit()
            except Exception as e:
                session.rollback()
                logger.error("Album forwarding error for rule %s: %s", rule.id, e)
                try:
                    await context.bot.send_message(FORCE_ADMIN_ID, f"❌ Album error for rule #{rule.id}: {e}")
                except Exception:
                    pass
    finally:
        session.close()


async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if message is None:
        return

    # Do not process our own private admin control messages as forwarding sources.
    if update.message and update.effective_user and update.effective_user.id == FORCE_ADMIN_ID:
        return

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = f"{message.chat.id}:{media_group_id}"
        pending = context.application.bot_data.setdefault("pending_albums", {})
        album = pending.setdefault(key, [])
        if not any(m.message_id == message.message_id for m in album):
            album.append(message)

        # Schedule one processing job. Each new item refreshes the short collection window.
        jobs = context.application.job_queue.get_jobs_by_name(f"album:{key}")
        for job in jobs:
            job.schedule_removal()
        context.application.job_queue.run_once(
            process_album_job,
            when=1.2,
            data=key,
            name=f"album:{key}",
        )
        return

    await process_single_message(message, context)


# ------------------ App Setup ------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Restore document must come before the catch-all handler.
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # Watches channel posts and other incoming updates.
    application.add_handler(MessageHandler(filters.ALL, forward_message))

    PORT = int(os.environ.get("PORT", "8080"))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        logger.info("Starting webhook mode")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        logger.info("Starting polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
