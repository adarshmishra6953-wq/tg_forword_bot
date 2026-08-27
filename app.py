#!/usr/bin/env python3
"""
ADVANCED TELEGRAM AUTO-FORWARD BOT
Render Ready
Bot Token Only

Features:
- Multiple Source -> Destination Rules
- Button Based Admin Panel
- Global ON/OFF
- Rule ON/OFF
- Edit Source / Destination / Name
- FORWARD / COPY mode
- Delay
- Retry on forwarding errors
- Duplicate Message Protection
- Link Blocking
- Username Blocking
- Blacklist / Whitelist
- Text Replacement
- Header / Footer
- Schedule
- Media Type Filters
- Album Support
- Test Rule
- Clone Rule
- Detailed Statistics
- Activity Log
- Full Backup / Restore
- Rule Export
- Delete Confirmation
- PostgreSQL / SQLite
- Render Ready
- Asia/Kolkata timezone

IMPORTANT:
The bot must be ADMIN in the source channel/group
and destination channel/group where required.
"""

import os
import re
import json
import time
import logging
import tempfile
import urllib.parse
import asyncio

from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

try:
    from telegram import (
        Update,
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        InputFile,
    )
except ImportError:
    from telegram import (
        Update,
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        FSInputFile,
    )
    InputFile = FSInputFile

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

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
    UniqueConstraint,
)

from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.mutable import MutableDict, MutableList


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("AdvancedForwardBot")


# ============================================================
# CONFIG
# ============================================================

FORCE_ADMIN_ID = int(os.environ.get("ADMIN_ID", "1695450646"))

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is missing.")
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

TIMEZONE = os.environ.get(
    "BOT_TIMEZONE",
    "Asia/Kolkata",
)

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Kolkata")
    TIMEZONE = "Asia/Kolkata"


# ============================================================
# DATABASE
# ============================================================

Engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False
    } if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

Base = declarative_base()

Session = sessionmaker(bind=Engine)


# ============================================================
# DATABASE MODELS
# ============================================================

class ForwardRule(Base):
    __tablename__ = "forward_rules"

    id = Column(Integer, primary_key=True)

    name = Column(
        String,
        default="unnamed_rule",
    )

    source_chat_id = Column(
        String,
        nullable=False,
    )

    destination_chat_id = Column(
        String,
        nullable=False,
    )

    is_active = Column(
        Boolean,
        default=True,
    )

    # Filters
    block_links = Column(
        Boolean,
        default=False,
    )

    block_usernames = Column(
        Boolean,
        default=False,
    )

    blacklist_words = Column(
        MutableList.as_mutable(PickleType),
        default=list,
    )

    whitelist_words = Column(
        MutableList.as_mutable(PickleType),
        default=list,
    )

    text_replacements = Column(
        MutableDict.as_mutable(PickleType),
        default=dict,
    )

    # Formatting
    header_text = Column(
        String,
        nullable=True,
    )

    footer_text = Column(
        String,
        nullable=True,
    )

    # Forward mode
    forward_mode = Column(
        String,
        default="FORWARD",
    )

    forward_delay = Column(
        Integer,
        default=0,
    )

    # Retry
    retry_enabled = Column(
        Boolean,
        default=True,
    )

    max_retries = Column(
        Integer,
        default=3,
    )

    # Schedule
    schedule_start = Column(
        String,
        nullable=True,
    )

    schedule_end = Column(
        String,
        nullable=True,
    )

    # Media filters
    allow_text = Column(
        Boolean,
        default=True,
    )

    allow_photo = Column(
        Boolean,
        default=True,
    )

    allow_video = Column(
        Boolean,
        default=True,
    )

    allow_document = Column(
        Boolean,
        default=True,
    )

    allow_audio = Column(
        Boolean,
        default=True,
    )

    allow_animation = Column(
        Boolean,
        default=True,
    )

    allow_sticker = Column(
        Boolean,
        default=True,
    )

    allow_other = Column(
        Boolean,
        default=True,
    )

    # Statistics
    forwarded_count = Column(
        Integer,
        default=0,
    )

    blocked_count = Column(
        Integer,
        default=0,
    )

    failed_count = Column(
        Integer,
        default=0,
    )

    last_triggered = Column(
        DateTime,
        nullable=True,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


class MetaConfig(Base):
    __tablename__ = "meta_config"

    id = Column(
        Integer,
        primary_key=True,
    )

    admin_user_id = Column(
        Integer,
        default=FORCE_ADMIN_ID,
    )

    forwarding_enabled = Column(
        Boolean,
        default=True,
    )


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(
        Integer,
        primary_key=True,
    )

    rule_id = Column(
        Integer,
        nullable=False,
    )

    source_chat_id = Column(
        String,
        nullable=False,
    )

    message_id = Column(
        Integer,
        nullable=False,
    )

    status = Column(
        String,
        default="success",
    )

    error_text = Column(
        String,
        nullable=True,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "source_chat_id",
            "message_id",
            name="uq_rule_message",
        ),
    )


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(
        Integer,
        primary_key=True,
    )

    rule_id = Column(
        Integer,
        nullable=True,
    )

    action = Column(
        String,
        nullable=False,
    )

    details = Column(
        String,
        nullable=True,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# ============================================================
# DATABASE SETUP
# ============================================================

def ensure_database():

    Base.metadata.create_all(Engine)

    inspector = inspect(Engine)

    dialect = Engine.dialect.name

    # Migration for old installations
    if inspector.has_table("forward_rules"):

        existing = {
            c["name"]
            for c in inspector.get_columns(
                "forward_rules"
            )
        }

        expected = {

            "retry_enabled": (
                "BOOLEAN",
                "INTEGER",
            ),

            "max_retries": (
                "INTEGER",
                "INTEGER",
            ),

            "allow_text": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_photo": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_video": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_document": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_audio": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_animation": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_sticker": (
                "BOOLEAN",
                "INTEGER",
            ),

            "allow_other": (
                "BOOLEAN",
                "INTEGER",
            ),

            "blocked_count": (
                "INTEGER",
                "INTEGER",
            ),

            "failed_count": (
                "INTEGER",
                "INTEGER",
            ),

            "created_at": (
                "TIMESTAMP",
                "DATETIME",
            ),
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
                        "Added column %s",
                        col,
                    )

    session = Session()

    try:

        meta = session.get(
            MetaConfig,
            1,
        )

        if not meta:

            meta = MetaConfig(
                id=1,
                admin_user_id=FORCE_ADMIN_ID,
                forwarding_enabled=True,
            )

            session.add(meta)
            session.commit()

    finally:
        session.close()


try:
    ensure_database()
except Exception:
    logger.exception(
        "Database initialization failed"
    )


# ============================================================
# HELPERS
# ============================================================

def admin_check(user_id: Optional[int]) -> bool:

    return user_id == FORCE_ADMIN_ID


def now_utc():

    return datetime.utcnow()


def get_global_forwarding_enabled(session):

    meta = session.get(
        MetaConfig,
        1,
    )

    if not meta:

        meta = MetaConfig(
            id=1,
            admin_user_id=FORCE_ADMIN_ID,
            forwarding_enabled=True,
        )

        session.add(meta)
        session.commit()

    return bool(
        meta.forwarding_enabled
    )


def set_global_forwarding(
    session,
    enabled: bool,
):

    meta = session.get(
        MetaConfig,
        1,
    )

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


def safe_join(items):

    try:
        return ", ".join(
            items or []
        )
    except Exception:
        return "None"


def short_text(value, limit=45):

    if not value:
        return "None"

    value = str(value)

    if len(value) > limit:
        return value[:limit] + "..."

    return value


def add_activity(
    session,
    action,
    details="",
    rule_id=None,
):

    try:

        log = ActivityLog(
            rule_id=rule_id,
            action=action,
            details=str(details)[:1000],
        )

        session.add(log)

    except Exception:
        pass


# ============================================================
# RULE SUMMARY
# ============================================================

def rule_summary(rule: ForwardRule):

    schedule = (
        f"{rule.schedule_start} - "
        f"{rule.schedule_end}"
        if rule.schedule_start
        and rule.schedule_end
        else "24 Hours"
    )

    return (
        f"📌 RULE #{rule.id}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 Name: {rule.name}\n\n"

        f"📥 Source:\n"
        f"`{rule.source_chat_id}`\n\n"

        f"📤 Destination:\n"
        f"`{rule.destination_chat_id}`\n\n"

        f"⚡ Status: "
        f"{'🟢 ON' if rule.is_active else '🔴 OFF'}\n"

        f"🔄 Mode: `{rule.forward_mode}`\n"
        f"⏱ Delay: `{rule.forward_delay}s`\n"

        f"🔁 Retry: "
        f"{'ON' if rule.retry_enabled else 'OFF'} "
        f"({rule.max_retries})\n\n"

        f"🔗 Links Block: "
        f"{'ON' if rule.block_links else 'OFF'}\n"

        f"👤 Username Block: "
        f"{'ON' if rule.block_usernames else 'OFF'}\n\n"

        f"🚫 Blacklist: "
        f"{len(rule.blacklist_words or [])}\n"

        f"✅ Whitelist: "
        f"{len(rule.whitelist_words or [])}\n"

        f"✏️ Replacements: "
        f"{len(rule.text_replacements or {})}\n\n"

        f"🕒 Schedule: `{schedule}`\n\n"

        f"📊 Forwarded: "
        f"{rule.forwarded_count or 0}\n"

        f"🚫 Blocked: "
        f"{rule.blocked_count or 0}\n"

        f"❌ Failed: "
        f"{rule.failed_count or 0}\n"
    )


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard(
    global_enabled=True,
):

    status = (
        "🟢 Global Forwarding: ON"
        if global_enabled
        else
        "🔴 Global Forwarding: OFF"
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
                "📜 Rules",
                callback_data="list_rules",
            ),
            InlineKeyboardButton(
                "📊 Dashboard",
                callback_data="dashboard",
            ),
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
                "📋 Activity Log",
                callback_data="activity",
            )
        ],

        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh",
            ),

            InlineKeyboardButton(
                "ℹ️ System Info",
                callback_data="global_info",
            ),
        ],
    ])


# ============================================================
# RULE MENU
# ============================================================

def rule_action_keyboard(rule):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "⏸ Disable"
                if rule.is_active
                else
                "▶️ Enable",
                callback_data=f"toggle_active|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "✏️ Name",
                callback_data=f"edit_name|{rule.id}",
            ),

            InlineKeyboardButton(
                "📥 Source",
                callback_data=f"edit_source|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "📤 Destination",
                callback_data=f"edit_dest|{rule.id}",
            ),

            InlineKeyboardButton(
                "🔧 Settings",
                callback_data=f"settings|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "🧪 Test",
                callback_data=f"test_rule|{rule.id}",
            ),

            InlineKeyboardButton(
                "📊 Stats",
                callback_data=f"stats|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "📋 Clone",
                callback_data=f"clone_rule|{rule.id}",
            ),

            InlineKeyboardButton(
                "📤 Export",
                callback_data=f"export_rule|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "🗑 Delete",
                callback_data=f"delete_confirm|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ Main Menu",
                callback_data="main",
            )
        ],
    ])


# ============================================================
# SETTINGS MENU
# ============================================================

def settings_keyboard(rule):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                f"🔗 Links: "
                f"{'✅' if rule.block_links else '❌'}",
                callback_data=f"toggle_links|{rule.id}",
            ),

            InlineKeyboardButton(
                f"👤 Usernames: "
                f"{'✅' if rule.block_usernames else '❌'}",
                callback_data=f"toggle_usernames|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                f"🔄 Mode: {rule.forward_mode}",
                callback_data=f"set_mode|{rule.id}",
            ),

            InlineKeyboardButton(
                f"⏱ Delay: {rule.forward_delay}s",
                callback_data=f"set_delay|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                f"🔁 Retry: "
                f"{'ON' if rule.retry_enabled else 'OFF'}",
                callback_data=f"toggle_retry|{rule.id}",
            ),

            InlineKeyboardButton(
                "🔢 Max Retry",
                callback_data=f"set_retries|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "📝 Text Filters",
                callback_data=f"text_filters|{rule.id}",
            ),

            InlineKeyboardButton(
                "🎞 Media Filters",
                callback_data=f"media_filters|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "➕ Replacement",
                callback_data=f"add_replace|{rule.id}",
            ),

            InlineKeyboardButton(
                "📄 Replacements",
                callback_data=f"view_replace|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "➕ Blacklist",
                callback_data=f"add_blacklist|{rule.id}",
            ),

            InlineKeyboardButton(
                "🚫 Blacklist",
                callback_data=f"view_blacklist|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "➕ Whitelist",
                callback_data=f"add_whitelist|{rule.id}",
            ),

            InlineKeyboardButton(
                "✅ Whitelist",
                callback_data=f"view_whitelist|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "🖊 Header",
                callback_data=f"edit_header|{rule.id}",
            ),

            InlineKeyboardButton(
                "🖊 Footer",
                callback_data=f"edit_footer|{rule.id}",
            ),
        ],

        [
            InlineKeyboardButton(
                "🕒 Schedule",
                callback_data=f"set_schedule|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"rule_open|{rule.id}",
            )
        ],
    ])


# ============================================================
# MEDIA FILTER MENU
# ============================================================

def media_keyboard(rule):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                f"📝 Text "
                f"{'✅' if rule.allow_text else '❌'}",
                callback_data=f"media_toggle|{rule.id}|text",
            ),

            InlineKeyboardButton(
                f"🖼 Photo "
                f"{'✅' if rule.allow_photo else '❌'}",
                callback_data=f"media_toggle|{rule.id}|photo",
            ),
        ],

        [
            InlineKeyboardButton(
                f"🎥 Video "
                f"{'✅' if rule.allow_video else '❌'}",
                callback_data=f"media_toggle|{rule.id}|video",
            ),

            InlineKeyboardButton(
                f"📄 Document "
                f"{'✅' if rule.allow_document else '❌'}",
                callback_data=f"media_toggle|{rule.id}|document",
            ),
        ],

        [
            InlineKeyboardButton(
                f"🎵 Audio "
                f"{'✅' if rule.allow_audio else '❌'}",
                callback_data=f"media_toggle|{rule.id}|audio",
            ),

            InlineKeyboardButton(
                f"🎞 GIF "
                f"{'✅' if rule.allow_animation else '❌'}",
                callback_data=f"media_toggle|{rule.id}|animation",
            ),
        ],

        [
            InlineKeyboardButton(
                f"😀 Sticker "
                f"{'✅' if rule.allow_sticker else '❌'}",
                callback_data=f"media_toggle|{rule.id}|sticker",
            ),

            InlineKeyboardButton(
                f"📦 Other "
                f"{'✅' if rule.allow_other else '❌'}",
                callback_data=f"media_toggle|{rule.id}|other",
            ),
        ],

        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"settings|{rule.id}",
            )
        ],
    ])


# ============================================================
# TEXT FILTER MENU
# ============================================================

def text_filter_keyboard(rule):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "➕ Blacklist Word",
                callback_data=f"add_blacklist|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "➕ Whitelist Word",
                callback_data=f"add_whitelist|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "📋 View Blacklist",
                callback_data=f"view_blacklist|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "📋 View Whitelist",
                callback_data=f"view_whitelist|{rule.id}",
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"settings|{rule.id}",
            )
        ],
    ])


# ============================================================
# BACKUP MENU
# ============================================================

def backup_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "💾 Create Backup",
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


# ============================================================
# RULE SERIALIZATION
# ============================================================

def rule_to_dict(rule):

    return {

        "id": rule.id,

        "name": rule.name,

        "source_chat_id":
            rule.source_chat_id,

        "destination_chat_id":
            rule.destination_chat_id,

        "is_active":
            bool(rule.is_active),

        "block_links":
            bool(rule.block_links),

        "block_usernames":
            bool(rule.block_usernames),

        "blacklist_words":
            list(rule.blacklist_words or []),

        "whitelist_words":
            list(rule.whitelist_words or []),

        "text_replacements":
            dict(rule.text_replacements or {}),

        "header_text":
            rule.header_text,

        "footer_text":
            rule.footer_text,

        "forward_mode":
            rule.forward_mode,

        "forward_delay":
            int(rule.forward_delay or 0),

        "retry_enabled":
            bool(rule.retry_enabled),

        "max_retries":
            int(rule.max_retries or 3),

        "schedule_start":
            rule.schedule_start,

        "schedule_end":
            rule.schedule_end,

        "allow_text":
            bool(rule.allow_text),

        "allow_photo":
            bool(rule.allow_photo),

        "allow_video":
            bool(rule.allow_video),

        "allow_document":
            bool(rule.allow_document),

        "allow_audio":
            bool(rule.allow_audio),

        "allow_animation":
            bool(rule.allow_animation),

        "allow_sticker":
            bool(rule.allow_sticker),

        "allow_other":
            bool(rule.allow_other),

        "forwarded_count":
            int(rule.forwarded_count or 0),

        "blocked_count":
            int(rule.blocked_count or 0),

        "failed_count":
            int(rule.failed_count or 0),

    }


def build_backup(session):

    rules = (
        session
        .query(ForwardRule)
        .order_by(ForwardRule.id)
        .all()
    )

    return {

        "backup_version": 3,

        "created_at":
            datetime.now(timezone.utc).isoformat(),

        "global_forwarding_enabled":
            get_global_forwarding_enabled(session),

        "rules":
            [
                rule_to_dict(rule)
                for rule in rules
            ],
    }


# ============================================================
# TIME SCHEDULE
# ============================================================

def time_in_schedule(
    start,
    end,
):

    if not start or not end:
        return True

    try:

        now = datetime.now(
            LOCAL_TZ
        ).time()

        start_time = datetime.strptime(
            start,
            "%H:%M",
        ).time()

        end_time = datetime.strptime(
            end,
            "%H:%M",
        ).time()

        if start_time <= end_time:

            return (
                start_time
                <= now
                <= end_time
            )

        return (
            now >= start_time
            or
            now <= end_time
        )

    except Exception:

        return True


# ============================================================
# MESSAGE TYPE
# ============================================================

def get_message_type(message):

    if getattr(message, "text", None):
        return "text"

    if getattr(message, "photo", None):
        return "photo"

    if getattr(message, "video", None):
        return "video"

    if getattr(message, "document", None):
        return "document"

    if getattr(message, "audio", None):
        return "audio"

    if getattr(message, "animation", None):
        return "animation"

    if getattr(message, "sticker", None):
        return "sticker"

    if getattr(message, "caption", None):

        if getattr(message, "photo", None):
            return "photo"

        if getattr(message, "video", None):
            return "video"

        if getattr(message, "document", None):
            return "document"

        return "other"

    return "other"


def media_allowed(
    rule,
    message,
):

    message_type = get_message_type(
        message
    )

    mapping = {

        "text":
            rule.allow_text,

        "photo":
            rule.allow_photo,

        "video":
            rule.allow_video,

        "document":
            rule.allow_document,

        "audio":
            rule.allow_audio,

        "animation":
            rule.allow_animation,

        "sticker":
            rule.allow_sticker,

        "other":
            rule.allow_other,
    }

    return bool(
        mapping.get(
            message_type,
            True,
        )
    )


# ============================================================
# SOURCE MATCH
# ============================================================

def source_matches(
    rule,
    message,
):

    try:

        message_chat_id = str(
            message.chat.id
        )

        source = (
            rule.source_chat_id
            or ""
        ).strip()

        if (
            source.startswith("-100")
            and message_chat_id == source
        ):
            return True

        if source.startswith("@"):

            username = (
                getattr(
                    message.chat,
                    "username",
                    "",
                )
                or ""
            )

            return bool(
                username
                and
                (
                    "@"
                    + username
                ).lower()
                ==
                source.lower()
            )

        if (
            source.lstrip("-").isdigit()
            and
            message_chat_id == source
        ):
            return True

        return False

    except Exception:

        return False


# ============================================================
# TEXT PROCESSING
# ============================================================

def prepare_text(
    rule,
    message,
):

    original = (
        message.text
        or
        message.caption
        or
        ""
    )

    lower = original.lower()

    # Link filter
    if rule.block_links:

        if (
            "http://" in lower
            or
            "https://" in lower
            or
            "t.me/" in lower
            or
            "telegram.me/" in lower
        ):

            return None, False, "link_blocked"

    # Username filter
    if rule.block_usernames:

        if re.search(
            r"@[a-zA-Z0-9_]+",
            original,
        ):

            return (
                None,
                False,
                "username_blocked",
            )

    # Blacklist
    for word in (
        rule.blacklist_words
        or []
    ):

        if (
            word
            and
            word.lower()
            in lower
        ):

            return (
                None,
                False,
                "blacklist",
            )

    # Whitelist
    if rule.whitelist_words:

        allowed = any(
            word
            and
            word.lower()
            in lower
            for word
            in rule.whitelist_words
        )

        if not allowed:

            return (
                None,
                False,
                "whitelist",
            )

    final_text = original

    modified = False

    # Replacements
    for (
        find,
        replacement
    ) in list(
        (
            rule.text_replacements
            or {}
        ).items()
    ):

        if (
            find
            and
            find in final_text
        ):

            final_text = (
                final_text.replace(
                    find,
                    replacement,
                )
            )

            modified = True

    # Header
    if rule.header_text:

        final_text = (
            rule.header_text
            + "\n\n"
            + final_text
        )

        modified = True

    # Footer
    if rule.footer_text:

        final_text = (
            final_text
            + "\n\n"
            + rule.footer_text
        )

        modified = True

    return (
        final_text,
        modified,
        None,
    )


# ============================================================
# MEDIA DETECTION
# ============================================================

def is_media_message(message):

    return any([

        getattr(
            message,
            "photo",
            None,
        ),

        getattr(
            message,
            "video",
            None,
        ),

        getattr(
            message,
            "document",
            None,
        ),

        getattr(
            message,
            "audio",
            None,
        ),

        getattr(
            message,
            "animation",
            None,
        ),

        getattr(
            message,
            "sticker",
            None,
        ),
    ])


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def already_forwarded(
    session,
    rule_id,
    source_chat_id,
    message_id,
):

    existing = (
        session
        .query(MessageLog)
        .filter(
            MessageLog.rule_id
            == rule_id,

            MessageLog.source_chat_id
            == str(source_chat_id),

            MessageLog.message_id
            == message_id,

            MessageLog.status
            == "success",
        )
        .first()
    )

    return existing is not None


def mark_forwarded(
    session,
    rule_id,
    source_chat_id,
    message_id,
):

    try:

        log = MessageLog(

            rule_id=rule_id,

            source_chat_id=
                str(source_chat_id),

            message_id=
                message_id,

            status="success",

        )

        session.add(log)

        session.commit()

        return True

    except Exception:

        session.rollback()

        return False


# ============================================================
# SEND MESSAGE
# ============================================================

async def send_one(
    rule,
    message,
    final_text,
    text_modified,
    context,
):

    force_copy = (
        text_modified
        or
        rule.forward_mode == "COPY"
    )

    # Normal Telegram forward
    if (
        rule.forward_mode == "FORWARD"
        and
        not force_copy
    ):

        await context.bot.forward_message(

            chat_id=
                rule.destination_chat_id,

            from_chat_id=
                message.chat.id,

            message_id=
                message.message_id,
        )

        return

    # Copy media
    if is_media_message(message):

        kwargs = {

            "chat_id":
                rule.destination_chat_id,

            "from_chat_id":
                message.chat.id,

            "message_id":
                message.message_id,
        }

        if final_text:

            kwargs["caption"] = (
                final_text[:1024]
            )

        await context.bot.copy_message(
            **kwargs
        )

        return

    # Copy text
    if final_text:

        await context.bot.send_message(

            chat_id=
                rule.destination_chat_id,

            text=
                final_text[:4096],
        )


# ============================================================
# RETRY SENDER
# ============================================================

async def send_with_retry(
    rule,
    message,
    final_text,
    modified,
    context,
):

    attempts = (
        max(
            1,
            int(
                rule.max_retries
                or
                3
            )
        )
        if rule.retry_enabled
        else
        1
    )

    last_error = None

    for attempt in range(
        1,
        attempts + 1,
    ):

        try:

            if (
                rule.forward_delay
                and
                rule.forward_delay > 0
            ):

                await asyncio.sleep(
                    rule.forward_delay
                )

            await send_one(

                rule,
                message,
                final_text,
                modified,
                context,
            )

            return True, None

        except Exception as error:

            last_error = error

            logger.warning(
                "Forward attempt %s/%s failed for rule %s: %s",
                attempt,
                attempts,
                rule.id,
                error,
            )

            if attempt < attempts:

                await asyncio.sleep(
                    min(
                        60,
                        5 * attempt,
                    )
                )

    return False, last_error


# ============================================================
# ALBUM SUPPORT
# ============================================================

async def send_album(
    rule,
    messages,
    context,
):

    messages = sorted(
        messages,
        key=lambda x: x.message_id,
    )

    prepared = []

    for message in messages:

        if not media_allowed(
            rule,
            message,
        ):
            continue

        final_text, modified, reason = (
            prepare_text(
                rule,
                message,
            )
        )

        if final_text is None:

            continue

        prepared.append(
            (
                message,
                final_text,
                modified,
            )
        )

    if not prepared:

        return 0

    # Preserve album only when no transformation
    can_batch = all(
        not modified
        for
        _,
        _,
        modified
        in prepared
    )

    ids = [
        message.message_id
        for
        message,
        _,
        _
        in prepared
    ]

    if can_batch:

        try:

            if (
                rule.forward_mode
                == "FORWARD"
                and
                hasattr(
                    context.bot,
                    "forward_messages",
                )
            ):

                await context.bot.forward_messages(

                    chat_id=
                        rule.destination_chat_id,

                    from_chat_id=
                        messages[0].chat.id,

                    message_ids=
                        ids,
                )

                return len(ids)

            if (
                rule.forward_mode
                == "COPY"
                and
                hasattr(
                    context.bot,
                    "copy_messages",
                )
            ):

                await context.bot.copy_messages(

                    chat_id=
                        rule.destination_chat_id,

                    from_chat_id=
                        messages[0].chat.id,

                    message_ids=
                        ids,
                )

                return len(ids)

        except Exception as error:

            logger.warning(
                "Album batch failed: %s",
                error,
            )

    sent = 0

    for (
        message,
        final_text,
        modified,
    ) in prepared:

        ok, error = await send_with_retry(

            rule,
            message,
            final_text,
            modified,
            context,
        )

        if ok:

            sent += 1

    return sent


# ============================================================
# PROCESS SINGLE MESSAGE
# ============================================================

async def process_single_message(
    message,
    context,
):

    session = Session()

    try:

        if not get_global_forwarding_enabled(
            session
        ):

            return

        rules = (
            session
            .query(ForwardRule)
            .filter(
                ForwardRule.is_active
                == True
            )
            .all()
        )

        for rule in rules:

            if not source_matches(
                rule,
                message,
            ):

                continue

            if not time_in_schedule(
                rule.schedule_start,
                rule.schedule_end,
            ):

                continue

            if not media_allowed(
                rule,
                message,
            ):

                rule.blocked_count = (
                    rule.blocked_count
                    or 0
                ) + 1

                add_activity(
                    session,
                    "MEDIA_BLOCKED",
                    get_message_type(
                        message
                    ),
                    rule.id,
                )

                session.commit()

                continue

            if already_forwarded(
                session,
                rule.id,
                message.chat.id,
                message.message_id,
            ):

                logger.info(
                    "Duplicate skipped: rule=%s message=%s",
                    rule.id,
                    message.message_id,
                )

                continue

            (
                final_text,
                modified,
                reason,
            ) = prepare_text(
                rule,
                message,
            )

            if final_text is None:

                rule.blocked_count = (
                    rule.blocked_count
                    or 0
                ) + 1

                add_activity(
                    session,
                    "MESSAGE_BLOCKED",
                    reason or "filter",
                    rule.id,
                )

                session.commit()

                continue

            success, error = (
                await send_with_retry(
                    rule,
                    message,
                    final_text,
                    modified,
                    context,
                )
            )

            if success:

                rule.forwarded_count = (
                    rule.forwarded_count
                    or 0
                ) + 1

                rule.last_triggered = (
                    now_utc()
                )

                mark_forwarded(
                    session,
                    rule.id,
                    message.chat.id,
                    message.message_id,
                )

                add_activity(
                    session,
                    "FORWARDED",
                    f"Message {message.message_id}",
                    rule.id,
                )

                session.commit()

            else:

                rule.failed_count = (
                    rule.failed_count
                    or 0
                ) + 1

                add_activity(
                    session,
                    "FORWARD_FAILED",
                    str(error),
                    rule.id,
                )

                session.commit()

                try:

                    await context.bot.send_message(

                        chat_id=
                            FORCE_ADMIN_ID,

                        text=(
                            "❌ Forward failed\n\n"
                            f"Rule: #{rule.id}\n"
                            f"Name: {rule.name}\n"
                            f"Error: {error}"
                        ),
                    )

                except Exception:
                    pass

    except Exception:

        session.rollback()

        logger.exception(
            "Message processing error"
        )

    finally:

        session.close()


# ============================================================
# ALBUM JOB
# ============================================================

async def process_album_job(
    context,
):

    key = context.job.data

    pending = context.application.bot_data.get(
        "pending_albums",
        {},
    )

    messages = pending.pop(
        key,
        [],
    )

    if not messages:
        return

    session = Session()

    try:

        if not get_global_forwarding_enabled(
            session
        ):
            return

        rules = (
            session
            .query(ForwardRule)
            .filter(
                ForwardRule.is_active
                == True
            )
            .all()
        )

        for rule in rules:

            if not source_matches(
                rule,
                messages[0],
            ):
                continue

            if not time_in_schedule(
                rule.schedule_start,
                rule.schedule_end,
            ):
                continue

            count = await send_album(
                rule,
                messages,
                context,
            )

            if count:

                rule.forwarded_count = (
                    rule.forwarded_count
                    or 0
                ) + count

                rule.last_triggered = (
                    now_utc()
                )

                add_activity(
                    session,
                    "ALBUM_FORWARDED",
                    f"{count} messages",
                    rule.id,
                )

                session.commit()

    except Exception:

        session.rollback()

        logger.exception(
            "Album processing error"
        )

    finally:

        session.close()


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not admin_check(
        user.id if user else None
    ):

        if update.message:

            await update.message.reply_text(
                "❌ Access denied."
            )

        return

    session = Session()

    try:

        enabled = (
            get_global_forwarding_enabled(
                session
            )
        )

    finally:

        session.close()

    await update.message.reply_text(

        "🚀 Advanced Telegram Forward Bot\n\n"
        "✅ Button based control\n"
        "✅ Multiple rules\n"
        "✅ Duplicate protection\n"
        "✅ Retry system\n"
        "✅ Media filters\n"
        "✅ Backup / Restore\n\n"
        "Use the buttons below.",

        reply_markup=
            main_keyboard(enabled),
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user = update.effective_user

    if not admin_check(
        user.id if user else None
    ):

        try:
            await query.edit_message_text(
                "❌ Access denied."
            )
        except Exception:
            pass

        return

    data = query.data or ""

    session = Session()

    try:

        # ----------------------------------------------------
        # MAIN
        # ----------------------------------------------------

        if data in (
            "main",
            "refresh",
        ):

            enabled = (
                get_global_forwarding_enabled(
                    session
                )
            )

            await query.edit_message_text(

                "🏠 Main Menu",

                reply_markup=
                    main_keyboard(enabled),
            )

            return

        # ----------------------------------------------------
        # GLOBAL TOGGLE
        # ----------------------------------------------------

        if data == "toggle_global":

            current = (
                get_global_forwarding_enabled(
                    session
                )
            )

            new_state = not current

            set_global_forwarding(
                session,
                new_state,
            )

            add_activity(
                session,
                "GLOBAL_FORWARDING",
                str(new_state),
            )

            session.commit()

            await query.edit_message_text(

                (
                    "🟢 Global Forwarding ON"
                    if new_state
                    else
                    "🔴 Global Forwarding OFF"
                ),

                reply_markup=
                    main_keyboard(new_state),
            )

            return

        # ----------------------------------------------------
        # NEW RULE
        # ----------------------------------------------------

        if data == "new_rule":

            context.user_data[
                "creating_rule"
            ] = {}

            await query.edit_message_text(

                "➕ NEW RULE\n\n"
                "Step 1/3\n"
                "Send SOURCE channel ID or @username\n\n"
                "Example:\n"
                "-1001234567890\n"
                "@mychannel",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data="main",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # LIST RULES
        # ----------------------------------------------------

        if data == "list_rules":

            rules = (
                session
                .query(ForwardRule)
                .order_by(
                    ForwardRule.id
                )
                .all()
            )

            if not rules:

                await query.edit_message_text(

                    "📜 No rules found.",

                    reply_markup=
                        main_keyboard(
                            get_global_forwarding_enabled(
                                session
                            )
                        ),
                )

                return

            buttons = []

            for rule in rules:

                status = (
                    "🟢"
                    if rule.is_active
                    else
                    "🔴"
                )

                buttons.append([

                    InlineKeyboardButton(

                        f"{status} #{rule.id} "
                        f"{rule.name}",

                        callback_data=
                            f"rule_open|{rule.id}",
                    )
                ])

            buttons.append([

                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="main",
                )
            ])

            await query.edit_message_text(

                "📜 YOUR RULES",

                reply_markup=
                    InlineKeyboardMarkup(
                        buttons
                    ),
            )

            return

        # ----------------------------------------------------
        # OPEN RULE
        # ----------------------------------------------------

        if data.startswith(
            "rule_open|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if not rule:
                await query.edit_message_text(
                    "❌ Rule not found."
                )
                return

            await query.edit_message_text(

                rule_summary(rule),

                reply_markup=
                    rule_action_keyboard(
                        rule
                    ),
            )

            return

        # ----------------------------------------------------
        # TOGGLE RULE
        # ----------------------------------------------------

        if data.startswith(
            "toggle_active|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.is_active = (
                    not bool(
                        rule.is_active
                    )
                )

                add_activity(
                    session,
                    "RULE_STATUS",
                    str(rule.is_active),
                    rule.id,
                )

                session.commit()

                await query.edit_message_text(

                    rule_summary(rule),

                    reply_markup=
                        rule_action_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # EDIT NAME
        # ----------------------------------------------------

        if data.startswith(
            "edit_name|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "edit_name_rule"
            ] = rid

            await query.edit_message_text(

                "✏️ Send the new rule name:",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"rule_open|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # EDIT SOURCE
        # ----------------------------------------------------

        if data.startswith(
            "edit_source|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "edit_source_rule"
            ] = rid

            await query.edit_message_text(

                "📥 Send new SOURCE ID/@username:",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"rule_open|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # EDIT DESTINATION
        # ----------------------------------------------------

        if data.startswith(
            "edit_dest|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "edit_dest_rule"
            ] = rid

            await query.edit_message_text(

                "📤 Send new DESTINATION ID/@username:",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"rule_open|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # SETTINGS
        # ----------------------------------------------------

        if data.startswith(
            "settings|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                await query.edit_message_text(

                    rule_summary(rule),

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # TEXT FILTERS
        # ----------------------------------------------------

        if data.startswith(
            "text_filters|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                await query.edit_message_text(

                    "📝 TEXT FILTERS\n\n"
                    "Use the buttons below.",

                    reply_markup=
                        text_filter_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # MEDIA FILTERS
        # ----------------------------------------------------

        if data.startswith(
            "media_filters|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                await query.edit_message_text(

                    "🎞 MEDIA FILTERS\n\n"
                    "Choose which message types "
                    "this rule should forward.",

                    reply_markup=
                        media_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # MEDIA TOGGLE
        # ----------------------------------------------------

        if data.startswith(
            "media_toggle|"
        ):

            parts = data.split("|")

            rid = int(parts[1])

            media_type = parts[2]

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                field_map = {

                    "text":
                        "allow_text",

                    "photo":
                        "allow_photo",

                    "video":
                        "allow_video",

                    "document":
                        "allow_document",

                    "audio":
                        "allow_audio",

                    "animation":
                        "allow_animation",

                    "sticker":
                        "allow_sticker",

                    "other":
                        "allow_other",
                }

                field = field_map.get(
                    media_type
                )

                if field:

                    setattr(
                        rule,
                        field,
                        not bool(
                            getattr(
                                rule,
                                field,
                            )
                        ),
                    )

                    session.commit()

                    await query.edit_message_text(

                        "🎞 MEDIA FILTERS",

                        reply_markup=
                            media_keyboard(
                                rule
                            ),
                    )

            return

        # ----------------------------------------------------
        # LINKS
        # ----------------------------------------------------

        if data.startswith(
            "toggle_links|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.block_links = (
                    not bool(
                        rule.block_links
                    )
                )

                session.commit()

                await query.edit_message_text(

                    rule_summary(rule),

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # USERNAMES
        # ----------------------------------------------------

        if data.startswith(
            "toggle_usernames|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.block_usernames = (
                    not bool(
                        rule.block_usernames
                    )
                )

                session.commit()

                await query.edit_message_text(

                    rule_summary(rule),

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # MODE
        # ----------------------------------------------------

        if data.startswith(
            "set_mode|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.forward_mode = (
                    "COPY"
                    if rule.forward_mode
                    == "FORWARD"
                    else
                    "FORWARD"
                )

                session.commit()

                await query.edit_message_text(

                    rule_summary(rule),

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # DELAY
        # ----------------------------------------------------

        if data.startswith(
            "set_delay|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "set_delay_rule"
            ] = rid

            await query.edit_message_text(

                "⏱ Send delay in seconds.\n\n"
                "Examples:\n"
                "0\n"
                "5\n"
                "10\n"
                "30\n"
                "60",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # RETRY TOGGLE
        # ----------------------------------------------------

        if data.startswith(
            "toggle_retry|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.retry_enabled = (
                    not bool(
                        rule.retry_enabled
                    )
                )

                session.commit()

                await query.edit_message_text(

                    rule_summary(rule),

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # SET MAX RETRIES
        # ----------------------------------------------------

        if data.startswith(
            "set_retries|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "set_retries_rule"
            ] = rid

            await query.edit_message_text(

                "🔁 Send maximum retry attempts.\n\n"
                "Example: 3",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        if data.startswith(
            "edit_header|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "edit_header_rule"
            ] = rid

            await query.edit_message_text(

                "🖊 Send HEADER text.\n\n"
                "Send `REMOVE` to clear it.",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # FOOTER
        # ----------------------------------------------------

        if data.startswith(
            "edit_footer|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "edit_footer_rule"
            ] = rid

            await query.edit_message_text(

                "🖊 Send FOOTER text.\n\n"
                "Send `REMOVE` to clear it.",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # SCHEDULE
        # ----------------------------------------------------

        if data.startswith(
            "set_schedule|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "set_schedule_rule"
            ] = rid

            await query.edit_message_text(

                "🕒 Send schedule:\n\n"
                "Example:\n"
                "`09:00 21:30`\n\n"
                "For 24 hours send:\n"
                "`ANY`",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # ADD REPLACEMENT
        # ----------------------------------------------------

        if data.startswith(
            "add_replace|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "add_replace_rule"
            ] = rid

            context.user_data.pop(
                "replace_find",
                None,
            )

            await query.edit_message_text(

                "✏️ Send FIND text:",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # VIEW REPLACEMENTS
        # ----------------------------------------------------

        if data.startswith(
            "view_replace|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if not rule:
                return

            replacements = (
                rule.text_replacements
                or {}
            )

            if not replacements:

                await query.edit_message_text(

                    "📄 No replacements.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

                return

            buttons = []

            for find, repl in (
                replacements.items()
            ):

                encoded = (
                    urllib.parse.quote_plus(
                        find
                    )
                )

                buttons.append([

                    InlineKeyboardButton(

                        f"{short_text(find,25)} → "
                        f"{short_text(repl,25)}",

                        callback_data="noop",
                    )
                ])

                buttons.append([

                    InlineKeyboardButton(

                        "❌ Delete",

                        callback_data=
                            f"del_replace|"
                            f"{rid}|{encoded}",
                    )
                ])

            buttons.append([

                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=
                        f"settings|{rid}",
                )
            ])

            await query.edit_message_text(

                "📄 TEXT REPLACEMENTS",

                reply_markup=
                    InlineKeyboardMarkup(
                        buttons
                    ),
            )

            return

        # ----------------------------------------------------
        # DELETE REPLACEMENT
        # ----------------------------------------------------

        if data.startswith(
            "del_replace|"
        ):

            _, rid, encoded = (
                data.split("|", 2)
            )

            rid = int(rid)

            find = (
                urllib.parse.unquote_plus(
                    encoded
                )
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                replacements = (
                    rule.text_replacements
                    or {}
                )

                replacements.pop(
                    find,
                    None,
                )

                rule.text_replacements = (
                    replacements
                )

                session.commit()

                await query.edit_message_text(

                    "✅ Replacement deleted.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # BLACKLIST ADD
        # ----------------------------------------------------

        if data.startswith(
            "add_blacklist|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "add_blacklist_rule"
            ] = rid

            await query.edit_message_text(

                "🚫 Send blacklist word:",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # BLACKLIST VIEW
        # ----------------------------------------------------

        if data.startswith(
            "view_blacklist|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if not rule:
                return

            words = (
                rule.blacklist_words
                or []
            )

            if not words:

                await query.edit_message_text(

                    "🚫 Blacklist is empty.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

                return

            buttons = []

            for word in words:

                encoded = (
                    urllib.parse.quote_plus(
                        word
                    )
                )

                buttons.append([

                    InlineKeyboardButton(
                        word[:50],
                        callback_data="noop",
                    )
                ])

                buttons.append([

                    InlineKeyboardButton(
                        "❌ Remove",
                        callback_data=
                            f"del_black|"
                            f"{rid}|{encoded}",
                    )
                ])

            buttons.append([

                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=
                        f"settings|{rid}",
                )
            ])

            await query.edit_message_text(

                "🚫 BLACKLIST",

                reply_markup=
                    InlineKeyboardMarkup(
                        buttons
                    ),
            )

            return

        # ----------------------------------------------------
        # DELETE BLACKLIST
        # ----------------------------------------------------

        if data.startswith(
            "del_black|"
        ):

            _, rid, encoded = (
                data.split("|", 2)
            )

            rid = int(rid)

            word = (
                urllib.parse.unquote_plus(
                    encoded
                )
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                words = (
                    rule.blacklist_words
                    or []
                )

                if word in words:

                    words.remove(word)

                rule.blacklist_words = words

                session.commit()

                await query.edit_message_text(

                    "✅ Blacklist updated.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # WHITELIST ADD
        # ----------------------------------------------------

        if data.startswith(
            "add_whitelist|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "add_whitelist_rule"
            ] = rid

            await query.edit_message_text(

                "✅ Send whitelist word:",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"settings|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # WHITELIST VIEW
        # ----------------------------------------------------

        if data.startswith(
            "view_whitelist|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if not rule:
                return

            words = (
                rule.whitelist_words
                or []
            )

            if not words:

                await query.edit_message_text(

                    "✅ Whitelist is empty.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

                return

            buttons = []

            for word in words:

                encoded = (
                    urllib.parse.quote_plus(
                        word
                    )
                )

                buttons.append([

                    InlineKeyboardButton(
                        word[:50],
                        callback_data="noop",
                    )
                ])

                buttons.append([

                    InlineKeyboardButton(
                        "❌ Remove",
                        callback_data=
                            f"del_white|"
                            f"{rid}|{encoded}",
                    )
                ])

            buttons.append([

                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=
                        f"settings|{rid}",
                )
            ])

            await query.edit_message_text(

                "✅ WHITELIST",

                reply_markup=
                    InlineKeyboardMarkup(
                        buttons
                    ),
            )

            return

        # ----------------------------------------------------
        # DELETE WHITELIST
        # ----------------------------------------------------

        if data.startswith(
            "del_white|"
        ):

            _, rid, encoded = (
                data.split("|", 2)
            )

            rid = int(rid)

            word = (
                urllib.parse.unquote_plus(
                    encoded
                )
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                words = (
                    rule.whitelist_words
                    or []
                )

                if word in words:

                    words.remove(word)

                rule.whitelist_words = words

                session.commit()

                await query.edit_message_text(

                    "✅ Whitelist updated.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # STATS
        # ----------------------------------------------------

        if data.startswith(
            "stats|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                total_logs = (
                    session
                    .query(MessageLog)
                    .filter(
                        MessageLog.rule_id
                        == rid
                    )
                    .count()
                )

                await query.edit_message_text(

                    f"📊 RULE #{rid} STATISTICS\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📝 {rule.name}\n\n"
                    f"✅ Forwarded: "
                    f"{rule.forwarded_count or 0}\n"
                    f"🚫 Blocked: "
                    f"{rule.blocked_count or 0}\n"
                    f"❌ Failed: "
                    f"{rule.failed_count or 0}\n"
                    f"🛡 Logged messages: "
                    f"{total_logs}\n\n"
                    f"🕒 Last activity:\n"
                    f"{rule.last_triggered or 'Never'}",

                    reply_markup=
                        rule_action_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # DASHBOARD
        # ----------------------------------------------------

        if data == "dashboard":

            rules_count = (
                session
                .query(ForwardRule)
                .count()
            )

            active_count = (
                session
                .query(ForwardRule)
                .filter(
                    ForwardRule.is_active
                    == True
                )
                .count()
            )

            total_forwarded = (
                session
                .query(
                    ForwardRule
                )
                .with_entities(
                    ForwardRule.forwarded_count
                )
                .all()
            )

            total_blocked = (
                session
                .query(
                    ForwardRule
                )
                .with_entities(
                    ForwardRule.blocked_count
                )
                .all()
            )

            total_failed = (
                session
                .query(
                    ForwardRule
                )
                .with_entities(
                    ForwardRule.failed_count
                )
                .all()
            )

            forwarded = sum(
                int(x[0] or 0)
                for x in total_forwarded
            )

            blocked = sum(
                int(x[0] or 0)
                for x in total_blocked
            )

            failed = sum(
                int(x[0] or 0)
                for x in total_failed
            )

            global_status = (
                "🟢 ON"
                if get_global_forwarding_enabled(
                    session
                )
                else
                "🔴 OFF"
            )

            await query.edit_message_text(

                f"📊 BOT DASHBOARD\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"🌐 Global: {global_status}\n"
                f"📋 Total Rules: {rules_count}\n"
                f"🟢 Active Rules: {active_count}\n\n"
                f"📤 Total Forwarded: {forwarded}\n"
                f"🚫 Total Blocked: {blocked}\n"
                f"❌ Total Failed: {failed}",

                reply_markup=
                    main_keyboard(
                        get_global_forwarding_enabled(
                            session
                        )
                    ),
            )

            return

        # ----------------------------------------------------
        # TEST RULE
        # ----------------------------------------------------

        if data.startswith(
            "test_rule|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            context.user_data[
                "test_rule"
            ] = rid

            await query.edit_message_text(

                "🧪 TEST RULE\n\n"
                "अब इस bot को कोई text message भेजें।\n"
                "Bot उसे इस rule के destination पर "
                "test के रूप में भेजने की कोशिश करेगा.",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    f"rule_open|{rid}",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # CLONE RULE
        # ----------------------------------------------------

        if data.startswith(
            "clone_rule|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            original = session.get(
                ForwardRule,
                rid,
            )

            if original:

                clone = ForwardRule(

                    name=
                        original.name
                        + " Copy",

                    source_chat_id=
                        original.source_chat_id,

                    destination_chat_id=
                        original.destination_chat_id,

                    is_active=False,

                    block_links=
                        original.block_links,

                    block_usernames=
                        original.block_usernames,

                    blacklist_words=
                        list(
                            original.blacklist_words
                            or []
                        ),

                    whitelist_words=
                        list(
                            original.whitelist_words
                            or []
                        ),

                    text_replacements=
                        dict(
                            original.text_replacements
                            or {}
                        ),

                    header_text=
                        original.header_text,

                    footer_text=
                        original.footer_text,

                    forward_mode=
                        original.forward_mode,

                    forward_delay=
                        original.forward_delay,

                    retry_enabled=
                        original.retry_enabled,

                    max_retries=
                        original.max_retries,

                    schedule_start=
                        original.schedule_start,

                    schedule_end=
                        original.schedule_end,

                    allow_text=
                        original.allow_text,

                    allow_photo=
                        original.allow_photo,

                    allow_video=
                        original.allow_video,

                    allow_document=
                        original.allow_document,

                    allow_audio=
                        original.allow_audio,

                    allow_animation=
                        original.allow_animation,

                    allow_sticker=
                        original.allow_sticker,

                    allow_other=
                        original.allow_other,

                )

                session.add(clone)

                add_activity(
                    session,
                    "RULE_CLONED",
                    f"From #{rid}",
                    clone.id,
                )

                session.commit()

                await query.edit_message_text(

                    f"✅ Rule cloned.\n\n"
                    f"New Rule ID: #{clone.id}\n"
                    f"Status: OFF",

                    reply_markup=
                        rule_action_keyboard(
                            clone
                        ),
                )

            return

        # ----------------------------------------------------
        # DELETE CONFIRM
        # ----------------------------------------------------

        if data.startswith(
            "delete_confirm|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if not rule:
                return

            await query.edit_message_text(

                f"⚠️ DELETE RULE #{rid}\n\n"
                f"Name: {rule.name}\n\n"
                "This cannot be undone from the bot.\n"
                "Are you sure?",

                reply_markup=
                    InlineKeyboardMarkup([

                        [
                            InlineKeyboardButton(
                                "🗑 YES, DELETE",
                                callback_data=
                                    f"delete_rule|{rid}",
                            ),

                            InlineKeyboardButton(
                                "❌ CANCEL",
                                callback_data=
                                    f"rule_open|{rid}",
                            ),
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # DELETE
        # ----------------------------------------------------

        if data.startswith(
            "delete_rule|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                add_activity(
                    session,
                    "RULE_DELETED",
                    rule.name,
                    rid,
                )

                session.delete(rule)

                session.commit()

            await query.edit_message_text(

                "🗑 Rule deleted.",

                reply_markup=
                    main_keyboard(
                        get_global_forwarding_enabled(
                            session
                        )
                    ),
            )

            return

        # ----------------------------------------------------
        # EXPORT
        # ----------------------------------------------------

        if data.startswith(
            "export_rule|"
        ):

            rid = int(
                data.split("|", 1)[1]
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                payload = rule_to_dict(
                    rule
                )

                path = None

                try:

                    fd, path = tempfile.mkstemp(
                        prefix="rule_",
                        suffix=".json",
                    )

                    os.close(fd)

                    with open(
                        path,
                        "w",
                        encoding="utf-8",
                    ) as file:

                        json.dump(
                            payload,
                            file,
                            ensure_ascii=False,
                            indent=2,
                        )

                    await query.message.reply_document(

                        document=
                            InputFile(
                                path,
                                filename=
                                    f"rule_{rid}.json",
                            ),

                        caption=
                            f"📤 Rule #{rid} exported.",
                    )

                finally:

                    if path:

                        try:
                            os.remove(path)
                        except OSError:
                            pass

            return

        # ----------------------------------------------------
        # BACKUP MENU
        # ----------------------------------------------------

        if data == "backup_menu":

            await query.edit_message_text(

                "💾 BACKUP / RESTORE\n\n"
                "Backup में सभी rules, filters, "
                "schedules और global state save होगी.",

                reply_markup=
                    backup_keyboard(),
            )

            return

        # ----------------------------------------------------
        # CREATE BACKUP
        # ----------------------------------------------------

        if data == "create_backup":

            backup = build_backup(
                session
            )

            path = None

            try:

                fd, path = tempfile.mkstemp(
                    prefix="telegram_bot_backup_",
                    suffix=".json",
                )

                os.close(fd)

                with open(
                    path,
                    "w",
                    encoding="utf-8",
                ) as file:

                    json.dump(
                        backup,
                        file,
                        ensure_ascii=False,
                        indent=2,
                    )

                await query.message.reply_document(

                    document=
                        InputFile(
                            path,
                            filename=
                                "bot_backup.json",
                        ),

                    caption=(
                        "✅ Full backup created.\n"
                        f"Rules: "
                        f"{len(backup['rules'])}\n"
                        f"Global: "
                        f"{'ON' if backup['global_forwarding_enabled'] else 'OFF'}"
                    ),
                )

            finally:

                if path:

                    try:
                        os.remove(path)
                    except OSError:
                        pass

            return

        # ----------------------------------------------------
        # RESTORE
        # ----------------------------------------------------

        if data == "restore_backup":

            context.user_data[
                "awaiting_restore"
            ] = True

            await query.edit_message_text(

                "♻️ RESTORE MODE\n\n"
                "अब `bot_backup.json` document भेजें.\n\n"
                "⚠️ Existing rules replace हो जाएँगी.",

                reply_markup=
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "❌ Cancel",
                                callback_data=
                                    "cancel_restore",
                            )
                        ]
                    ]),
            )

            return

        # ----------------------------------------------------
        # CANCEL RESTORE
        # ----------------------------------------------------

        if data == "cancel_restore":

            context.user_data.pop(
                "awaiting_restore",
                None,
            )

            await query.edit_message_text(

                "Restore cancelled.",

                reply_markup=
                    main_keyboard(
                        get_global_forwarding_enabled(
                            session
                        )
                    ),
            )

            return

        # ----------------------------------------------------
        # ACTIVITY
        # ----------------------------------------------------

        if data == "activity":

            logs = (
                session
                .query(ActivityLog)
                .order_by(
                    ActivityLog.id.desc()
                )
                .limit(15)
                .all()
            )

            if not logs:

                text = (
                    "📋 Activity Log\n\n"
                    "No activity yet."
                )

            else:

                lines = [
                    "📋 ACTIVITY LOG",
                    "━━━━━━━━━━━━━━━━━━",
                ]

                for log in logs:

                    timestamp = (
                        log.created_at
                        or ""
                    )

                    lines.append(

                        f"\n#{log.id} "
                        f"{log.action}\n"
                        f"Rule: "
                        f"{log.rule_id or '-'}\n"
                        f"{short_text(log.details,100)}\n"
                        f"{timestamp}"
                    )

                text = "\n".join(
                    lines
                )

            await query.edit_message_text(

                text,

                reply_markup=
                    main_keyboard(
                        get_global_forwarding_enabled(
                            session
                        )
                    ),
            )

            return

        # ----------------------------------------------------
        # GLOBAL INFO
        # ----------------------------------------------------

        if data == "global_info":

            rules_count = (
                session
                .query(ForwardRule)
                .count()
            )

            active_count = (
                session
                .query(ForwardRule)
                .filter(
                    ForwardRule.is_active
                    == True
                )
                .count()
            )

            await query.edit_message_text(

                f"ℹ️ SYSTEM INFO\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Admin ID: {FORCE_ADMIN_ID}\n"
                f"🗄 Database: "
                f"{DATABASE_URL.split('@')[-1]}\n"
                f"🌍 Timezone: {TIMEZONE}\n"
                f"📋 Rules: {rules_count}\n"
                f"🟢 Active: {active_count}\n"
                f"🌐 Global: "
                f"{'ON' if get_global_forwarding_enabled(session) else 'OFF'}",

                reply_markup=
                    main_keyboard(
                        get_global_forwarding_enabled(
                            session
                        )
                    ),
            )

            return

        # ----------------------------------------------------
        # NOOP
        # ----------------------------------------------------

        if data == "noop":

            return

    except Exception as error:

        session.rollback()

        logger.exception(
            "Callback error"
        )

        try:

            await query.message.reply_text(

                "❌ Operation failed.\n\n"
                f"Error: {error}"
            )

        except Exception:
            pass

    finally:

        session.close()


# ============================================================
# TEXT MESSAGE HANDLER
# ============================================================

async def text_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not admin_check(
        user.id if user else None
    ):
        return

    if not update.message:
        return

    text_value = (
        update.message.text
        or
        ""
    ).strip()

    if not text_value:
        return

    session = Session()

    try:

        # ----------------------------------------------------
        # CREATE RULE
        # ----------------------------------------------------

        if (
            "creating_rule"
            in context.user_data
        ):

            state = (
                context.user_data[
                    "creating_rule"
                ]
            )

            if "source" not in state:

                if not (
                    text_value.startswith("-100")
                    or
                    text_value.startswith("@")
                    or
                    text_value.lstrip("-").isdigit()
                ):

                    await update.message.reply_text(

                        "❌ Invalid source.\n"
                        "Use -100... or @username"
                    )

                    return

                state["source"] = (
                    text_value
                )

                await update.message.reply_text(

                    "📤 Now send DESTINATION ID/@username"
                )

                return

            if "dest" not in state:

                if not (
                    text_value.startswith("-100")
                    or
                    text_value.startswith("@")
                    or
                    text_value.lstrip("-").isdigit()
                ):

                    await update.message.reply_text(

                        "❌ Invalid destination."
                    )

                    return

                state["dest"] = (
                    text_value
                )

                await update.message.reply_text(

                    "📝 Now send a name for this rule."
                )

                return

            if "name" not in state:

                state["name"] = (
                    text_value[:64]
                )

                rule = ForwardRule(

                    name=
                        state["name"],

                    source_chat_id=
                        state["source"],

                    destination_chat_id=
                        state["dest"],

                    is_active=True,

                    blacklist_words=[],

                    whitelist_words=[],

                    text_replacements={},

                    forward_mode=
                        "FORWARD",

                    forward_delay=0,

                    retry_enabled=True,

                    max_retries=3,

                )

                session.add(rule)

                session.commit()

                add_activity(
                    session,
                    "RULE_CREATED",
                    rule.name,
                    rule.id,
                )

                session.commit()

                context.user_data.pop(
                    "creating_rule",
                    None,
                )

                await update.message.reply_text(

                    "✅ RULE CREATED\n\n"
                    + rule_summary(rule),

                    reply_markup=
                        rule_action_keyboard(
                            rule
                        ),
                )

                return

        # ----------------------------------------------------
        # EDIT NAME
        # ----------------------------------------------------

        if (
            "edit_name_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "edit_name_rule"
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.name = (
                    text_value[:64]
                )

                session.commit()

                await update.message.reply_text(

                    "✅ Rule name updated.",

                    reply_markup=
                        rule_action_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # EDIT SOURCE
        # ----------------------------------------------------

        if (
            "edit_source_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "edit_source_rule"
            )

            if not (
                text_value.startswith("-100")
                or
                text_value.startswith("@")
                or
                text_value.lstrip("-").isdigit()
            ):

                await update.message.reply_text(
                    "❌ Invalid source ID."
                )

                context.user_data[
                    "edit_source_rule"
                ] = rid

                return

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.source_chat_id = (
                    text_value
                )

                session.commit()

                await update.message.reply_text(

                    "✅ Source updated.",

                    reply_markup=
                        rule_action_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # EDIT DESTINATION
        # ----------------------------------------------------

        if (
            "edit_dest_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "edit_dest_rule"
            )

            if not (
                text_value.startswith("-100")
                or
                text_value.startswith("@")
                or
                text_value.lstrip("-").isdigit()
            ):

                await update.message.reply_text(
                    "❌ Invalid destination ID."
                )

                context.user_data[
                    "edit_dest_rule"
                ] = rid

                return

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.destination_chat_id = (
                    text_value
                )

                session.commit()

                await update.message.reply_text(

                    "✅ Destination updated.",

                    reply_markup=
                        rule_action_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # DELAY
        # ----------------------------------------------------

        if (
            "set_delay_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "set_delay_rule"
            )

            try:

                value = int(
                    text_value
                )

                value = max(
                    0,
                    min(
                        value,
                        3600,
                    )
                )

            except ValueError:

                await update.message.reply_text(
                    "❌ Send a number in seconds."
                )

                context.user_data[
                    "set_delay_rule"
                ] = rid

                return

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.forward_delay = (
                    value
                )

                session.commit()

                await update.message.reply_text(

                    f"✅ Delay set to {value}s.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # RETRIES
        # ----------------------------------------------------

        if (
            "set_retries_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "set_retries_rule"
            )

            try:

                value = int(
                    text_value
                )

                value = max(
                    1,
                    min(
                        value,
                        10,
                    )
                )

            except ValueError:

                await update.message.reply_text(
                    "❌ Send a number from 1 to 10."
                )

                context.user_data[
                    "set_retries_rule"
                ] = rid

                return

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                rule.max_retries = (
                    value
                )

                session.commit()

                await update.message.reply_text(

                    f"✅ Max retries: {value}",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # REPLACEMENT
        # ----------------------------------------------------

        if (
            "add_replace_rule"
            in context.user_data
            and
            "replace_find"
            not in context.user_data
        ):

            context.user_data[
                "replace_find"
            ] = text_value

            await update.message.reply_text(

                "✏️ Now send REPLACE text:"
            )

            return

        if (
            "add_replace_rule"
            in context.user_data
            and
            "replace_find"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "add_replace_rule"
            )

            find = context.user_data.pop(
                "replace_find"
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                replacements = (
                    rule.text_replacements
                    or {}
                )

                replacements[find] = (
                    text_value
                )

                rule.text_replacements = (
                    replacements
                )

                session.commit()

                await update.message.reply_text(

                    "✅ Replacement saved.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # BLACKLIST
        # ----------------------------------------------------

        if (
            "add_blacklist_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "add_blacklist_rule"
            )

            word = text_value.lower()

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                words = (
                    rule.blacklist_words
                    or []
                )

                if word not in words:

                    words.append(word)

                rule.blacklist_words = (
                    words
                )

                session.commit()

                await update.message.reply_text(

                    "🚫 Blacklist updated.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # WHITELIST
        # ----------------------------------------------------

        if (
            "add_whitelist_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "add_whitelist_rule"
            )

            word = text_value.lower()

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                words = (
                    rule.whitelist_words
                    or []
                )

                if word not in words:

                    words.append(word)

                rule.whitelist_words = (
                    words
                )

                session.commit()

                await update.message.reply_text(

                    "✅ Whitelist updated.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        if (
            "edit_header_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "edit_header_rule"
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                if text_value.lower() == "remove":

                    rule.header_text = None

                else:

                    rule.header_text = (
                        text_value
                    )

                session.commit()

                await update.message.reply_text(

                    "✅ Header updated.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # FOOTER
        # ----------------------------------------------------

        if (
            "edit_footer_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "edit_footer_rule"
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                if text_value.lower() == "remove":

                    rule.footer_text = None

                else:

                    rule.footer_text = (
                        text_value
                    )

                session.commit()

                await update.message.reply_text(

                    "✅ Footer updated.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

            return

        # ----------------------------------------------------
        # SCHEDULE
        # ----------------------------------------------------

        if (
            "set_schedule_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "set_schedule_rule"
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if not rule:
                return

            if text_value.lower() == "any":

                rule.schedule_start = None
                rule.schedule_end = None

                session.commit()

                await update.message.reply_text(

                    "🕒 Schedule cleared.",

                    reply_markup=
                        settings_keyboard(
                            rule
                        ),
                )

                return

            parts = text_value.split()

            if len(parts) != 2:

                await update.message.reply_text(

                    "❌ Format:\n"
                    "09:00 21:30"
                )

                context.user_data[
                    "set_schedule_rule"
                ] = rid

                return

            start, end = parts

            try:

                datetime.strptime(
                    start,
                    "%H:%M",
                )

                datetime.strptime(
                    end,
                    "%H:%M",
                )

            except Exception:

                await update.message.reply_text(

                    "❌ Invalid time.\n"
                    "Use HH:MM."
                )

                context.user_data[
                    "set_schedule_rule"
                ] = rid

                return

            rule.schedule_start = start
            rule.schedule_end = end

            session.commit()

            await update.message.reply_text(

                "✅ Schedule saved.",

                reply_markup=
                    settings_keyboard(
                        rule
                    ),
            )

            return

        # ----------------------------------------------------
        # TEST RULE
        # ----------------------------------------------------

        if (
            "test_rule"
            in context.user_data
        ):

            rid = context.user_data.pop(
                "test_rule"
            )

            rule = session.get(
                ForwardRule,
                rid,
            )

            if rule:

                try:

                    await context.bot.send_message(

                        chat_id=
                            rule.destination_chat_id,

                        text=
                            "🧪 TEST MESSAGE\n\n"
                            + text_value,
                    )

                    await update.message.reply_text(

                        "✅ Test message sent.",

                        reply_markup=
                            rule_action_keyboard(
                                rule
                            ),
                    )

                except Exception as error:

                    await update.message.reply_text(

                        "❌ Test failed.\n\n"
                        f"{error}",

                        reply_markup=
                            rule_action_keyboard(
                                rule
                            ),
                    )

            return

    finally:

        session.close()


# ============================================================
# DOCUMENT RESTORE HANDLER
# ============================================================

async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not admin_check(
        user.id if user else None
    ):
        return

    if not context.user_data.get(
        "awaiting_restore"
    ):
        return

    document = (
        update.message.document
        if update.message
        else None
    )

    if not document:
        return

    if (
        document.file_size
        and
        document.file_size
        > 5 * 1024 * 1024
    ):

        await update.message.reply_text(
            "❌ Backup maximum 5 MB."
        )

        return

    path = None

    try:

        tg_file = await context.bot.get_file(
            document.file_id
        )

        fd, path = tempfile.mkstemp(
            prefix="restore_",
            suffix=".json",
        )

        os.close(fd)

        await tg_file.download_to_drive(
            path
        )

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

        if not isinstance(
            data,
            dict,
        ):

            raise ValueError(
                "Invalid backup"
            )

        rules_data = data.get(
            "rules"
        )

        if not isinstance(
            rules_data,
            list,
        ):

            raise ValueError(
                "Backup rules missing"
            )

        session = Session()

        try:

            # Delete current rules
            session.query(
                ForwardRule
            ).delete()

            for item in rules_data:

                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                source = str(
                    item.get(
                        "source_chat_id",
                        "",
                    )
                ).strip()

                destination = str(
                    item.get(
                        "destination_chat_id",
                        "",
                    )
                ).strip()

                if not source or not destination:
                    continue

                rule = ForwardRule(

                    name=str(
                        item.get(
                            "name",
                            "restored_rule",
                        )
                    )[:64],

                    source_chat_id=
                        source,

                    destination_chat_id=
                        destination,

                    is_active=
                        bool(
                            item.get(
                                "is_active",
                                True,
                            )
                        ),

                    block_links=
                        bool(
                            item.get(
                                "block_links",
                                False,
                            )
                        ),

                    block_usernames=
                        bool(
                            item.get(
                                "block_usernames",
                                False,
                            )
                        ),

                    blacklist_words=
                        list(
                            item.get(
                                "blacklist_words",
                                [],
                            )
                            or []
                        ),

                    whitelist_words=
                        list(
                            item.get(
                                "whitelist_words",
                                [],
                            )
                            or []
                        ),

                    text_replacements=
                        dict(
                            item.get(
                                "text_replacements",
                                {},
                            )
                            or {}
                        ),

                    header_text=
                        item.get(
                            "header_text"
                        ),

                    footer_text=
                        item.get(
                            "footer_text"
                        ),

                    forward_mode=
                        (
                            item.get(
                                "forward_mode",
                                "FORWARD",
                            )
                            if item.get(
                                "forward_mode"
                            )
                            in (
                                "FORWARD",
                                "COPY",
                            )
                            else
                            "FORWARD"
                        ),

                    forward_delay=max(
                        0,
                        int(
                            item.get(
                                "forward_delay",
                                0,
                            )
                            or 0
                        ),
                    ),

                    retry_enabled=
                        bool(
                            item.get(
                                "retry_enabled",
                                True,
                            )
                        ),

                    max_retries=max(
                        1,
                        int(
                            item.get(
                                "max_retries",
                                3,
                            )
                            or 3
                        ),
                    ),

                    schedule_start=
                        item.get(
                            "schedule_start"
                        ),

                    schedule_end=
                        item.get(
                            "schedule_end"
                        ),

                    allow_text=
                        bool(
                            item.get(
                                "allow_text",
                                True,
                            )
                        ),

                    allow_photo=
                        bool(
                            item.get(
                                "allow_photo",
                                True,
                            )
                        ),

                    allow_video=
                        bool(
                            item.get(
                                "allow_video",
                                True,
                            )
                        ),

                    allow_document=
                        bool(
                            item.get(
                                "allow_document",
                                True,
                            )
                        ),

                    allow_audio=
                        bool(
                            item.get(
                                "allow_audio",
                                True,
                            )
                        ),

                    allow_animation=
                        bool(
                            item.get(
                                "allow_animation",
                                True,
                            )
                        ),

                    allow_sticker=
                        bool(
                            item.get(
                                "allow_sticker",
                                True,
                            )
                        ),

                    allow_other=
                        bool(
                            item.get(
                                "allow_other",
                                True,
                            )
                        ),

                    forwarded_count=max(
                        0,
                        int(
                            item.get(
                                "forwarded_count",
                                0,
                            )
                            or 0
                        ),
                    ),

                    blocked_count=max(
                        0,
                        int(
                            item.get(
                                "blocked_count",
                                0,
                            )
                            or 0
                        ),
                    ),

                    failed_count=max(
                        0,
                        int(
                            item.get(
                                "failed_count",
                                0,
                            )
                            or 0
                        ),
                    ),
                )

                session.add(rule)

            meta = session.get(
                MetaConfig,
                1,
            )

            enabled = bool(
                data.get(
                    "global_forwarding_enabled",
                    True,
                )
            )

            if not meta:

                meta = MetaConfig(
                    id=1,
                    admin_user_id=
                        FORCE_ADMIN_ID,
                    forwarding_enabled=
                        enabled,
                )

                session.add(meta)

            else:

                meta.forwarding_enabled = (
                    enabled
                )

            session.commit()

            count = (
                session
                .query(ForwardRule)
                .count()
            )

        finally:

            session.close()

        context.user_data.pop(
            "awaiting_restore",
            None,
        )

        await update.message.reply_text(

            "✅ BACKUP RESTORED\n\n"
            f"Rules restored: {count}\n"
            f"Global forwarding: "
            f"{'ON' if enabled else 'OFF'}",

            reply_markup=
                main_keyboard(enabled),
        )

    except Exception as error:

        logger.exception(
            "Restore failed"
        )

        await update.message.reply_text(

            "❌ Restore failed.\n\n"
            f"{error}"
        )

    finally:

        if path:

            try:
                os.remove(path)
            except OSError:
                pass


# ============================================================
# FORWARD MESSAGE HANDLER
# ============================================================

async def forward_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = (
        update.channel_post
        or
        update.message
    )

    if message is None:
        return

    # Do not process admin control messages
    if (
        update.message
        and
        update.effective_user
        and
        update.effective_user.id
        == FORCE_ADMIN_ID
    ):

        return

    media_group_id = getattr(
        message,
        "media_group_id",
        None,
    )

    if media_group_id:

        key = (
            f"{message.chat.id}:"
            f"{media_group_id}"
        )

        pending = (
            context.application
            .bot_data
            .setdefault(
                "pending_albums",
                {},
            )
        )

        album = pending.setdefault(
            key,
            [],
        )

        if not any(
            m.message_id
            ==
            message.message_id
            for m in album
        ):

            album.append(
                message
            )

        # Remove existing jobs
        jobs = (
            context.application
            .job_queue
            .get_jobs_by_name(
                f"album:{key}"
            )
        )

        for job in jobs:

            job.schedule_removal()

        # Wait 1.5 sec for album items
        context.application.job_queue.run_once(

            process_album_job,

            when=1.5,

            data=key,

            name=f"album:{key}",
        )

        return

    await process_single_message(
        message,
        context,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Unhandled bot error",
        exc_info=context.error,
    )

    try:

        await context.bot.send_message(

            chat_id=
                FORCE_ADMIN_ID,

            text=(
                "⚠️ Bot Error\n\n"
                f"{context.error}"
            ),
        )

    except Exception:
        pass


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "Starting Advanced Telegram Forward Bot"
    )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # Buttons
    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # Restore document
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler,
        )
    )

    # Admin text input
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_message_handler,
        )
    )

    # Incoming channel/group messages
    application.add_handler(
        MessageHandler(
            filters.ALL,
            forward_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    port = int(
        os.environ.get(
            "PORT",
            "8080",
        )
    )

    webhook_url = os.environ.get(
        "WEBHOOK_URL"
    )

    if webhook_url:

        logger.info(
            "Starting webhook mode"
        )

        application.run_webhook(

            listen="0.0.0.0",

            port=port,

            url_path=BOT_TOKEN,

            webhook_url=(
                webhook_url.rstrip("/")
                + "/"
                + BOT_TOKEN
            ),
        )

    else:

        logger.info(
            "Starting polling mode"
        )

        application.run_polling(
            allowed_updates=
                Update.ALL_TYPES
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
