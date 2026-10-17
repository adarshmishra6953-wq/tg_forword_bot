#!/usr/bin/env python3
"""
Advanced Multi-Rule Telegram Auto-Forward Bot - Render ready

Features:
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
- Media album support
- Full backup/restore
- Global forwarding ON/OFF
- Keyword-based inline button system
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
    from telegram import (
        Update,
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        InputMediaPhoto,
        InputMediaVideo,
        InputMediaDocument,
        InputMediaAudio,
        FSInputFile,
    )
except ImportError:
    from telegram import (
        Update,
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        InputMediaPhoto,
        InputMediaVideo,
        InputMediaDocument,
        InputMediaAudio,
        InputFile,
    )
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

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    PickleType,
    DateTime,
    inspect,
    text,
)

from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.mutable import MutableDict, MutableList


# ------------------ Logging ------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Telegram Bot API supported inline-button styles.
BUTTON_COLORS = {
    "default": "Default",
    "primary": "Blue",
    "success": "Green",
    "danger": "Red",
}


def make_url_button(button: Dict[str, Any]) -> InlineKeyboardButton:
    """Create a URL button, using style only when the installed PTB supports it."""
    label = str(button.get("text") or "Link")[:64]
    url = str(button.get("url") or "").strip()
    if not url:
        raise ValueError("URL button has no URL")
    color = button.get("color", "default")
    # Validate URL scheme before sending it to Telegram.
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid button URL: {url}")
    style = color if color in ("primary", "success", "danger") else None
    if style:
        try:
            return InlineKeyboardButton(text=label, url=url, style=style)
        except TypeError:
            # Older python-telegram-bot versions don't support the style field.
            pass
    return InlineKeyboardButton(text=label, url=url)


# ------------------ Config ------------------

FORCE_ADMIN_ID = 1695450646

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set.")
    raise SystemExit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1,
    )

if not DATABASE_URL:
    DATABASE_URL = "sqlite:///bot_rules.db"

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")

Engine = create_engine(
    DATABASE_URL,
    connect_args=(
        {"check_same_thread": False}
        if DATABASE_URL.startswith("sqlite")
        else {}
    ),
    pool_pre_ping=True,
)

Base = declarative_base()
Session = sessionmaker(bind=Engine)


# ------------------ Database Models ------------------

class ForwardRule(Base):
    __tablename__ = "forward_rules"

    id = Column(Integer, primary_key=True)

    name = Column(String, default="unnamed_rule")

    source_chat_id = Column(String, nullable=False)

    destination_chat_id = Column(String, nullable=False)

    is_active = Column(Boolean, default=True)

    block_links = Column(Boolean, default=False)

    block_usernames = Column(Boolean, default=False)

    blacklist_words = Column(
        MutableList.as_mutable(PickleType),
        default=list,
    )

    whitelist_words = Column(
        MutableList.as_mutable(PickleType),
        default=list,
    )

    # New keyword-button settings
    button_text = Column(String, nullable=True)

    button_url = Column(String, nullable=True)

    button_trigger_words = Column(
        MutableList.as_mutable(PickleType),
        default=list,
    )

    # Multiple keyword buttons; each item stores text, URL, keywords,
    # color/style, position, enabled flag and a stable ID.
    button_configs = Column(
        MutableList.as_mutable(PickleType),
        default=list,
    )

    text_replacements = Column(
        MutableDict.as_mutable(PickleType),
        default=dict,
    )

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

    admin_user_id = Column(
        Integer,
        default=FORCE_ADMIN_ID,
    )

    forwarding_enabled = Column(
        Boolean,
        default=True,
    )


# ------------------ Database Setup ------------------

def ensure_tables_and_columns():

    Base.metadata.create_all(Engine)

    inspector = inspect(Engine)

    dialect = Engine.dialect.name

    if inspector.has_table("forward_rules"):

        existing = {
            c["name"]
            for c in inspector.get_columns("forward_rules")
        }

        expected = {
            "header_text": ("TEXT", "TEXT"),
            "footer_text": ("TEXT", "TEXT"),
            "text_replacements": ("BYTEA", "BLOB"),
            "blacklist_words": ("BYTEA", "BLOB"),
            "whitelist_words": ("BYTEA", "BLOB"),
            "button_text": ("TEXT", "TEXT"),
            "button_url": ("TEXT", "TEXT"),
            "button_trigger_words": ("BYTEA", "BLOB"),
            "button_configs": ("BYTEA", "BLOB"),
            "forwarded_count": ("INTEGER", "INTEGER"),
            "last_triggered": ("TIMESTAMP", "DATETIME"),
        }

        missing = [
            col
            for col in expected
            if col not in existing
        ]

        if missing:

            with Engine.begin() as conn:

                for col in missing:

                    pg_type, sqlite_type = expected[col]

                    sql_type = (
                        pg_type
                        if dialect.startswith("postgres")
                        else sqlite_type
                    )

                    conn.execute(
                        text(
                            f'ALTER TABLE forward_rules '
                            f'ADD COLUMN "{col}" {sql_type}'
                        )
                    )

                    logger.info(
                        "Added missing column %s",
                        col,
                    )

    if inspector.has_table("meta_config"):

        existing = {
            c["name"]
            for c in inspect(Engine).get_columns(
                "meta_config"
            )
        }

        if "forwarding_enabled" not in existing:

            with Engine.begin() as conn:

                conn.execute(
                    text(
                        'ALTER TABLE meta_config '
                        'ADD COLUMN "forwarding_enabled" BOOLEAN'
                    )
                )

    session = Session()

    try:

        meta = session.get(MetaConfig, 1)

        if not meta:

            meta = MetaConfig(
                id=1,
                admin_user_id=FORCE_ADMIN_ID,
                forwarding_enabled=True,
            )

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

        meta = MetaConfig(
            id=1,
            admin_user_id=FORCE_ADMIN_ID,
            forwarding_enabled=True,
        )

        session.add(meta)
        session.commit()

    return bool(meta.forwarding_enabled)


def set_global_forwarding(session, enabled: bool):

    meta = session.get(MetaConfig, 1)

    if not meta:

        meta = MetaConfig(
            id=1,
            admin_user_id=FORCE_ADMIN_ID,
            forwarding_enabled=enabled,
        )

        session.add(meta)

    else:
        meta.forwarding_enabled = enabled

    session.commit()


def get_button_configs(rule: ForwardRule) -> List[Dict[str, Any]]:
    """Return normalized multi-button settings, migrating the legacy single button in memory."""
    raw = getattr(rule, "button_configs", None) or []
    configs: List[Dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        try:
            button_id = int(item.get("id", index))
        except (TypeError, ValueError):
            button_id = index
        try:
            position = int(item.get("position", index))
        except (TypeError, ValueError):
            position = index
        configs.append({
            "id": button_id,
            "text": str(item.get("text") or item.get("button_text") or "Link")[:64],
            "url": str(item.get("url") or item.get("button_url") or ""),
            "keywords": list(item.get("keywords") or item.get("trigger_words") or []),
            "color": item.get("color") if item.get("color") in BUTTON_COLORS else "default",
            "position": max(1, position),
            "enabled": bool(item.get("enabled", True)),
        })
    if configs:
        return sorted(configs, key=lambda b: (b["position"], b["id"]))

    # Backward compatibility with rules created before multi-button support.
    legacy_text = getattr(rule, "button_text", None)
    legacy_url = getattr(rule, "button_url", None)
    if legacy_text and legacy_url:
        keywords = getattr(rule, "button_trigger_words", None) or []
        if isinstance(keywords, str):
            keywords = [word.strip() for word in keywords.split(",") if word.strip()]
        return [{
            "id": 1, "text": str(legacy_text)[:64], "url": str(legacy_url),
            "keywords": list(keywords), "color": "default", "position": 1,
            "enabled": True,
        }]
    return []


def save_button_configs(rule: ForwardRule, configs: List[Dict[str, Any]]) -> None:
    """Persist normalized multi-button settings and mirror the first button to legacy fields."""
    normalized = []
    for index, item in enumerate(configs, start=1):
        if not isinstance(item, dict):
            continue
        normalized.append({
            "id": int(item.get("id", index)),
            "text": str(item.get("text") or "Link")[:64],
            "url": str(item.get("url") or ""),
            "keywords": list(item.get("keywords") or []),
            "color": item.get("color") if item.get("color") in BUTTON_COLORS else "default",
            "position": max(1, int(item.get("position", index))),
            "enabled": bool(item.get("enabled", True)),
        })
    rule.button_configs = normalized
    # Keep legacy columns populated for compatibility with older code/backups.
    first = normalized[0] if normalized else None
    rule.button_text = first["text"] if first else None
    rule.button_url = first["url"] if first else None
    rule.button_trigger_words = list(first["keywords"]) if first else []




def next_button_id(configs: List[Dict[str, Any]]) -> int:
    """Return a stable positive ID that is not currently used by any button."""
    used = set()
    for item in configs or []:
        try:
            used.add(int(item.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def button_management_keyboard(rule: ForwardRule) -> InlineKeyboardMarkup:
    """Main keyboard for adding, viewing and bulk-managing URL buttons."""
    configs = get_button_configs(rule)
    enabled_count = sum(1 for item in configs if item.get("enabled", True))
    rid = rule.id
    rows = [
        [InlineKeyboardButton("➕ Add Button", callback_data=f"btn_add|{rid}")],
        [InlineKeyboardButton(
            f"📋 View / Edit Buttons ({len(configs)})",
            callback_data=f"btn_list|{rid}",
        )],
    ]
    if configs:
        if enabled_count:
            rows.append([InlineKeyboardButton(
                "⛔ Disable All Buttons",
                callback_data=f"btn_disable_all|{rid}",
            )])
        if enabled_count < len(configs):
            rows.append([InlineKeyboardButton(
                "✅ Enable All Buttons",
                callback_data=f"btn_enable_all|{rid}",
            )])
    rows.append([InlineKeyboardButton(
        "⬅️ Back to Settings",
        callback_data=f"settings|{rid}",
    )])
    return InlineKeyboardMarkup(rows)


def button_list_keyboard(rule: ForwardRule) -> InlineKeyboardMarkup:
    """Keyboard listing each button with a stable callback identifier."""
    rid = rule.id
    configs = get_button_configs(rule)
    rows = []
    for button in sorted(configs, key=lambda b: (int(b.get("position", 1)), int(b.get("id", 1)))):
        status = "🟢" if button.get("enabled", True) else "🔴"
        label = f"{status} {button.get('position', 1)}. {button.get('text', 'Link')}"
        rows.append([InlineKeyboardButton(
            label[:64],
            callback_data=f"btn_item|{rid}|{int(button['id'])}",
        )])
    rows.append([InlineKeyboardButton(
        "⬅️ Back",
        callback_data=f"button_manage|{rid}",
    )])
    return InlineKeyboardMarkup(rows)


def single_button_keyboard(rule_id: int, button_id: int) -> InlineKeyboardMarkup:
    """Actions available for one configured button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Enable / Disable", callback_data=f"btn_toggle|{rule_id}|{button_id}")],
        [InlineKeyboardButton("↕️ Change Position", callback_data=f"btn_position|{rule_id}|{button_id}")],
        [InlineKeyboardButton("🎨 Change Color", callback_data=f"btn_color|{rule_id}|{button_id}")],
        [InlineKeyboardButton("🗑 Delete This Button", callback_data=f"btn_delete|{rule_id}|{button_id}")],
        [InlineKeyboardButton("⬅️ Back to Button List", callback_data=f"btn_list|{rule_id}")],
    ])


def format_rule_summary(rule: ForwardRule) -> str:

    start = rule.schedule_start or "Any"

    end = rule.schedule_end or "Any"

    return (
        f"Rule #{rule.id} — {rule.name}\n"
        f"Source: `{rule.source_chat_id}` → "
        f"Dest: `{rule.destination_chat_id}`\n"
        f"Active: `{rule.is_active}` | "
        f"Mode: `{rule.forward_mode}` | "
        f"Delay: `{rule.forward_delay}s`\n"
        f"LinksBlocked: `{rule.block_links}` | "
        f"UsernamesBlocked: `{rule.block_usernames}`\n"
        f"Blacklist: `{safe_str_join(rule.blacklist_words) or 'None'}` | "
        f"Whitelist: `{safe_str_join(rule.whitelist_words) or 'None'}`\n"
        f"Header: `{(rule.header_text[:40] + '...') if rule.header_text else 'None'}` | "
        f"Footer: `{(rule.footer_text[:40] + '...') if rule.footer_text else 'None'}`\n"
        f"Buttons: `{len(get_button_configs(rule))} configured` | "
        f"Enabled: `{sum(1 for b in get_button_configs(rule) if b.get('enabled', True))}`\\n"
        f"Replacements: `{len(rule.text_replacements or {})} rules` | "
        f"Schedule: `{start}-{end}`\n"
        f"Forwarded Count: `{rule.forwarded_count or 0}`"
    )


# ------------------ Main Menu ------------------

def main_menu_keyboard(global_enabled: bool = True):

    status = (
        "🟢 Global Forwarding: ON"
        if global_enabled
        else "🔴 Global Forwarding: OFF"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ New Rule",
                callback_data="new_rule",
            )
        ],
        [
            InlineKeyboardButton(
                "📜 List Rules",
                callback_data="list_rules",
            )
        ],
        [
            InlineKeyboardButton(
                status,
                callback_data="toggle_global",
            )
        ],
        [
            InlineKeyboardButton(
                "💾 Backup / Restore",
                callback_data="backup_menu",
            )
        ],
        [
            InlineKeyboardButton(
                "🔁 Refresh",
                callback_data="refresh",
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Global Info",
                callback_data="global_info",
            )
        ],
    ])


def rule_action_keyboard(rule: ForwardRule):

    rid = rule.id

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶️ Enable" if not rule.is_active else "⏸️ Disable",
                callback_data=f"toggle_active|{rid}",
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Edit Name",
                callback_data=f"edit_name|{rid}",
            ),
            InlineKeyboardButton(
                "🗑 Delete",
                callback_data=f"delete_rule|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔧 Settings",
                callback_data=f"settings|{rid}",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Stats",
                callback_data=f"stats|{rid}",
            ),
            InlineKeyboardButton(
                "🔁 Export",
                callback_data=f"export_rule|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="main",
            )
        ],
    ])


def rule_settings_keyboard(rule: ForwardRule):

    rid = rule.id

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Links: {'✅' if rule.block_links else '❌'}",
                callback_data=f"toggle_links|{rid}",
            ),
            InlineKeyboardButton(
                f"Usernames: {'✅' if rule.block_usernames else '❌'}",
                callback_data=f"toggle_usernames|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                f"Mode: {rule.forward_mode}",
                callback_data=f"set_mode|{rid}",
            ),
            InlineKeyboardButton(
                f"Delay: {rule.forward_delay}s",
                callback_data=f"set_delay|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Add Replace",
                callback_data=f"add_replace|{rid}",
            ),
            InlineKeyboardButton(
                "📄 View Replacements",
                callback_data=f"view_replace|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Blacklist Word",
                callback_data=f"add_blacklist|{rid}",
            ),
            InlineKeyboardButton(
                "📋 View Blacklist",
                callback_data=f"view_blacklist|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Whitelist Word",
                callback_data=f"add_whitelist|{rid}",
            ),
            InlineKeyboardButton(
                "📋 View Whitelist",
                callback_data=f"view_whitelist|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🖊️ Edit Header",
                callback_data=f"edit_header|{rid}",
            ),
            InlineKeyboardButton(
                "🖊️ Edit Footer",
                callback_data=f"edit_footer|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔘 Manage Multiple Buttons",
                callback_data=f"button_manage|{rid}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🕒 Set Schedule",
                callback_data=f"set_schedule|{rid}",
            ),
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"rule_open|{rid}",
            ),
        ],
    ])


def backup_menu_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💾 Create Full Backup",
                callback_data="create_backup",
            )
        ],
        [
            InlineKeyboardButton(
                "♻️ Restore Backup",
                callback_data="restore_backup",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="main",
            )
        ],
    ])


# भाग 1 यहाँ समाप्त होता है।
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
        "button_text": rule.button_text,
        "button_url": rule.button_url,
        "button_trigger_words": list(rule.button_trigger_words or []),
        "button_configs": list(get_button_configs(rule)),
        "forwarded_count": int(rule.forwarded_count or 0),
        "last_triggered": (
            rule.last_triggered.isoformat()
            if rule.last_triggered else None
        ),
    }


def build_backup(session) -> Dict[str, Any]:
    rules = session.query(ForwardRule).order_by(
        ForwardRule.id
    ).all()

    return {
        "backup_version": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "global_forwarding_enabled": get_global_forwarding_enabled(
            session
        ),
        "rules": [rule_to_dict(r) for r in rules],
    }


def restore_backup_data(session, data: Dict[str, Any]):

    if not isinstance(data, dict) or not isinstance(
        data.get("rules"), list
    ):
        raise ValueError("Invalid backup format")

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
                last_triggered = datetime.fromisoformat(
                    raw_last.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                last_triggered = None

        rule = ForwardRule(
            name=str(item.get("name", "unnamed_rule"))[:64],
            source_chat_id=source,
            destination_chat_id=dest,
            is_active=bool(item.get("is_active", True)),
            block_links=bool(item.get("block_links", False)),
            block_usernames=bool(
                item.get("block_usernames", False)
            ),
            blacklist_words=list(
                item.get("blacklist_words") or []
            ),
            whitelist_words=list(
                item.get("whitelist_words") or []
            ),
            text_replacements=dict(
                item.get("text_replacements") or {}
            ),
            header_text=item.get("header_text"),
            footer_text=item.get("footer_text"),
            forward_mode=(
                item.get("forward_mode", "FORWARD")
                if item.get("forward_mode") in ("FORWARD", "COPY")
                else "FORWARD"
            ),
            forward_delay=max(
                0, int(item.get("forward_delay", 0) or 0)
            ),
            schedule_start=item.get("schedule_start"),
            schedule_end=item.get("schedule_end"),
            button_text=item.get("button_text"),
            button_url=item.get("button_url"),
            button_trigger_words=list(
                item.get("button_trigger_words") or []
            ),
            button_configs=list(item.get("button_configs") or []),
            forwarded_count=max(
                0, int(item.get("forwarded_count", 0) or 0)
            ),
            last_triggered=last_triggered,
        )

        session.add(rule)

    enabled = bool(
        data.get("global_forwarding_enabled", True)
    )

    meta = session.get(MetaConfig, 1)

    if not meta:
        meta = MetaConfig(
            id=1,
            admin_user_id=FORCE_ADMIN_ID,
            forwarding_enabled=enabled,
        )
        session.add(meta)
    else:
        meta.forwarding_enabled = enabled

    session.commit()


# ------------------ Start Command ------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not admin_check(user.id):
        await update.message.reply_text(
            "Keval admin is bot ko use kar sakta hai."
        )
        return

    session = Session()

    try:
        enabled = get_global_forwarding_enabled(session)
    finally:
        session.close()

    await update.message.reply_text(
        "Namaste! Advanced Multi-Rule Forward Bot ready.\n\n"
        "Buttons se apne forwarding rules manage karein.\n"
        "Keyword Button feature bhi available hai.\n"
        "Media albums, backup/restore aur global ON/OFF enabled hain.\n\n"
        "Menu ke liye neeche buttons use karein.",
        reply_markup=main_menu_keyboard(enabled),
    )


# ------------------ Callback Handler ------------------

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    user = update.effective_user

    if not user or not admin_check(user.id):
        await query.answer(
            "Sirf admin is bot ko use kar sakta hai.",
            show_alert=True,
        )
        return

    data = query.data or ""

    session = Session()

    try:

        # Main menu
        if data in ("main", "refresh"):

            enabled = get_global_forwarding_enabled(session)

            await query.edit_message_text(
                "Main Menu",
                reply_markup=main_menu_keyboard(enabled),
            )
            return

        # Global forwarding ON/OFF
        if data == "toggle_global":

            enabled = get_global_forwarding_enabled(session)

            new_state = not enabled

            set_global_forwarding(session, new_state)

            await query.edit_message_text(
                f"Global forwarding: "
                f"{'ON 🟢' if new_state else 'OFF 🔴'}",
                reply_markup=main_menu_keyboard(new_state),
            )
            return

        # Backup menu
        if data == "backup_menu":

            await query.edit_message_text(
                "💾 Backup / Restore\n\n"
                "Create Backup: sabhi rules ka backup banayein.\n"
                "Restore Backup: JSON backup upload karein.",
                reply_markup=backup_menu_keyboard(),
            )
            return

        if data == "create_backup":

            backup = build_backup(session)

            fd, path = tempfile.mkstemp(
                prefix="telegram_bot_backup_",
                suffix=".json",
            )
            os.close(fd)

            try:

                with open(path, "w", encoding="utf-8") as f:
                    json.dump(
                        backup,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )

                await query.message.reply_document(
                    document=FSInputFile(
                        path,
                        filename="bot_backup.json",
                    ),
                    caption=(
                        f"Backup created.\n"
                        f"Rules: {len(backup['rules'])}"
                    ),
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
                "Restore mode ON.\n\n"
                "Ab bot_backup.json document bhejein.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "❌ Cancel",
                            callback_data="cancel_restore",
                        )
                    ]
                ]),
            )
            return

        if data == "cancel_restore":

            context.user_data.pop("awaiting_restore", None)

            enabled = get_global_forwarding_enabled(session)

            await query.edit_message_text(
                "Restore cancelled.",
                reply_markup=main_menu_keyboard(enabled),
            )
            return

        # New forwarding rule
        if data == "new_rule":

            context.user_data["creating_rule"] = {}

            await query.edit_message_text(
                "Source Channel ID bhejein.\n"
                "Example: -1001234567890 ya @channel",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # List rules
        if data == "list_rules":

            rules = session.query(ForwardRule).order_by(
                ForwardRule.id
            ).all()

            if not rules:

                await query.edit_message_text(
                    "Koi rule nahi mila.",
                    reply_markup=main_menu_keyboard(
                        get_global_forwarding_enabled(session)
                    ),
                )
                return

            buttons = [
                [
                    InlineKeyboardButton(
                        f"#{r.id} {r.name}",
                        callback_data=f"rule_open|{r.id}",
                    )
                ]
                for r in rules
            ]

            buttons.append([
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="main",
                )
            ])

            await query.edit_message_text(
                "Forwarding Rules:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Open rule
        if data.startswith("rule_open|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return

            await query.edit_message_text(
                format_rule_summary(rule),
                reply_markup=rule_action_keyboard(rule),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Enable / disable rule
        if data.startswith("toggle_active|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:

                rule.is_active = not bool(rule.is_active)
                session.commit()

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_action_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Delete rule
        if data.startswith("delete_rule|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:
                session.delete(rule)
                session.commit()

            await query.edit_message_text(
                f"Rule #{rid} deleted.",
                reply_markup=main_menu_keyboard(
                    get_global_forwarding_enabled(session)
                ),
            )
            return

        # Edit rule name
        if data.startswith("edit_name|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["edit_name_rule"] = rid

            await query.edit_message_text(
                "Rule ka naya naam bhejein:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # Open settings
        if data.startswith("settings|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Rule statistics
        if data.startswith("stats|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:

                await query.edit_message_text(
                    f"Rule #{rid} Stats\n\n"
                    f"Forwarded: {rule.forwarded_count or 0}\n"
                    f"Last Triggered: {rule.last_triggered or 'Never'}",
                    reply_markup=rule_action_keyboard(rule),
                )

            return

        # Export individual rule
        if data.startswith("export_rule|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:

                await query.message.reply_text(
                    json.dumps(
                        rule_to_dict(rule),
                        ensure_ascii=False,
                        indent=2,
                    )
                )

            return

        # Toggle link blocking
        if data.startswith("toggle_links|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:
                rule.block_links = not bool(rule.block_links)
                session.commit()

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Toggle username blocking
        if data.startswith("toggle_usernames|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:
                rule.block_usernames = not bool(
                    rule.block_usernames
                )
                session.commit()

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # FORWARD / COPY mode
        if data.startswith("set_mode|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if rule:

                rule.forward_mode = (
                    "COPY"
                    if rule.forward_mode == "FORWARD"
                    else "FORWARD"
                )

                session.commit()

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Delay
        if data.startswith("set_delay|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["set_delay_rule"] = rid

            await query.edit_message_text(
                "Delay seconds bhejein: 0, 5, 15, 30 ya 60",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # Add text replacement
        if data.startswith("add_replace|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["add_replace_rule"] = rid
            context.user_data.pop("replace_find", None)

            await query.edit_message_text(
                "Woh text bhejein jise replace karna hai:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # View replacements
        if data.startswith("view_replace|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if not rule:
                return

            replacements = rule.text_replacements or {}

            if not replacements:

                await query.edit_message_text(
                    "Koi replacement set nahi hai.",
                    reply_markup=rule_settings_keyboard(rule),
                )
                return

            buttons = []

            for idx, (find_text, repl_text) in enumerate(
                replacements.items()
            ):

                preview = (
                    f"{str(find_text)[:25]} → "
                    f"{str(repl_text)[:25]}"
                )

                buttons.append([
                    InlineKeyboardButton(
                        preview,
                        callback_data="noop",
                    )
                ])

                buttons.append([
                    InlineKeyboardButton(
                        "❌ Delete",
                        callback_data=f"del_replace|{rid}|{idx}",
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"settings|{rid}",
                )
            ])

            await query.edit_message_text(
                "Text Replacements:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Delete replacement
        if data.startswith("del_replace|"):

            _, rid, idx_text = data.split("|", 2)

            rule = session.get(ForwardRule, int(rid))

            if rule:

                replacements = dict(rule.text_replacements or {})

                try:
                    idx = int(idx_text)
                    keys = list(replacements.keys())

                    if 0 <= idx < len(keys):
                        replacements.pop(keys[idx], None)
                        rule.text_replacements = replacements
                        session.commit()

                except (ValueError, TypeError):
                    pass

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Add blacklist word
        if data.startswith("add_blacklist|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["add_blacklist_rule"] = rid

            await query.edit_message_text(
                "Blacklist mein add karne ke liye word bhejein:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # View blacklist
        if data.startswith("view_blacklist|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if not rule:
                return

            words = rule.blacklist_words or []

            if not words:

                await query.edit_message_text(
                    "Blacklist empty hai.",
                    reply_markup=rule_settings_keyboard(rule),
                )
                return

            buttons = []

            for word in words:

                encoded = urllib.parse.quote_plus(word)

                buttons.append([
                    InlineKeyboardButton(
                        word[:50],
                        callback_data="noop",
                    )
                ])

                buttons.append([
                    InlineKeyboardButton(
                        "❌ Remove",
                        callback_data=f"del_black|{rid}|{encoded}",
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"settings|{rid}",
                )
            ])

            await query.edit_message_text(
                "Blacklist:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Delete blacklist word
        if data.startswith("del_black|"):

            _, rid, encoded = data.split("|", 2)

            word = urllib.parse.unquote_plus(encoded)

            rule = session.get(ForwardRule, int(rid))

            if rule:

                words = list(rule.blacklist_words or [])

                if word in words:
                    words.remove(word)
                    rule.blacklist_words = words
                    session.commit()

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Add regular whitelist word
        if data.startswith("add_whitelist|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["add_whitelist_rule"] = rid

            await query.edit_message_text(
                "Whitelist mein add karne ke liye word bhejein:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # View regular whitelist
        if data.startswith("view_whitelist|"):

            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if not rule:
                return

            words = rule.whitelist_words or []

            if not words:

                await query.edit_message_text(
                    "Whitelist empty hai.",
                    reply_markup=rule_settings_keyboard(rule),
                )
                return

            buttons = []

            for word in words:

                encoded = urllib.parse.quote_plus(word)

                buttons.append([
                    InlineKeyboardButton(
                        word[:50],
                        callback_data="noop",
                    )
                ])

                buttons.append([
                    InlineKeyboardButton(
                        "❌ Remove",
                        callback_data=f"del_white|{rid}|{encoded}",
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"settings|{rid}",
                )
            ])

            await query.edit_message_text(
                "Whitelist:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Delete regular whitelist word
        if data.startswith("del_white|"):

            _, rid, encoded = data.split("|", 2)

            word = urllib.parse.unquote_plus(encoded)

            rule = session.get(ForwardRule, int(rid))

            if rule:

                words = list(rule.whitelist_words or [])

                if word in words:
                    words.remove(word)
                    rule.whitelist_words = words
                    session.commit()

                await query.edit_message_text(
                    format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )

            return

        # Edit header
        if data.startswith("edit_header|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["edit_header_rule"] = rid

            await query.edit_message_text(
                "Naya header text bhejein:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # Edit footer
        if data.startswith("edit_footer|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["edit_footer_rule"] = rid

            await query.edit_message_text(
                "Naya footer text bhejein:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # Schedule
        if data.startswith("set_schedule|"):

            rid = int(data.split("|", 1)[1])

            context.user_data["set_schedule_rule"] = rid

            await query.edit_message_text(
                "Schedule bhejein:\n"
                "09:00 21:30\n\n"
                "Schedule hatane ke liye any bhejein.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data="main",
                        )
                    ]
                ]),
            )
            return

        # ---------------- Multiple Keyword Buttons ----------------

        if data.startswith("button_manage|"):
            rid = int(data.split("|", 1)[1])
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            configs = get_button_configs(rule)
            if not rule.button_configs and configs:
                save_button_configs(rule, configs)
                session.commit()
            await query.edit_message_text(
                f"Button Manager — Rule #{rid}\\nTotal: {len(configs)} | Enabled: {sum(1 for b in configs if b.get('enabled', True))}",
                reply_markup=button_management_keyboard(rule),
            )
            return

        if data.startswith("btn_add|"):
            rid = int(data.split("|", 1)[1])
            if not session.get(ForwardRule, rid):
                await query.edit_message_text("Rule nahi mila.")
                return
            context.user_data["multi_button_setup"] = {"rule_id": rid, "step": "text"}
            await query.edit_message_text(
                "Naye button ka text bhejein (maximum 64 characters).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"button_manage|{rid}")]]),
            )
            return

        if data.startswith("btn_list|"):
            rid = int(data.split("|", 1)[1])
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            configs = get_button_configs(rule)
            if not configs:
                await query.edit_message_text(
                    "Abhi koi button configured nahi hai.",
                    reply_markup=button_management_keyboard(rule),
                )
                return
            await query.edit_message_text(
                "Kisi button ko manage karne ke liye select karein:",
                reply_markup=button_list_keyboard(rule),
            )
            return

        if data.startswith("btn_item|"):
            _, rid_text, bid_text = data.split("|", 2)
            rid, bid = int(rid_text), int(bid_text)
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            button = next((b for b in get_button_configs(rule) if b["id"] == bid), None)
            if not button:
                await query.edit_message_text("Button nahi mila.")
                return
            await query.edit_message_text(
                f"Button #{bid}: {button['text']}\\n"
                f"Position: {button['position']}\\n"
                f"Color: {BUTTON_COLORS.get(button['color'], 'Default')}\\n"
                f"Keywords: {', '.join(button['keywords']) or 'None'}\\n"
                f"Status: {'Enabled' if button.get('enabled', True) else 'Disabled'}",
                reply_markup=single_button_keyboard(rid, bid),
            )
            return

        if data.startswith("btn_disable_all|") or data.startswith("btn_enable_all|"):
            action, rid_text = data.split("|", 1)
            rid = int(rid_text)
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            configs = get_button_configs(rule)
            state = action == "btn_enable_all"
            for button in configs:
                button["enabled"] = state
            save_button_configs(rule, configs)
            session.commit()
            await query.edit_message_text(
                "Sabhi buttons " + ("enable" if state else "disable") + " kar diye gaye.",
                reply_markup=button_management_keyboard(rule),
            )
            return

        if data.startswith("btn_toggle|"):
            _, rid_text, bid_text = data.split("|", 2)
            rid, bid = int(rid_text), int(bid_text)
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            configs = get_button_configs(rule)
            button = next((b for b in configs if b["id"] == bid), None)
            if button:
                button["enabled"] = not button.get("enabled", True)
                save_button_configs(rule, configs)
                session.commit()
            await query.edit_message_text(
                "Button status updated.",
                reply_markup=single_button_keyboard(rid, bid),
            )
            return

        if data.startswith("btn_delete|"):
            _, rid_text, bid_text = data.split("|", 2)
            rid, bid = int(rid_text), int(bid_text)
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            configs = [b for b in get_button_configs(rule) if b["id"] != bid]
            save_button_configs(rule, configs)
            session.commit()
            await query.edit_message_text(
                f"Button #{bid} delete kar diya gaya.",
                reply_markup=button_management_keyboard(rule),
            )
            return

        if data.startswith("btn_position|"):
            _, rid_text, bid_text = data.split("|", 2)
            rid, bid = int(rid_text), int(bid_text)
            context.user_data["button_position_edit"] = {"rule_id": rid, "button_id": bid}
            await query.edit_message_text(
                "Is button ki nayi position number bhejein (1 se shuru).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"btn_item|{rid}|{bid}")]]),
            )
            return

        if data.startswith("btn_color|"):
            _, rid_text, bid_text = data.split("|", 2)
            rid, bid = int(rid_text), int(bid_text)
            context.user_data["button_color_edit"] = {"rule_id": rid, "button_id": bid}
            await query.edit_message_text(
                "Button color select karein:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Default", callback_data=f"btn_setcolor|{rid}|{bid}|default")],
                    [InlineKeyboardButton("🔵 Blue", callback_data=f"btn_setcolor|{rid}|{bid}|primary")],
                    [InlineKeyboardButton("🟢 Green", callback_data=f"btn_setcolor|{rid}|{bid}|success")],
                    [InlineKeyboardButton("🔴 Red", callback_data=f"btn_setcolor|{rid}|{bid}|danger")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"btn_item|{rid}|{bid}")],
                ]),
            )
            return

        if data.startswith("btn_setcolor|"):
            _, rid_text, bid_text, color = data.split("|", 3)
            rid, bid = int(rid_text), int(bid_text)
            rule = session.get(ForwardRule, rid)
            if not rule or color not in BUTTON_COLORS:
                await query.edit_message_text("Invalid rule/color.")
                return
            configs = get_button_configs(rule)
            button = next((b for b in configs if b["id"] == bid), None)
            if button:
                button["color"] = color
                save_button_configs(rule, configs)
                session.commit()
            await query.edit_message_text(
                f"Button color set: {BUTTON_COLORS[color]}",
                reply_markup=single_button_keyboard(rid, bid),
            )
            return

        # Legacy callback now opens the multi-button manager.
        if data.startswith("setup_button|"):
            rid = int(data.split("|", 1)[1])
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            await query.edit_message_text(
                "Multiple Button Manager",
                reply_markup=button_management_keyboard(rule),
            )
            return


            rid = int(data.split("|", 1)[1])

            rule = session.get(ForwardRule, rid)

            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return

            context.user_data["setup_button_rule"] = rid
            context.user_data["setup_button_step"] = "text"

            await query.edit_message_text(
                "🔘 Button Setup\n\n"
                "Step 1/3:\n"
                "Button par jo text dikhana hai, woh bhejein.\n\n"
                "Example: JOIN NOW",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "❌ Cancel",
                            callback_data=f"settings|{rid}",
                        )
                    ]
                ]),
            )
            return

        # Legacy disable button callback: disable every configured button.
        if data.startswith("disable_button|"):
            rid = int(data.split("|", 1)[1])
            rule = session.get(ForwardRule, rid)
            if not rule:
                await query.edit_message_text("Rule nahi mila.")
                return
            configs = get_button_configs(rule)
            for button in configs:
                button["enabled"] = False
            save_button_configs(rule, configs)
            session.commit()
            await query.edit_message_text(
                "Sabhi buttons disable kar diye gaye.",
                reply_markup=button_management_keyboard(rule),
            )
            return

        if data.startswith("btn_newcolor|"):
            _, rid_text, color = data.split("|", 2)
            rid = int(rid_text)
            state = context.user_data.get("multi_button_setup")
            rule = session.get(ForwardRule, rid)
            if not rule or not state or int(state.get("rule_id", -1)) != rid or color not in BUTTON_COLORS:
                await query.edit_message_text("Button setup expire ho gaya. Dobara Add Button karein.")
                return
            configs = get_button_configs(rule)
            new_button = {
                "id": next_button_id(configs),
                "text": state["text"],
                "url": state["url"],
                "keywords": state["keywords"],
                "color": color,
                "position": int(state["position"]),
                "enabled": True,
            }
            # Insert at the requested one-based position.
            insert_at = max(0, min(len(configs), int(state["position"]) - 1))
            configs.sort(key=lambda b: (int(b.get("position", 1)), int(b.get("id", 1))))
            configs.insert(insert_at, new_button)
            for pos, item in enumerate(configs, start=1):
                item["position"] = pos
            save_button_configs(rule, configs)
            session.commit()
            context.user_data.pop("multi_button_setup", None)
            await query.edit_message_text(
                f"Button add ho gaya: {new_button['text']}\\n"
                f"Position: {new_button['position']} | Color: {BUTTON_COLORS[color]}",
                reply_markup=button_management_keyboard(rule),
            )
            return

        # Global information
        if data == "global_info":

            enabled = get_global_forwarding_enabled(session)

            count = session.query(ForwardRule).count()

            active = session.query(ForwardRule).filter(
                ForwardRule.is_active == True
            ).count()

            await query.edit_message_text(
                f"Admin ID: {FORCE_ADMIN_ID}\n"
                f"Timezone: Asia/Kolkata\n"
                f"Global forwarding: {'ON' if enabled else 'OFF'}\n"
                f"Total rules: {count}\n"
                f"Active rules: {active}",
                reply_markup=main_menu_keyboard(enabled),
            )
            return

        if data == "noop":
            return

    except Exception:

        logger.exception("Callback handler error")

        try:
            await query.message.reply_text(
                "Operation failed. Render logs check karein."
            )
        except Exception:
            pass

    finally:
        session.close()


# भाग 2 यहाँ समाप्त होता है।
# ------------------ Text Flow Handler ------------------

async def text_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not admin_check(user.id):
        return

    text_value = (update.message.text or "").strip()

    if not text_value:
        return

    session = Session()

    try:
        # Create new forwarding rule
        if "creating_rule" in context.user_data:
            state = context.user_data["creating_rule"]

            if "source" not in state:
                if not (
                    text_value.startswith("-100")
                    or text_value.startswith("@")
                    or text_value.lstrip("-").isdigit()
                ):
                    await update.message.reply_text(
                        "Invalid source ID. Send -100... or @channel."
                    )
                    return

                state["source"] = text_value

                await update.message.reply_text(
                    "Ab destination channel ID bhejein."
                )
                return

            if "dest" not in state:
                if not (
                    text_value.startswith("-100")
                    or text_value.startswith("@")
                    or text_value.lstrip("-").isdigit()
                ):
                    await update.message.reply_text(
                        "Invalid destination ID."
                    )
                    return

                state["dest"] = text_value

                await update.message.reply_text(
                    "Ab is rule ka naam bhejein."
                )
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
                    button_trigger_words=[],
                    is_active=True,
                    forward_mode="FORWARD",
                    forward_delay=0,
                )

                session.add(rule)
                session.commit()

                context.user_data.pop("creating_rule", None)

                await update.message.reply_text(
                    "Rule created successfully!\n\n"
                    + format_rule_summary(rule),
                    reply_markup=main_menu_keyboard(
                        get_global_forwarding_enabled(session)
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

        # Set existing button position
        if "button_position_edit" in context.user_data:
            state = context.user_data.pop("button_position_edit")
            rid = int(state["rule_id"])
            bid = int(state["button_id"])
            rule = session.get(ForwardRule, rid)
            if not rule:
                await update.message.reply_text("Rule nahi mila.")
                return
            configs = get_button_configs(rule)
            button = next((b for b in configs if b["id"] == bid), None)
            if not button:
                await update.message.reply_text("Button nahi mila.")
                return
            try:
                position = int(text_value)
                if position < 1 or position > len(configs):
                    raise ValueError
            except ValueError:
                context.user_data["button_position_edit"] = state
                await update.message.reply_text(f"Position 1 se {len(configs)} ke beech honi chahiye.")
                return
            configs = [b for b in configs if b["id"] != bid]
            configs.insert(position - 1, button)
            for pos, item in enumerate(configs, start=1):
                item["position"] = pos
            save_button_configs(rule, configs)
            session.commit()
            await update.message.reply_text(
                f"Position update ho gayi: {position}",
                reply_markup=single_button_keyboard(rid, bid),
            )
            return

        # ---------------- Multi Button Setup Flow ----------------
        if "multi_button_setup" in context.user_data:
            state = context.user_data["multi_button_setup"]
            rid = int(state["rule_id"])
            rule = session.get(ForwardRule, rid)
            if not rule:
                context.user_data.pop("multi_button_setup", None)
                await update.message.reply_text("Rule nahi mila.")
                return

            step = state.get("step")
            if step == "text":
                if not text_value or len(text_value) > 64:
                    await update.message.reply_text("Button text 1-64 characters ka hona chahiye.")
                    return
                state["text"] = text_value
                state["step"] = "url"
                await update.message.reply_text("Ab button ka URL bhejein (https://...).")
                return

            if step == "url":
                from urllib.parse import urlparse
                parsed = urlparse(text_value)
                if parsed.scheme not in ("http", "https") or not parsed.netloc or any(ch.isspace() for ch in text_value):
                    await update.message.reply_text("Invalid URL. https://example.com jaisa link bhejein.")
                    return
                state["url"] = text_value
                state["step"] = "keywords"
                await update.message.reply_text("Ab is button ke trigger keywords comma se alag karke bhejein. Example: bonus, cricket, offer")
                return

            if step == "keywords":
                words = list(dict.fromkeys(
                    w.strip().lower() for w in text_value.split(",") if w.strip()
                ))
                if not words:
                    await update.message.reply_text("Kam se kam ek keyword bhejein.")
                    return
                if any(len(w) > 200 for w in words):
                    await update.message.reply_text("Har keyword maximum 200 characters ka ho.")
                    return
                state["keywords"] = words
                state["step"] = "position"
                configs = get_button_configs(rule)
                await update.message.reply_text(
                    f"Button ki position number bhejein (1-{len(configs)+1}). "
                    "1 ka matlab pehle button ki position hai."
                )
                return

            if step == "position":
                try:
                    position = int(text_value)
                    configs = get_button_configs(rule)
                    if position < 1 or position > len(configs) + 1:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text(
                        f"Valid position bhejein: 1 se {len(get_button_configs(rule))+1}."
                    )
                    return
                state["position"] = position
                state["step"] = "color"
                await update.message.reply_text(
                    "Color choose karein: default, blue, green ya red.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Default", callback_data=f"btn_newcolor|{rid}|default")],
                        [InlineKeyboardButton("🔵 Blue", callback_data=f"btn_newcolor|{rid}|primary")],
                        [InlineKeyboardButton("🟢 Green", callback_data=f"btn_newcolor|{rid}|success")],
                        [InlineKeyboardButton("🔴 Red", callback_data=f"btn_newcolor|{rid}|danger")],
                    ]),
                )
                return

        # ---------------- Legacy Button Setup Flow ----------------
        if "setup_button_rule" in context.user_data:

            rid = context.user_data["setup_button_rule"]

            step = context.user_data.get("setup_button_step")

            rule = session.get(ForwardRule, int(rid))

            if not rule:
                context.user_data.pop("setup_button_rule", None)
                context.user_data.pop("setup_button_step", None)

                await update.message.reply_text(
                    "Rule nahi mila. Setup cancel kar diya gaya."
                )
                return

            # Step 1: Button text
            if step == "text":

                if len(text_value) > 64:
                    await update.message.reply_text(
                        "Button text maximum 64 characters ka ho sakta hai."
                    )
                    return

                context.user_data["setup_button_text"] = text_value
                context.user_data["setup_button_step"] = "url"

                await update.message.reply_text(
                    "Step 2/3:\n\n"
                    "Button par kaunsa link lagana hai?\n"
                    "Example: https://example.com"
                )
                return

            # Step 2: Button URL
            if step == "url":

                from urllib.parse import urlparse

                parsed = urlparse(text_value)

                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.netloc
                    or any(ch.isspace() for ch in text_value)
                ):
                    await update.message.reply_text(
                        "Invalid URL.\n"
                        "https://example.com jaisa valid link bhejein."
                    )
                    return

                context.user_data["setup_button_url"] = text_value
                context.user_data["setup_button_step"] = "keywords"

                await update.message.reply_text(
                    "Step 3/3:\n\n"
                    "Ab woh whitelist keywords bhejein "
                    "jin par button lagna chahiye.\n\n"
                    "Multiple words comma se alag karein.\n"
                    "Example:\n"
                    "cricket, bonus, offer"
                )
                return

            # Step 3: Trigger keywords
            if step == "keywords":

                words = [
                    word.strip().lower()
                    for word in text_value.split(",")
                    if word.strip()
                ]

                if not words:
                    await update.message.reply_text(
                        "Kam se kam ek keyword bhejein."
                    )
                    return

                if any(len(word) > 200 for word in words):
                    await update.message.reply_text(
                        "Har keyword maximum 200 characters ka ho."
                    )
                    return

                rule.button_text = context.user_data[
                    "setup_button_text"
                ]

                rule.button_url = context.user_data[
                    "setup_button_url"
                ]

                rule.button_trigger_words = list(dict.fromkeys(words))

                session.commit()

                context.user_data.pop("setup_button_rule", None)
                context.user_data.pop("setup_button_step", None)
                context.user_data.pop("setup_button_text", None)
                context.user_data.pop("setup_button_url", None)

                await update.message.reply_text(
                    "Keyword Button successfully configured!\n\n"
                    + format_rule_summary(rule),
                    reply_markup=rule_settings_keyboard(rule),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

        # Edit rule name
        if "edit_name_rule" in context.user_data:

            rid = context.user_data.pop("edit_name_rule")

            rule = session.get(ForwardRule, int(rid))

            if rule:
                rule.name = text_value[:64]
                session.commit()

                await update.message.reply_text(
                    "Rule name updated.",
                    reply_markup=rule_action_keyboard(rule),
                )
            return

        # Delay
        if "set_delay_rule" in context.user_data:

            rid = context.user_data.pop("set_delay_rule")

            try:
                value = int(text_value)

                if value < 0:
                    raise ValueError

            except ValueError:
                context.user_data["set_delay_rule"] = rid

                await update.message.reply_text(
                    "Positive integer bhejein: 0, 5, 15, 30, 60."
                )
                return

            rule = session.get(ForwardRule, int(rid))

            if rule:
                rule.forward_delay = value
                session.commit()

                await update.message.reply_text(
                    "Delay updated.",
                    reply_markup=rule_settings_keyboard(rule),
                )
            return

        # Text replacement: first message
        if (
            "add_replace_rule" in context.user_data
            and "replace_find" not in context.user_data
        ):
            context.user_data["replace_find"] = text_value

            await update.message.reply_text(
                "Ab replacement text bhejein."
            )
            return

        # Text replacement: second message
        if (
            "add_replace_rule" in context.user_data
            and "replace_find" in context.user_data
        ):
            rid = context.user_data.pop("add_replace_rule")
            find_text = context.user_data.pop("replace_find")

            rule = session.get(ForwardRule, int(rid))

            if rule:
                replacements = dict(rule.text_replacements or {})
                replacements[find_text] = text_value

                rule.text_replacements = replacements
                session.commit()

                await update.message.reply_text(
                    "Replacement saved.",
                    reply_markup=rule_settings_keyboard(rule),
                )
            return

        # Blacklist
        if "add_blacklist_rule" in context.user_data:

            rid = context.user_data.pop("add_blacklist_rule")

            rule = session.get(ForwardRule, int(rid))

            if rule:
                words = list(rule.blacklist_words or [])
                word = text_value.lower()

                if word not in words:
                    words.append(word)

                rule.blacklist_words = words
                session.commit()

                await update.message.reply_text(
                    "Blacklist updated.",
                    reply_markup=rule_settings_keyboard(rule),
                )
            return

        # Existing whitelist
        if "add_whitelist_rule" in context.user_data:

            rid = context.user_data.pop("add_whitelist_rule")

            rule = session.get(ForwardRule, int(rid))

            if rule:
                words = list(rule.whitelist_words or [])
                word = text_value.lower()

                if word not in words:
                    words.append(word)

                rule.whitelist_words = words
                session.commit()

                await update.message.reply_text(
                    "Whitelist updated.",
                    reply_markup=rule_settings_keyboard(rule),
                )
            return

        # Header
        if "edit_header_rule" in context.user_data:

            rid = context.user_data.pop("edit_header_rule")

            rule = session.get(ForwardRule, int(rid))

            if rule:
                rule.header_text = text_value
                session.commit()

                await update.message.reply_text(
                    "Header updated.",
                    reply_markup=rule_settings_keyboard(rule),
                )
            return

        # Footer
        if "edit_footer_rule" in context.user_data:

            rid = context.user_data.pop("edit_footer_rule")

            rule = session.get(ForwardRule, int(rid))

            if rule:
                rule.footer_text = text_value
                session.commit()

                await update.message.reply_text(
                    "Footer updated.",
                    reply_markup=rule_settings_keyboard(rule),
                )
            return

        # Schedule
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

                await update.message.reply_text(
                    "Schedule cleared.",
                    reply_markup=rule_settings_keyboard(rule),
                )
                return

            parts = text_value.split()

            if len(parts) != 2:
                context.user_data["set_schedule_rule"] = rid

                await update.message.reply_text(
                    "Format: 09:00 21:30 ya any."
                )
                return

            start_time, end_time = parts

            try:
                datetime.strptime(start_time, "%H:%M")
                datetime.strptime(end_time, "%H:%M")

            except ValueError:
                context.user_data["set_schedule_rule"] = rid

                await update.message.reply_text(
                    "Time format HH:MM mein bhejein."
                )
                return

            rule.schedule_start = start_time
            rule.schedule_end = end_time

            session.commit()

            await update.message.reply_text(
                "Schedule saved.",
                reply_markup=rule_settings_keyboard(rule),
            )
            return

    except Exception:
        logger.exception("Text handler error")

        await update.message.reply_text(
            "Operation failed. Render logs check karein."
        )

    finally:
        session.close()


# ------------------ Restore Document Handler ------------------

async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user or not admin_check(user.id):
        return

    if not context.user_data.get("awaiting_restore"):
        return

    document = update.message.document

    if not document:
        return

    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await update.message.reply_text(
            "Backup maximum 5 MB ka hona chahiye."
        )
        return

    path = None

    try:
        tg_file = await context.bot.get_file(document.file_id)

        fd, path = tempfile.mkstemp(
            prefix="restore_",
            suffix=".json",
        )
        os.close(fd)

        await tg_file.download_to_drive(path)

        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        session = Session()

        try:
            restore_backup_data(session, data)

            enabled = get_global_forwarding_enabled(session)

            count = session.query(ForwardRule).count()

        finally:
            session.close()

        context.user_data.pop("awaiting_restore", None)

        await update.message.reply_text(
            f"Backup restored successfully.\n"
            f"Rules: {count}\n"
            f"Global forwarding: {'ON' if enabled else 'OFF'}",
            reply_markup=main_menu_keyboard(enabled),
        )

    except Exception as exc:
        logger.exception("Restore failed")

        await update.message.reply_text(
            f"Restore failed: {exc}"
        )

    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ------------------ Schedule Helper ------------------

def time_in_schedule(
    start: Optional[str],
    end: Optional[str],
) -> bool:

    if not start or not end:
        return True

    now = datetime.now(KOLKATA_TZ).time()

    start_time = datetime.strptime(
        start,
        "%H:%M",
    ).time()

    end_time = datetime.strptime(
        end,
        "%H:%M",
    ).time()

    if start_time <= end_time:
        return start_time <= now <= end_time

    return now >= start_time or now <= end_time


# ------------------ Message Processing ------------------

def source_matches(rule: ForwardRule, message) -> bool:

    try:
        chat_id = str(message.chat.id)
        source = (rule.source_chat_id or "").strip()

        if source.startswith("@"):
            username = getattr(message.chat, "username", "") or ""

            return bool(
                username
                and ("@" + username).lower() == source.lower()
            )

        if chat_id == source:
            return True

        if source.lstrip("-").isdigit() and chat_id == source:
            return True

        return False

    except Exception:
        return False


def prepare_text(rule: ForwardRule, message):

    original = message.text or message.caption or ""
    lower = original.lower()

    if rule.block_links and (
        "http" in lower or "t.me" in lower
    ):
        return None, False

    if rule.block_usernames and re.search(
        r"@[a-zA-Z0-9_]+",
        original,
    ):
        return None, False

    for word in rule.blacklist_words or []:
        if word and word.lower() in lower:
            return None, False

    # Existing whitelist filtering remains unchanged.
    if rule.whitelist_words:
        if not any(
            word and word.lower() in lower
            for word in rule.whitelist_words
        ):
            return None, False

    final_text = original
    modified = False

    for find_text, replace_text in (
        rule.text_replacements or {}
    ).items():

        if find_text and find_text in final_text:
            final_text = final_text.replace(
                find_text,
                replace_text,
            )
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
        getattr(message, "voice", None),
    ])


def matching_buttons(rule: ForwardRule, messages) -> List[Dict[str, Any]]:
    configs = [b for b in get_button_configs(rule) if b.get("enabled", True)]
    combined_text = " ".join(
        (getattr(message, "text", None) or getattr(message, "caption", None) or "")
        for message in messages
    ).lower()
    matched = []
    for button in configs:
        keywords = button.get("keywords") or []
        if keywords and any(word and word.lower() in combined_text for word in keywords):
            matched.append(button)
    return sorted(matched, key=lambda b: (int(b.get("position", 1)), int(b.get("id", 1))))


def build_button_markup(buttons: List[Dict[str, Any]]):
    if not buttons:
        return None
    rows = []
    # Position is global order; use two buttons per row.
    ordered = sorted(buttons, key=lambda b: (int(b.get("position", 1)), int(b.get("id", 1))))
    for index in range(0, len(ordered), 2):
        rows.append([make_url_button(button) for button in ordered[index:index + 2]])
    return InlineKeyboardMarkup(rows)


def get_keyword_button(rule: ForwardRule, message):
    buttons = matching_buttons(rule, [message])
    return build_button_markup(buttons)


async def send_one(
    rule: ForwardRule,
    message,
    final_text: str,
    text_modified: bool,
    context: ContextTypes.DEFAULT_TYPE,
):
    keyboard = get_keyword_button(rule, message)
    force_copy = text_modified or rule.forward_mode == "COPY" or keyboard is not None

    if rule.forward_mode == "FORWARD" and not force_copy:
        await context.bot.forward_message(
            chat_id=rule.destination_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        return

    if is_media_message(message):
        await context.bot.copy_message(
            chat_id=rule.destination_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            caption=final_text or "",
            reply_markup=keyboard,
        )
    elif final_text and final_text.strip():
        await context.bot.send_message(
            chat_id=rule.destination_chat_id,
            text=final_text,
            reply_markup=keyboard,
        )


async def asyncio_sleep(seconds: int):
    import asyncio
    await asyncio.sleep(seconds)


def media_to_input_media(message, caption=None):
    """Create InputMedia for preserving an album as a single Telegram media group."""
    if getattr(message, "photo", None):
        return InputMediaPhoto(
            media=message.photo[-1].file_id,
            caption=caption,
        )
    if getattr(message, "video", None):
        return InputMediaVideo(
            media=message.video.file_id,
            caption=caption,
        )
    if getattr(message, "document", None):
        return InputMediaDocument(
            media=message.document.file_id,
            caption=caption,
        )
    if getattr(message, "audio", None):
        return InputMediaAudio(
            media=message.audio.file_id,
            caption=caption,
        )
    return None


async def send_album(
    rule: ForwardRule,
    messages: List,
    context: ContextTypes.DEFAULT_TYPE,
):
    messages = sorted(messages, key=lambda message: message.message_id)
    if len(messages) < 2:
        if not messages:
            return 0
        final_text, modified = prepare_text(rule, messages[0])
        if final_text is None:
            return 0
        await send_one(rule, messages[0], final_text, modified, context)
        return 1

    # Evaluate filtering against the whole album, so its items stay together.
    combined = " ".join(
        (message.text or message.caption or "")
        for message in messages
    )
    proxy = type("AlbumTextProxy", (), {})()
    proxy.text = combined
    proxy.caption = None
    final_combined, modified = prepare_text(rule, proxy)
    if final_combined is None:
        return 0

    caption_index = next(
        (i for i, message in enumerate(messages) if message.caption),
        None,
    )
    caption_text = None
    if caption_index is not None:
        source_caption = messages[caption_index].caption or ""
        caption_text = source_caption
        for find_text, replace_text in (rule.text_replacements or {}).items():
            if find_text:
                caption_text = caption_text.replace(find_text, replace_text)
        if rule.header_text:
            caption_text = f"{rule.header_text}\\n\\n{caption_text}"
        if rule.footer_text:
            caption_text = f"{caption_text}\\n\\n{rule.footer_text}"

    media = []
    for index, message in enumerate(messages):
        item_caption = caption_text if index == caption_index else None
        item = media_to_input_media(message, caption=item_caption)
        if item is None:
            # Fallback to copy as a group if unsupported media type is present.
            copied = await context.bot.copy_messages(
                chat_id=rule.destination_chat_id,
                from_chat_id=messages[0].chat.id,
                message_ids=[m.message_id for m in messages],
            )
            buttons = matching_buttons(rule, messages)
            keyboard = build_button_markup(buttons)
            if keyboard:
                await context.bot.send_message(
                    chat_id=rule.destination_chat_id,
                    text="Links:",
                    reply_markup=keyboard,
                )
            return len(copied or messages)

        media.append(item)

    # Telegram does not support reply_markup on sendMediaGroup. Send the album
    # together first, then add a separate button row if any keyword matches.
    await context.bot.send_media_group(
        chat_id=rule.destination_chat_id,
        media=media,
    )

    buttons = matching_buttons(rule, messages)
    keyboard = build_button_markup(buttons)
    if keyboard:
        await context.bot.send_message(
            chat_id=rule.destination_chat_id,
            text="👇 Related links",
            reply_markup=keyboard,
        )

    return len(messages)


async def process_single_message(
    message,
    context: ContextTypes.DEFAULT_TYPE,
):

    session = Session()

    try:
        if not get_global_forwarding_enabled(session):
            return

        rules = session.query(ForwardRule).filter(
            ForwardRule.is_active == True
        ).all()

        for rule in rules:

            if not source_matches(rule, message):
                continue

            if not time_in_schedule(
                rule.schedule_start,
                rule.schedule_end,
            ):
                continue

            final_text, modified = prepare_text(rule, message)

            if final_text is None:
                continue

            try:
                if rule.forward_delay > 0:
                    await asyncio_sleep(rule.forward_delay)

                await send_one(
                    rule,
                    message,
                    final_text,
                    modified,
                    context,
                )

                rule.forwarded_count = (
                    rule.forwarded_count or 0
                ) + 1

                rule.last_triggered = datetime.utcnow()

                session.commit()

            except Exception as exc:
                session.rollback()

                logger.exception(
                    "Forward error for rule %s",
                    rule.id,
                )

                try:
                    await context.bot.send_message(
                        chat_id=FORCE_ADMIN_ID,
                        text=(
                            f"Forward error for rule #{rule.id}: "
                            f"{exc}"
                        ),
                    )
                except Exception:
                    pass

    finally:
        session.close()


async def process_album_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    key = context.job.data

    pending = context.application.bot_data.get(
        "pending_albums",
        {},
    )

    messages = pending.pop(key, [])

    if not messages:
        return

    session = Session()

    try:
        if not get_global_forwarding_enabled(session):
            return

        rules = session.query(ForwardRule).filter(
            ForwardRule.is_active == True
        ).all()

        for rule in rules:

            if not source_matches(rule, messages[0]):
                continue

            if not time_in_schedule(
                rule.schedule_start,
                rule.schedule_end,
            ):
                continue

            try:
                count = await send_album(
                    rule,
                    messages,
                    context,
                )

                if count:
                    rule.forwarded_count = (
                        rule.forwarded_count or 0
                    ) + count

                    rule.last_triggered = datetime.utcnow()

                    session.commit()

            except Exception:
                session.rollback()

                logger.exception(
                    "Album forwarding error for rule %s",
                    rule.id,
                )

    finally:
        session.close()


async def forward_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.channel_post or update.message

    if message is None:
        return

    if (
        update.message
        and update.effective_user
        and update.effective_user.id == FORCE_ADMIN_ID
    ):
        return

    media_group_id = getattr(
        message,
        "media_group_id",
        None,
    )

    if media_group_id:

        key = f"{message.chat.id}:{media_group_id}"

        pending = context.application.bot_data.setdefault(
            "pending_albums",
            {},
        )

        album = pending.setdefault(key, [])

        if not any(
            item.message_id == message.message_id
            for item in album
        ):
            album.append(message)

        jobs = context.application.job_queue.get_jobs_by_name(
            f"album:{key}"
        )

        for job in jobs:
            job.schedule_removal()

        context.application.job_queue.run_once(
            process_album_job,
            when=1.5,
            data=key,
            name=f"album:{key}",
        )

        return

    await process_single_message(message, context)


# ------------------ Application Setup ------------------

def main():

    application = Application.builder().token(
        BOT_TOKEN
    ).build()

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL,
            forward_message,
        )
    )

    port = int(os.environ.get("PORT", "8080"))

    webhook_url = os.environ.get("WEBHOOK_URL")

    if webhook_url:

        logger.info("Starting webhook mode")

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}",
        )

    else:

        logger.info("Starting polling mode")

        application.run_polling(
            allowed_updates=Update.ALL_TYPES
        )


if __name__ == "__main__":
    main()
