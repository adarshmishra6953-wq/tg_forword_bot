# paste this full file replacing your old file
import os
import logging
import time
import re
from typing import Optional, List, Dict

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError

# --------------------------
# 1. Logging Configuration
# --------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --------------------------
# 2. Conversation States
# --------------------------
# Keep states unique integers
(
    SET_SOURCE,
    SET_DESTINATION,
    SET_REPLACEMENT_FIND,
    SET_REPLACEMENT_REPLACE,
    SET_BLACKLIST_WORD,
    SET_WHITELIST_WORD,
    # Multi-rule add flow states
    ADD_RULE_SOURCE,
    ADD_RULE_DEST,
    ADD_RULE_HEADER,
    ADD_RULE_FOOTER,
    ADD_RULE_MODE,
) = range(11)

# --------------------------
# 3. Database Setup (SQLAlchemy)
# --------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL environment variable is not set. Bot will not save settings.")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()
Session = sessionmaker(bind=Engine) if Engine else None

# --------------------------
# 4. Models
# --------------------------
class BotConfig(Base):
    __tablename__ = 'config'
    id = Column(Integer, primary_key=True)
    
    # Global default admin + fallback settings
    SOURCE_CHAT_ID = Column(String, nullable=True)
    DESTINATION_CHAT_ID = Column(String, nullable=True)
    IS_FORWARDING_ACTIVE = Column(Boolean, default=False)
    BLOCK_LINKS = Column(Boolean, default=False)
    BLOCK_USERNAMES = Column(Boolean, default=False)
    FORWARD_DELAY_SECONDS = Column(Integer, default=0)
    ADMIN_USER_ID = Column(Integer, nullable=True)
    FORWARDING_MODE = Column(String, default='FORWARD')  # 'FORWARD' or 'COPY'
    TEXT_REPLACEMENTS = Column(PickleType, default={})
    WORD_BLACKLIST = Column(PickleType, default=[])
    WORD_WHITELIST = Column(PickleType, default=[])

class ForwardRule(Base):
    """
    Each rule represents a mapping from SOURCE_CHAT_ID -> DESTINATION_CHAT_ID
    and stores its own header/footer and rule-specific filters.
    """
    __tablename__ = 'rules'
    id = Column(Integer, primary_key=True)
    
    SOURCE_CHAT_ID = Column(String, nullable=False)   # e.g. '-10012345' or '@channelname'
    DESTINATION_CHAT_ID = Column(String, nullable=False)
    HEADER = Column(String, default="")   # prepend text
    FOOTER = Column(String, default="")   # append text
    IS_ACTIVE = Column(Boolean, default=True)
    FORWARDING_MODE = Column(String, default='FORWARD')  # 'FORWARD' or 'COPY'
    FORWARD_DELAY_SECONDS = Column(Integer, default=0)
    BLOCK_LINKS = Column(Boolean, default=False)
    BLOCK_USERNAMES = Column(Boolean, default=False)
    TEXT_REPLACEMENTS = Column(PickleType, default={})
    WORD_BLACKLIST = Column(PickleType, default=[])
    WORD_WHITELIST = Column(PickleType, default=[])

# Create tables if Engine available
if Engine:
    try:
        Base.metadata.create_all(Engine)
        logger.info("Database tables created/recreated successfully.")
    except OperationalError as e:
        logger.error(f"Database connection error during table creation: {e}")

# --------------------------
# 5. Force admin ID (if you want)
# --------------------------
FORCE_ADMIN_ID = 1695450646  # replace or keep as you want

# --------------------------
# 6. DB helper functions
# --------------------------
def load_global_config() -> BotConfig:
    """Load global config from DB or return default temporary config if DB missing."""
    if not Engine:
        return BotConfig(id=1, IS_FORWARDING_ACTIVE=False, TEXT_REPLACEMENTS={}, WORD_BLACKLIST=[], WORD_WHITELIST=[], ADMIN_USER_ID=None)
    session = Session()
    config = None
    try:
        config = session.query(BotConfig).first()
        if not config:
            config = BotConfig(id=1, IS_FORWARDING_ACTIVE=False)
            session.add(config)
            session.commit()
            session.expunge(config)
            logger.info("New BotConfig entry created in DB.")
        else:
            session.expunge(config)
    except Exception as e:
        logger.error(f"Error loading global config: {e}")
        config = BotConfig(id=1, IS_FORWARDING_ACTIVE=False, TEXT_REPLACEMENTS={}, WORD_BLACKLIST=[], WORD_WHITELIST=[])
    finally:
        session.close()
    if not hasattr(config, 'FORWARDING_MODE') or config.FORWARDING_MODE is None:
        config.FORWARDING_MODE = 'FORWARD'
    return config

def save_global_config(config: BotConfig):
    if not Engine: return
    session = Session()
    try:
        session.merge(config)
        session.commit()
    except Exception as e:
        logger.error(f"Error saving global config: {e}")
    finally:
        session.close()

def load_all_rules() -> List[ForwardRule]:
    """Load all rules from DB (returns list)."""
    if not Engine:
        return []
    session = Session()
    rules = []
    try:
        rules = session.query(ForwardRule).order_by(ForwardRule.id).all()
        # expunge so detached instance can be used safely later
        for r in rules:
            session.expunge(r)
    except Exception as e:
        logger.error(f"Error loading rules: {e}")
        rules = []
    finally:
        session.close()
    return rules

def load_rule_by_id(rule_id: int) -> Optional[ForwardRule]:
    if not Engine:
        return None
    session = Session()
    rule = None
    try:
        rule = session.query(ForwardRule).filter_by(id=rule_id).first()
        if rule:
            session.expunge(rule)
    except Exception as e:
        logger.error(f"Error loading rule {rule_id}: {e}")
        rule = None
    finally:
        session.close()
    return rule

def save_rule(rule: ForwardRule):
    if not Engine: return
    session = Session()
    try:
        session.merge(rule)
        session.commit()
    except Exception as e:
        logger.error(f"Error saving rule: {e}")
    finally:
        session.close()

def delete_rule(rule_id: int):
    if not Engine: return
    session = Session()
    try:
        r = session.query(ForwardRule).filter_by(id=rule_id).first()
        if r:
            session.delete(r)
            session.commit()
    except Exception as e:
        logger.error(f"Error deleting rule: {e}")
    finally:
        session.close()

# --------------------------
# 7. UI helper functions
# --------------------------
def get_current_settings_text(config: BotConfig) -> str:
    status = "Shuru" if config.IS_FORWARDING_ACTIVE else "Ruka Hua"
    links = "Haa" if config.BLOCK_LINKS else "Nahi"
    usernames = "Haa" if config.BLOCK_USERNAMES else "Nahi"
    mode_text = "Forward (Original)" if config.FORWARDING_MODE == 'FORWARD' else "Copy (Caption Edit Possible)"
    replacements_list = "\n".join([f"   - '{f}' -> '{r}'" for f, r in (config.TEXT_REPLACEMENTS or {}).items()]) if (config.TEXT_REPLACEMENTS and len(config.TEXT_REPLACEMENTS) > 0) else "Koi Niyam Set Nahi"
    blacklist_list = ", ".join(config.WORD_BLACKLIST or []) if (config.WORD_BLACKLIST and len(config.WORD_BLACKLIST) > 0) else "Koi Shabdh Block Nahi"
    whitelist_list = ", ".join(config.WORD_WHITELIST or []) if (config.WORD_WHITELIST and len(config.WORD_WHITELIST) > 0) else "Koi Shabdh Jaruri Nahi"
    return (
        f"**Bot Status:** `{status}`\n"
        f"**Forwarding Mode:** `{mode_text}`\n\n"
        f"**Source ID:** `{config.SOURCE_CHAT_ID or 'Set Nahi'}`\n"
        f"**Destination ID:** `{config.DESTINATION_CHAT_ID or 'Set Nahi'}`\n\n"
        f"**Filters:**\n"
        f" - Links Block: `{links}`\n"
        f" - Usernames Block: `{usernames}`\n"
        f" - Forwarding Delay: `{config.FORWARD_DELAY_SECONDS} seconds`\n"
        f" - **Blacklist Words:** `{blacklist_list}`\n"
        f" - **Whitelist Words:** `{whitelist_list}`\n\n"
        f"**Text Replacement Rules:**\n{replacements_list}"
    )

def create_main_menu_keyboard(config: BotConfig) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("➡️ Source Set Karein", callback_data='set_source'),
            InlineKeyboardButton("🎯 Destination Set Karein", callback_data='set_destination')
        ],
        [
            InlineKeyboardButton(f"🔗 Links Block ({'✅' if config.BLOCK_LINKS else '❌'})", callback_data='toggle_block_links'),
            InlineKeyboardButton(f"👤 Usernames Block ({'✅' if config.BLOCK_USERNAMES else '❌'})", callback_data='toggle_block_usernames')
        ],
        [
            InlineKeyboardButton("📝 Text Badalna (Replacement)", callback_data='menu_replacement'),
            InlineKeyboardButton(f"⏰ Schedule ({config.FORWARD_DELAY_SECONDS}s)", callback_data='menu_schedule')
        ],
        [
            InlineKeyboardButton("⛔️ Blacklist Manage Karein", callback_data='menu_blacklist'),
            InlineKeyboardButton("✅ Whitelist Manage Karein", callback_data='menu_whitelist')
        ],
        [
            InlineKeyboardButton(f"📨 Mode: {'COPY' if config.FORWARDING_MODE == 'COPY' else 'FORWARD'}", callback_data='menu_forwarding_mode'),
            InlineKeyboardButton("🧾 Rules (Multi)", callback_data='menu_rules')
        ],
        [
            InlineKeyboardButton("⏸️ Rokein" if config.IS_FORWARDING_ACTIVE else "▶️ Shuru Karein", callback_data='toggle_forwarding'),
            InlineKeyboardButton("🔄 Bot Settings Refresh", callback_data='refresh_config'),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_keyboard(callback_data='main_menu') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data=callback_data)]])

def create_rules_list_keyboard(rules: List[ForwardRule]) -> InlineKeyboardMarkup:
    # List rules with small buttons to toggle or delete
    keyboard = []
    for r in rules:
        label = f"{r.id}: {r.SOURCE_CHAT_ID} → {r.DESTINATION_CHAT_ID} ({'Active' if r.IS_ACTIVE else 'Off'})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f'view_rule_{r.id}')])
        keyboard.append([
            InlineKeyboardButton("Toggle Active", callback_data=f'toggle_rule_{r.id}'),
            InlineKeyboardButton("Delete", callback_data=f'del_rule_{r.id}')
        ])
    keyboard.append([InlineKeyboardButton("➕ Add New Rule", callback_data='add_rule')])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

# --------------------------
# 8. Command Handlers
# --------------------------
GLOBAL_CONFIG = load_global_config()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global GLOBAL_CONFIG
    user_id = update.effective_user.id
    GLOBAL_CONFIG = load_global_config()

    # Force admin if configured
    if FORCE_ADMIN_ID and GLOBAL_CONFIG.ADMIN_USER_ID != FORCE_ADMIN_ID:
        GLOBAL_CONFIG.ADMIN_USER_ID = FORCE_ADMIN_ID
        save_global_config(GLOBAL_CONFIG)
        logger.info(f"Admin User ID forcibly set to: {FORCE_ADMIN_ID}")
    elif GLOBAL_CONFIG.ADMIN_USER_ID is None:
        GLOBAL_CONFIG.ADMIN_USER_ID = user_id
        save_global_config(GLOBAL_CONFIG)
        logger.info(f"Admin User ID set to: {user_id}")

    await update.message.reply_text(
        f"Namaste! Aapka Telegram Auto-Forward Bot shuru ho gaya hai.\n\n"
        f"**Current Settings:**\n{get_current_settings_text(GLOBAL_CONFIG)}",
        reply_markup=create_main_menu_keyboard(GLOBAL_CONFIG),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

# --------------------------
# 9. Callback Handler (Main)
# --------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GLOBAL_CONFIG
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # Admin permission check
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and chat_id != FORCE_ADMIN_ID:
        await query.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return

    GLOBAL_CONFIG = load_global_config()  # reload

    # Basic navigation
    if data == 'main_menu':
        await query.edit_message_text(
            f"**Mukhya Menu (Main Menu)**\n\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_main_menu_keyboard(GLOBAL_CONFIG),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    # toggles
    if data == 'toggle_block_links':
        GLOBAL_CONFIG.BLOCK_LINKS = not GLOBAL_CONFIG.BLOCK_LINKS
        save_global_config(GLOBAL_CONFIG)
    elif data == 'toggle_block_usernames':
        GLOBAL_CONFIG.BLOCK_USERNAMES = not GLOBAL_CONFIG.BLOCK_USERNAMES
        save_global_config(GLOBAL_CONFIG)
    elif data == 'toggle_forwarding':
        GLOBAL_CONFIG.IS_FORWARDING_ACTIVE = not GLOBAL_CONFIG.IS_FORWARDING_ACTIVE
        save_global_config(GLOBAL_CONFIG)

    if data in ['toggle_block_links', 'toggle_block_usernames', 'toggle_forwarding']:
        await query.edit_message_text(
            f"**Setting Updated**\n\nCurrent Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    # refresh
    if data == 'refresh_config':
        save_global_config(GLOBAL_CONFIG)
        await query.edit_message_text(
            f"**Bot Settings Refresh** safaltapoorvak ho gaya hai. Settings Database se **Reload** ho gayi hain.\n\nCurrent Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    # Source / Destination set flows (global)
    if data == 'set_source':
        await query.edit_message_text("Kripya Source Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard())
        return SET_SOURCE
    if data == 'set_destination':
        await query.edit_message_text("Kripya Destination Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard())
        return SET_DESTINATION

    # Schedule menu (global)
    if data == 'menu_schedule':
        keyboard = [
            [InlineKeyboardButton("0 Sec (Default)", callback_data='set_delay_0'), InlineKeyboardButton("5 Sec", callback_data='set_delay_5')],
            [InlineKeyboardButton("15 Sec", callback_data='set_delay_15'), InlineKeyboardButton("30 Sec", callback_data='set_delay_30')],
            [InlineKeyboardButton("60 Sec (1 Minute)", callback_data='set_delay_60')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text("Message Forward hone se pehle kitna **Delay** chahiye?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    if data.startswith('set_delay_'):
        delay = int(data.split('_')[2])
        GLOBAL_CONFIG.FORWARD_DELAY_SECONDS = delay
        save_global_config(GLOBAL_CONFIG)
        await query.edit_message_text(
            f"**Forwarding Delay:** Ab `{delay} seconds` set kar diya gaya hai.\n\nCurrent Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    # Forwarding mode (global)
    if data == 'menu_forwarding_mode':
        keyboard = [
            [InlineKeyboardButton(f"1. Forward (Original) {'✅' if GLOBAL_CONFIG.FORWARDING_MODE == 'FORWARD' else '❌'}", callback_data='set_mode_forward')],
            [InlineKeyboardButton(f"2. Copy (Caption Editing) {'✅' if GLOBAL_CONFIG.FORWARDING_MODE == 'COPY' else '❌'}", callback_data='set_mode_copy')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text("Message Forwarding ka **Mode** chunein:\n\n*1. Forward*: Message ka format original rehta hai.\n*2. Copy*: Hamesha Copy hoga, jisse aapka Text Replacement niyam lagu ho sake.", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    if data.startswith('set_mode_'):
        mode = data.split('_')[2].upper()
        GLOBAL_CONFIG.FORWARDING_MODE = mode
        save_global_config(GLOBAL_CONFIG)
        await query.edit_message_text(
            f"**Forwarding Mode** ab `{mode}` set kar diya gaya hai.\n\nCurrent Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    # Replacement / Blacklist / Whitelist menus (global) - reuse your existing logic
    if data == 'menu_replacement':
        keyboard = [[InlineKeyboardButton("➕ Naya Niyam Jodein", callback_data='add_replacement_find')], [InlineKeyboardButton("🗑️ Saare Niyam Hatayein", callback_data='clear_replacements')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]]
        await query.edit_message_text(f"**Text Replacement Niyam (Global)**\n\n{get_current_settings_text(GLOBAL_CONFIG)}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return ConversationHandler.END

    if data == 'add_replacement_find':
        await query.edit_message_text("Vah **Text Bhejein** jise aap Message mein **Dhoondhna** chahte hain (Find Text).", reply_markup=create_back_keyboard('menu_replacement'))
        return SET_REPLACEMENT_FIND
    if data == 'clear_replacements':
        GLOBAL_CONFIG.TEXT_REPLACEMENTS = {}
        save_global_config(GLOBAL_CONFIG)
        await query.edit_message_text("**Saare Text Replacement Niyam (Global)** hata diye gaye hain.", reply_markup=create_back_keyboard())
        return ConversationHandler.END

    # Blacklist
    if data == 'menu_blacklist':
        keyboard = [[InlineKeyboardButton("➕ Shabdh Blacklist Karein", callback_data='add_blacklist_word')], [InlineKeyboardButton("🗑️ Saare Blacklist Hatayein", callback_data='clear_blacklist')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]]
        await query.edit_message_text(f"**Word Blacklist Settings (Global)**\n\nCurrent Blacklisted Words: {', '.join(GLOBAL_CONFIG.WORD_BLACKLIST or []) or 'Koi nahi'}", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    if data == 'add_blacklist_word':
        await query.edit_message_text("Kripya vah **Shabdh** bhejein jise aap **Block** karna chahte hain.", reply_markup=create_back_keyboard('menu_blacklist'))
        return SET_BLACKLIST_WORD
    if data == 'clear_blacklist':
        GLOBAL_CONFIG.WORD_BLACKLIST = []
        save_global_config(GLOBAL_CONFIG)
        await query.edit_message_text("**Saare Blacklisted Shabdh (Global)** hata diye gaye hain.", reply_markup=create_back_keyboard())
        return ConversationHandler.END

    # Whitelist
    if data == 'menu_whitelist':
        keyboard = [[InlineKeyboardButton("➕ Shabdh Whitelist Karein", callback_data='add_whitelist_word')], [InlineKeyboardButton("🗑️ Saare Whitelist Hatayein", callback_data='clear_whitelist')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]]
        await query.edit_message_text(f"**Word Whitelist Settings (Global)**\n\nCurrent Whitelisted Words: {', '.join(GLOBAL_CONFIG.WORD_WHITELIST or []) or 'Koi nahi'} (Inka hona jaruri hai)", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    if data == 'add_whitelist_word':
        await query.edit_message_text("Kripya vah **Shabdh** bhejein jiska Message mein **Hona Jaruri** hai.", reply_markup=create_back_keyboard('menu_whitelist'))
        return SET_WHITELIST_WORD
    if data == 'clear_whitelist':
        GLOBAL_CONFIG.WORD_WHITELIST = []
        save_global_config(GLOBAL_CONFIG)
        await query.edit_message_text("**Saare Whitelisted Shabdh (Global)** hata diye gaye hain.", reply_markup=create_back_keyboard())
        return ConversationHandler.END

    # -------------------------
    # Rules (Multi-rule) Menu
    # -------------------------
    if data == 'menu_rules':
        rules = load_all_rules()
        if not rules:
            keyboard = [[InlineKeyboardButton("➕ Add New Rule", callback_data='add_rule')], [InlineKeyboardButton("⬅️ Back", callback_data='main_menu')]]
            await query.edit_message_text("Abhi koi rule nahi hai. Naya rule jodne ke liye button dabayein.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("Rules List:", reply_markup=create_rules_list_keyboard(rules))
        return ConversationHandler.END

    # Add rule start
    if data == 'add_rule':
        await query.edit_message_text("Naya Rule: Kripya Source Channel ka ID ya Username bhejein (eg. -10012345 ya @channelname).", reply_markup=create_back_keyboard('menu_rules'))
        # store a temporary rule container in user_data
        context.user_data['new_rule'] = {}
        return ADD_RULE_SOURCE

    # View a single rule
    if data.startswith('view_rule_'):
        rid = int(data.split('_')[2])
        r = load_rule_by_id(rid)
        if not r:
            await query.edit_message_text("Rule nahi mila.", reply_markup=create_back_keyboard('menu_rules'))
            return ConversationHandler.END
        text = (
            f"Rule ID: `{r.id}`\n"
            f"**{r.SOURCE_CHAT_ID}** ➜ **{r.DESTINATION_CHAT_ID}**\n"
            f"Active: `{r.IS_ACTIVE}`\n"
            f"Mode: `{r.FORWARDING_MODE}`\n"
            f"Delay: `{r.FORWARD_DELAY_SECONDS}s`\n"
            f"Header: `{r.HEADER or 'N/A'}`\n"
            f"Footer: `{r.FOOTER or 'N/A'}`\n"
        )
        keyboard = [
            [InlineKeyboardButton("Toggle Active", callback_data=f'toggle_rule_{r.id}'), InlineKeyboardButton("Delete", callback_data=f'del_rule_{r.id}')],
            [InlineKeyboardButton("⬅️ Back", callback_data='menu_rules')]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return ConversationHandler.END

    # Toggle rule active
    if data.startswith('toggle_rule_'):
        rid = int(data.split('_')[2])
        r = load_rule_by_id(rid)
        if r:
            r.IS_ACTIVE = not r.IS_ACTIVE
            save_rule(r)
            await query.edit_message_text(f"Rule `{rid}` active status ab `{r.IS_ACTIVE}` hai.", reply_markup=create_back_keyboard('menu_rules'), parse_mode='Markdown')
        else:
            await query.edit_message_text("Rule nahi mila.", reply_markup=create_back_keyboard('menu_rules'))
        return ConversationHandler.END

    # Delete rule
    if data.startswith('del_rule_'):
        rid = int(data.split('_')[2])
        delete_rule(rid)
        await query.edit_message_text(f"Rule `{rid}` delete kar diya gaya hai.", reply_markup=create_back_keyboard('menu_rules'))
        return ConversationHandler.END

    # Fallback: do nothing / end
    return ConversationHandler.END

# --------------------------
# 10. Conversation Handlers (Inputs)
# --------------------------
async def handle_chat_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE, config_attr: str) -> int:
    global GLOBAL_CONFIG
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END

    chat_input = update.message.text.strip()
    if not (chat_input.startswith('-100') or chat_input.startswith('@') or chat_input.isdigit()):
         await update.message.reply_text("Galat format! Kripya ID (-100...) ya Username (@...) bhejein.", reply_markup=create_back_keyboard('main_menu'))
         return ConversationHandler.END

    setattr(GLOBAL_CONFIG, config_attr, chat_input)
    save_global_config(GLOBAL_CONFIG)
    
    await update.message.reply_text(
        f"**{config_attr.replace('_', ' ')}** safaltapoorvak `{chat_input}` set kar diya gaya hai.\n\n"
        f"Current Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
        reply_markup=create_back_keyboard(),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def set_source_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_chat_id_input(update, context, "SOURCE_CHAT_ID")

async def set_destination_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_chat_id_input(update, context, "DESTINATION_CHAT_ID")

async def set_list_word(update: Update, context: ContextTypes.DEFAULT_TYPE, list_name: str, callback_menu: str) -> int:
    global GLOBAL_CONFIG
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END
    
    word = update.message.text.strip().lower()
    current_list = getattr(GLOBAL_CONFIG, list_name) or []
    
    if word not in current_list:
        current_list.append(word)
        setattr(GLOBAL_CONFIG, list_name, current_list)
        save_global_config(GLOBAL_CONFIG)
        msg = f"Shabdh: **'{word}'** safaltapoorvak **{list_name.split('_')[1]}** mein jod diya gaya hai."
    else:
        msg = f"Shabdh: **'{word}'** pehle se hi **{list_name.split('_')[1]}** mein hai."

    await update.message.reply_text(
        msg + f"\n\n{list_name.split('_')[1]}: {', '.join(current_list)}",
        reply_markup=create_back_keyboard(callback_menu),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def set_blacklist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_list_word(update, context, "WORD_BLACKLIST", 'menu_blacklist')

async def set_whitelist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_list_word(update, context, "WORD_WHITELIST", 'menu_whitelist')

async def set_replacement_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END
    
    context.user_data['find_text'] = update.message.text.strip()
    await update.message.reply_text(
        f"Ab vah **Text Bhejein** jiske saath aap '{context.user_data['find_text']}' ko **Badalna (Replace)** chahte hain (Replace Text).",
        reply_markup=create_back_keyboard('menu_replacement')
    )
    return SET_REPLACEMENT_REPLACE

async def set_replacement_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global GLOBAL_CONFIG
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END

    find_text = context.user_data.pop('find_text')
    replace_text = update.message.text.strip()
    
    replacements = GLOBAL_CONFIG.TEXT_REPLACEMENTS or {}
    replacements[find_text] = replace_text
    GLOBAL_CONFIG.TEXT_REPLACEMENTS = replacements
    save_global_config(GLOBAL_CONFIG)
    
    await update.message.reply_text(
        f"**Naya Replacement Niyam (Global)** safaltapoorvak set kiya gaya:\n"
        f"**Dhoondhein:** `{find_text}`\n"
        f"**Badlein:** `{replace_text}`\n\n"
        f"Current Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
        reply_markup=create_back_keyboard(),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

# --------------------------
# 11. Multi-rule Add Flow Handlers
# --------------------------
async def add_rule_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Expecting source id / username
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END
    text = update.message.text.strip()
    if not (text.startswith('-100') or text.startswith('@') or text.isdigit()):
        await update.message.reply_text("Galat format! Kripya ID (-100...) ya Username (@...) bhejein.", reply_markup=create_back_keyboard('menu_rules'))
        return ConversationHandler.END
    context.user_data['new_rule']['source'] = text
    await update.message.reply_text("Ab Destination Channel ka ID ya Username bhejein.", reply_markup=create_back_keyboard('menu_rules'))
    return ADD_RULE_DEST

async def add_rule_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not (text.startswith('-100') or text.startswith('@') or text.isdigit()):
        await update.message.reply_text("Galat format! Kripya ID (-100...) ya Username (@...) bhejein.", reply_markup=create_back_keyboard('menu_rules'))
        return ConversationHandler.END
    context.user_data['new_rule']['dest'] = text
    await update.message.reply_text("Optional: Header bhejein (message ke aage jo lagana hai). Agar nahi chahte to `-` bhejein.", reply_markup=create_back_keyboard('menu_rules'))
    return ADD_RULE_HEADER

async def add_rule_header(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['new_rule']['header'] = "" if text == '-' else text
    await update.message.reply_text("Optional: Footer bhejein (message ke end mein jo lagana hai). Agar nahi chahte to `-` bhejein.", reply_markup=create_back_keyboard('menu_rules'))
    return ADD_RULE_FOOTER

async def add_rule_footer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['new_rule']['footer'] = "" if text == '-' else text
    # Choose mode
    keyboard = [
        [InlineKeyboardButton("1. Forward (Original)", callback_data='add_rule_mode_forward')],
        [InlineKeyboardButton("2. Copy (Always Copy)", callback_data='add_rule_mode_copy')],
        [InlineKeyboardButton("⬅️ Cancel", callback_data='menu_rules')]
    ]
    await update.message.reply_text("Choose Forwarding Mode for this rule:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_RULE_MODE

async def add_rule_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == 'add_rule_mode_forward':
        mode = 'FORWARD'
    else:
        mode = 'COPY'
    nr = context.user_data.get('new_rule', {})
    # Build ForwardRule instance and save
    rule = ForwardRule(
        SOURCE_CHAT_ID=nr.get('source'),
        DESTINATION_CHAT_ID=nr.get('dest'),
        HEADER=nr.get('header', ''),
        FOOTER=nr.get('footer', ''),
        FORWARDING_MODE=mode,
        IS_ACTIVE=True
    )
    save_rule(rule)
    # Clear temp
    context.user_data.pop('new_rule', None)
    await query.edit_message_text(f"Naya rule ban gaya hai:\n`{rule.SOURCE_CHAT_ID}` ➜ `{rule.DESTINATION_CHAT_ID}`\nMode: `{rule.FORWARDING_MODE}`", parse_mode='Markdown', reply_markup=create_back_keyboard('menu_rules'))
    return ConversationHandler.END

# --------------------------
# 12. Core Forwarding Logic (uses per-rule config if applicable)
# --------------------------
def match_rule_for_message(rules: List[ForwardRule], message_chat_id: int) -> Optional[ForwardRule]:
    """
    Find the first active rule matching message.chat.id.
    Matching logic: if str(message_chat_id) in rule.SOURCE_CHAT_ID (backward compatibility)
    or exact match or username match ('@name'). This is simple but effective.
    """
    mid = str(message_chat_id)
    for r in rules:
        if not r.IS_ACTIVE:
            continue
        src = r.SOURCE_CHAT_ID or ""
        # Accept if src equals or contains or matches @username
        if src == mid or mid in src or (src.startswith('@') and src[1:] in mid) or (src.startswith('@') and src[1:] in src):
            return r
        # also allow when bot owner entered multiple sources separated by comma
        if ',' in src:
            parts = [p.strip() for p in src.split(',') if p.strip()]
            if mid in parts or any(p == mid or (p.startswith('@') and p[1:] == mid) for p in parts):
                return r
    return None

async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    For each incoming message, find matching rule. If none, fallback to global config behavior (if set).
    """
    message = update.channel_post or update.message
    if not message:
        return

    # Reload global config each time
    config = load_global_config()
    if not config.IS_FORWARDING_ACTIVE:
        return

    rules = load_all_rules()
    rule = match_rule_for_message(rules, message.chat.id) if rules else None

    # If a rule matched, use rule settings; else try global SOURCE check
    use_rule = rule is not None

    # If no rule matched, but global SOURCE is set and message.chat.id in it — use global config as fallback
    if not use_rule:
        source_id_str = config.SOURCE_CHAT_ID
        if not (source_id_str and str(message.chat.id) in source_id_str):
            return
        # build a pseudo-rule from global config
        rule = ForwardRule(
            SOURCE_CHAT_ID=config.SOURCE_CHAT_ID,
            DESTINATION_CHAT_ID=config.DESTINATION_CHAT_ID,
            HEADER="",
            FOOTER="",
            FORWARDING_MODE=config.FORWARDING_MODE,
            FORWARD_DELAY_SECONDS=config.FORWARD_DELAY_SECONDS,
            BLOCK_LINKS=config.BLOCK_LINKS,
            BLOCK_USERNAMES=config.BLOCK_USERNAMES,
            TEXT_REPLACEMENTS=config.TEXT_REPLACEMENTS or {},
            WORD_BLACKLIST=config.WORD_BLACKLIST or [],
            WORD_WHITELIST=config.WORD_WHITELIST or []
        )

    if not rule.DESTINATION_CHAT_ID:
        admin_to_notify = config.ADMIN_USER_ID or FORCE_ADMIN_ID
        if admin_to_notify:
            await context.bot.send_message(admin_to_notify, f"Source Message aaya, lekin Destination ID set nahi hai for rule/source `{rule.SOURCE_CHAT_ID}`!")
        return

    text_to_process = message.text or message.caption or ""
    text_lower = (text_to_process or "").lower()

    # Per-rule filters
    if rule.BLOCK_LINKS and ('http' in text_lower or 't.me' in text_lower):
        return
    if rule.BLOCK_USERNAMES and re.search(r'@[a-zA-Z0-9_]+', text_lower):
        return

    # Blacklist
    if rule.WORD_BLACKLIST:
        for w in rule.WORD_BLACKLIST:
            if w and w in text_lower:
                return

    # Whitelist: must contain at least one whitelist word if whitelist exists
    if rule.WORD_WHITELIST:
        is_whitelisted = False
        for w in rule.WORD_WHITELIST:
            if w and w in text_lower:
                is_whitelisted = True
                break
        if not is_whitelisted:
            return

    # Text replacements (rule-level then fallback to global if none)
    final_text = text_to_process or ""
    text_modified = False
    replacements = rule.TEXT_REPLACEMENTS or {}
    if not replacements:
        replacements = config.TEXT_REPLACEMENTS or {}

    if replacements and final_text:
        for find, replace in replacements.items():
            if find in final_text:
                final_text = final_text.replace(find, replace)
                text_modified = True

    # Prepend header / append footer
    header = (rule.HEADER or "") if use_rule else ""
    footer = (rule.FOOTER or "") if use_rule else ""
    if header:
        final_text = f"{header}\n{final_text}"
        text_modified = True
    if footer:
        final_text = f"{final_text}\n{footer}"
        text_modified = True

    # Apply Delay
    delay = int(rule.FORWARD_DELAY_SECONDS or 0)
    if delay > 0:
        time.sleep(delay)

    # Decide copy vs forward
    force_copy = text_modified or (rule.FORWARDING_MODE == 'COPY')

    original_parse_mode = getattr(message, 'parse_mode', None)
    final_parse_mode = None
    if rule.FORWARDING_MODE == 'FORWARD' and not text_modified and original_parse_mode:
        final_parse_mode = original_parse_mode

    dest_id = rule.DESTINATION_CHAT_ID

    try:
        if force_copy:
            # text-only
            if message.text and not message.caption and not message.photo and not message.video and not message.document:
                if final_text and final_text.strip():
                    await context.bot.send_message(chat_id=dest_id, text=final_text, parse_mode=final_parse_mode, disable_web_page_preview=True)
            # media
            elif message.photo or message.video or message.document or message.audio or message.voice or message.sticker:
                caption_to_send = final_text if final_text else ""
                await context.bot.copy_message(
                    chat_id=dest_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    caption=caption_to_send,
                    parse_mode=final_parse_mode
                )
        else:
            await context.bot.forward_message(chat_id=dest_id, from_chat_id=message.chat.id, message_id=message.message_id)
    except Exception as e:
        logger.error(f"Error copying/sending message for rule {getattr(rule, 'id', None)}: {e}")

# --------------------------
# 13. Main: set up application, handlers
# --------------------------
def main() -> None:
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set. Bot cannot start.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback)],
        states={
            SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source_id)],
            SET_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_destination_id)],
            SET_REPLACEMENT_FIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_replacement_find)],
            SET_REPLACEMENT_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_replacement_replace)],
            SET_BLACKLIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_blacklist_word)], 
            SET_WHITELIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_whitelist_word)],
            # add rule flow
            ADD_RULE_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rule_source)],
            ADD_RULE_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rule_dest)],
            ADD_RULE_HEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rule_header)],
            ADD_RULE_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rule_footer)],
            # ADD_RULE_MODE will be handled by callback handler (callback query)
        },
        fallbacks=[
            CallbackQueryHandler(handle_callback),
            CommandHandler("start", start)
        ],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    # callback for add_rule_mode
    application.add_handler(CallbackQueryHandler(add_rule_mode_callback, pattern=r'^add_rule_mode_.*$'))
    application.add_handler(CallbackQueryHandler(handle_callback))
    # message handler (all messages & channel posts)
    application.add_handler(MessageHandler(filters.ALL, forward_message))

    # webhook/polling
    PORT = int(os.environ.get("PORT", "8080"))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        logger.info(f"Starting bot with Webhook on URL: {WEBHOOK_URL} using port {PORT}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        logger.warning("WEBHOOK_URL not set. Falling back to Polling. (Not recommended for Render Web Services)")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
