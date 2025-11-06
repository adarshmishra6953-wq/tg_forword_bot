import os
import logging
import time
import re
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

# 1. Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States (Ensure these are unique integers)
SET_SOURCE, SET_DESTINATION, SET_REPLACEMENT_FIND, SET_REPLACEMENT_REPLACE, SET_BLACKLIST_WORD, SET_WHITELIST_WORD = range(6)

# 2. Database Setup (SQLAlchemy)
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # Changed from logger.error to logger.warning as script can technically run without DB
    logger.warning("DATABASE_URL environment variable is not set. Bot will not save settings.")

# Adjust URL format for Render/Heroku PostgreSQL compatibility
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# 3. Database Model (All bot settings are stored here)
class BotConfig(Base):
    __tablename__ = 'config'
    id = Column(Integer, primary_key=True)
    
    SOURCE_CHAT_ID = Column(String)
    DESTINATION_CHAT_ID = Column(String)
    IS_FORWARDING_ACTIVE = Column(Boolean, default=True)
    BLOCK_LINKS = Column(Boolean, default=False)
    BLOCK_USERNAMES = Column(Boolean, default=False)
    FORWARD_DELAY_SECONDS = Column(Integer, default=0)
    ADMIN_USER_ID = Column(Integer)
    
    # NEW FIELD: 'FORWARD' (default) or 'COPY'
    FORWARDING_MODE = Column(String, default='FORWARD') 
    
    TEXT_REPLACEMENTS = Column(PickleType, default={})
    WORD_BLACKLIST = Column(PickleType, default=[]) 
    WORD_WHITELIST = Column(PickleType, default=[]) 

# Create tables if Engine is available
if Engine:
    try:
        Base.metadata.create_all(Engine)
    except OperationalError as e:
        logger.error(f"Database connection error during table creation: {e}")

# === VAHAN JODEIN JAHAN GLOBAL_CONFIG load kiya jata hai ===
# Example: Use your actual Telegram User ID here to force admin status
# Isse ADMIN_USER_ID hamesha set rahega, bhale hi DB reset ho jaye.
# APNI ASLI TELEGRAM USER ID YAHAN DALEN (e.g., 1234567890)
FORCE_ADMIN_ID = 1695450646 # <--- **APKI ID YAHAN SET HAI**

# 4. Configuration Management Functions
def load_config_from_db():
    """Load configuration from DB or return a default/error state."""
    if not Engine:
        # Return a temporary config if DB is missing (settings will not save)
        return BotConfig(id=1, IS_FORWARDING_ACTIVE=False, TEXT_REPLACEMENTS={}, WORD_BLACKLIST=[], WORD_WHITELIST=[], ADMIN_USER_ID=None)

    session = Session()
    try:
        config = session.query(BotConfig).first()
        if not config:
            # Create default entry if DB is empty
            config = BotConfig(id=1)
            session.add(config)
            session.commit()
            logger.info("New BotConfig entry created in DB.")
    except Exception as e:
        logger.error(f"Error loading config from DB: {e}")
        # Return a temporary config in case of DB read error
        config = BotConfig(id=1, IS_FORWARDING_ACTIVE=False, TEXT_REPLACEMENTS={}, WORD_BLACKLIST=[], WORD_WHITELIST=[])
    finally:
        session.close()
    
    # Ensure FORWARDING_MODE exists for older databases
    if not hasattr(config, 'FORWARDING_MODE') or config.FORWARDING_MODE is None:
        config.FORWARDING_MODE = 'FORWARD'
        # No immediate save needed here.
        
    return config

def save_config_to_db(config):
    """Save the provided configuration object back to the database."""
    if not Engine: return
    
    session = Session()
    try:
        session.merge(config)
        session.commit()
    except Exception as e:
        logger.error(f"Error saving config to DB: {e}")
    finally:
        session.close()

# Global config instance (Loaded on startup)
GLOBAL_CONFIG = load_config_from_db()

# 5. Utility Functions (Inline Keyboard and Text formatting)

def get_current_settings_text(config):
    """Returns a formatted string of current bot settings."""
    status = "Shuru" if config.IS_FORWARDING_ACTIVE else "Ruka Hua"
    links = "Haa" if config.BLOCK_LINKS else "Nahi"
    usernames = "Haa" if config.BLOCK_USERNAMES else "Nahi"
    
    # Get Mode Text
    mode_text = "Forward (Original)" if config.FORWARDING_MODE == 'FORWARD' else "Copy (Caption Edit Possible)"

    replacements_list = "\n".join(
        [f"   - '{f}' -> '{r}'" for f, r in (config.TEXT_REPLACEMENTS or {}).items()]
    ) if (config.TEXT_REPLACEMENTS and len(config.TEXT_REPLACEMENTS) > 0) else "Koi Niyam Set Nahi"

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

def create_main_menu_keyboard(config):
    """Creates the main inline keyboard menu."""
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
            # New button for Forwarding Mode
            InlineKeyboardButton(f"📨 Mode: {'COPY' if config.FORWARDING_MODE == 'COPY' else 'FORWARD'}", callback_data='menu_forwarding_mode'),
            InlineKeyboardButton("🔄 Bot Settings Refresh", callback_data='refresh_config')
        ],
        [
            InlineKeyboardButton("⏸️ Rokein" if config.IS_FORWARDING_ACTIVE else "▶️ Shuru Karein", callback_data='toggle_forwarding'),
            InlineKeyboardButton("⚙️ Current Settings Dekhein", callback_data='show_settings'),
        ],
        [
            # Button to help copy destination messages (as requested)
            InlineKeyboardButton("📋 Destination Message Copy Karein", url=f"https://t.me/share/url?url=t.me/{config.DESTINATION_CHAT_ID}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_keyboard(callback_data='main_menu'):
    """Creates a back button keyboard."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data=callback_data)]])

# 6. Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /start command and sets the admin user."""
    global GLOBAL_CONFIG
    user_id = update.effective_user.id
    
    # Reload config to ensure we have the latest state on every /start
    GLOBAL_CONFIG = load_config_from_db()

    # FORCE ADMIN ID: Agar FORCE_ADMIN_ID set hai, to use hi use karein
    if FORCE_ADMIN_ID and GLOBAL_CONFIG.ADMIN_USER_ID != FORCE_ADMIN_ID:
        GLOBAL_CONFIG.ADMIN_USER_ID = FORCE_ADMIN_ID
        save_config_to_db(GLOBAL_CONFIG)
        logger.info(f"Admin User ID forcibly set to: {FORCE_ADMIN_ID}")
    
    elif GLOBAL_CONFIG.ADMIN_USER_ID is None:
        GLOBAL_CONFIG.ADMIN_USER_ID = user_id
        save_config_to_db(GLOBAL_CONFIG)
        logger.info(f"Admin User ID set to: {user_id}")
    
    await update.message.reply_text(
        f"Namaste! Aapka Telegram Auto-Forward Bot shuru ho gaya hai.\n\n"
        f"**Current Settings:**\n{get_current_settings_text(GLOBAL_CONFIG)}",
        reply_markup=create_main_menu_keyboard(GLOBAL_CONFIG),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

# 7. Callback Handlers (For Inline Buttons)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all Inline Button presses and transitions conversations."""
    global GLOBAL_CONFIG
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # Admin Check (Ensures only admin can change settings)
    # Check if chat_id matches saved Admin ID OR the forced Admin ID
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and chat_id != FORCE_ADMIN_ID:
        await query.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return

    GLOBAL_CONFIG = load_config_from_db() # Ensure latest config before making changes

    # --- Navigation and Simple Toggles ---
    if data == 'main_menu':
        await query.edit_message_text(
            f"**Mukhya Menu (Main Menu)**\n\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_main_menu_keyboard(GLOBAL_CONFIG),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    elif data == 'toggle_block_links':
        GLOBAL_CONFIG.BLOCK_LINKS = not GLOBAL_CONFIG.BLOCK_LINKS
    elif data == 'toggle_block_usernames':
        GLOBAL_CONFIG.BLOCK_USERNAMES = not GLOBAL_CONFIG.BLOCK_USERNAMES
    elif data == 'toggle_forwarding':
        GLOBAL_CONFIG.IS_FORWARDING_ACTIVE = not GLOBAL_CONFIG.IS_FORWARDING_ACTIVE
    elif data == 'refresh_config':
        # Reloads config from DB, simulating a fresh start without data loss
        msg = f"**Bot Settings Refresh** safaltapoorvak ho gaya hai. Settings Database se **Reload** ho gayi hain."
        save_config_to_db(GLOBAL_CONFIG) # Ensure the current state is saved before confirmation
        await query.edit_message_text(msg + f"\n\nCurrent Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}", reply_markup=create_back_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END

    if data in ['toggle_block_links', 'toggle_block_usernames', 'toggle_forwarding']:
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text(
            f"**Setting Updated**\n\n"
            f"Current Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END
        
    # --- Conversation Starters ---
    elif data == 'set_source':
        await query.edit_message_text("Kripya Source Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard())
        return SET_SOURCE
    
    elif data == 'set_destination':
        await query.edit_message_text("Kripya Destination Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard())
        return SET_DESTINATION

    # --- Nested Menus ---
    elif data == 'menu_schedule':
        keyboard = [
            [InlineKeyboardButton("0 Sec (Default)", callback_data='set_delay_0'), InlineKeyboardButton("5 Sec", callback_data='set_delay_5')],
            [InlineKeyboardButton("15 Sec", callback_data='set_delay_15'), InlineKeyboardButton("30 Sec", callback_data='set_delay_30')],
            [InlineKeyboardButton("60 Sec (1 Minute)", callback_data='set_delay_60')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text("Message Forward hone se pehle kitna **Delay** chahiye?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
        
    elif data.startswith('set_delay_'):
        delay = int(data.split('_')[2])
        GLOBAL_CONFIG.FORWARD_DELAY_SECONDS = delay
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text(
            f"**Forwarding Delay:** Ab `{delay} seconds` set kar diya gaya hai.\n\n"
            f"Current Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    # --- NEW: Forwarding Mode Menu ---
    elif data == 'menu_forwarding_mode':
        keyboard = [
            [InlineKeyboardButton(f"1. Forward (Original) {'✅' if GLOBAL_CONFIG.FORWARDING_MODE == 'FORWARD' else '❌'}", callback_data='set_mode_forward')],
            [InlineKeyboardButton(f"2. Copy (Caption Editing) {'✅' if GLOBAL_CONFIG.FORWARDING_MODE == 'COPY' else '❌'}", callback_data='set_mode_copy')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text("Message Forwarding ka **Mode** chunein:\n\n*1. Forward*: Message ka format original rehta hai. Yeh tabhi Copy hota hai jab Text Replacement kiya jata hai.\n*2. Copy*: Hamesha Copy hoga, jisse aapka Text Replacement niyam hamesha lagu ho aur original caption bhi hataya ja sake.", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    elif data.startswith('set_mode_'):
        mode = data.split('_')[2].upper()
        GLOBAL_CONFIG.FORWARDING_MODE = mode
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text(
            f"**Forwarding Mode** ab `{mode}` set kar diya gaya hai.\n\n"
            f"Current Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    # --- END NEW: Forwarding Mode Menu ---


    # --- Blacklist/Whitelist/Replacement Menus (Similar structure to be handled in conversation) ---
    elif data == 'menu_blacklist':
        keyboard = [[InlineKeyboardButton("➕ Shabdh Blacklist Karein", callback_data='add_blacklist_word')], [InlineKeyboardButton("🗑️ Saare Blacklist Hatayein", callback_data='clear_blacklist')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]]
        await query.edit_message_text(f"**Word Blacklist Settings**\n\nCurrent Blacklisted Words: {', '.join(GLOBAL_CONFIG.WORD_BLACKLIST or []) or 'Koi nahi'}", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
        
    elif data == 'add_blacklist_word':
        await query.edit_message_text("Kripya vah **Shabdh** bhejein jise aap **Block** karna chahte hain.", reply_markup=create_back_keyboard('menu_blacklist'))
        return SET_BLACKLIST_WORD

    elif data == 'clear_blacklist':
        GLOBAL_CONFIG.WORD_BLACKLIST = []
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text("**Saare Blacklisted Shabdh** hata diye gaye hain.", reply_markup=create_back_keyboard())
        return ConversationHandler.END
        
    # Whitelist Menu (Only for clarity, handlers are similar to blacklist/replacement)
    elif data == 'menu_whitelist':
        keyboard = [[InlineKeyboardButton("➕ Shabdh Whitelist Karein", callback_data='add_whitelist_word')], [InlineKeyboardButton("🗑️ Saare Whitelist Hatayein", callback_data='clear_whitelist')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]]
        await query.edit_message_text(f"**Word Whitelist Settings**\n\nCurrent Whitelisted Words: {', '.join(GLOBAL_CONFIG.WORD_WHITELIST or []) or 'Koi nahi'} (Inka hona jaruri hai)", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    elif data == 'add_whitelist_word':
        await query.edit_message_text("Kripya vah **Shabdh** bhejein jiska Message mein **Hona Jaruri** hai.", reply_markup=create_back_keyboard('menu_whitelist'))
        return SET_WHITELIST_WORD

    elif data == 'clear_whitelist':
        GLOBAL_CONFIG.WORD_WHITELIST = []
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text("**Saare Whitelisted Shabdh** hata diye gaye hain.", reply_markup=create_back_keyboard())
        return ConversationHandler.END
        
    elif data == 'menu_replacement':
        keyboard = [[InlineKeyboardButton("➕ Naya Niyam Jodein", callback_data='add_replacement_find')], [InlineKeyboardButton("🗑️ Saare Niyam Hatayein", callback_data='clear_replacements')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]]
        await query.edit_message_text(f"**Text Replacement Niyam**\n\n{get_current_settings_text(GLOBAL_CONFIG)}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return ConversationHandler.END

    elif data == 'add_replacement_find':
        await query.edit_message_text("Vah **Text Bhejein** jise aap Message mein **Dhoondhna** chahte hain (Find Text).", reply_markup=create_back_keyboard('menu_replacement'))
        return SET_REPLACEMENT_FIND
    
    elif data == 'clear_replacements':
        GLOBAL_CONFIG.TEXT_REPLACEMENTS = {}
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text("**Saare Text Replacement Niyam** hata diye gaye hain.", reply_markup=create_back_keyboard())
        return ConversationHandler.END

    return ConversationHandler.END # End the conversation if the callback is handled

# 8. Conversation Handlers (For User Input)

async def handle_chat_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE, config_attr: str) -> int:
    """Utility function to handle receiving chat ID/Username."""
    global GLOBAL_CONFIG
    # Check against saved Admin ID AND forced Admin ID
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END

    chat_input = update.message.text.strip()
    if not (chat_input.startswith('-100') or chat_input.startswith('@') or chat_input.isdigit()):
         await update.message.reply_text("Galat format! Kripya ID (-100...) ya Username (@...) bhejein.", reply_markup=create_back_keyboard('main_menu'))
         return ConversationHandler.END

    setattr(GLOBAL_CONFIG, config_attr, chat_input)
    save_config_to_db(GLOBAL_CONFIG)
    
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
    """Adds a word to Blacklist or Whitelist."""
    global GLOBAL_CONFIG
    # Check against saved Admin ID AND forced Admin ID
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END
    
    word = update.message.text.strip().lower()
    current_list = getattr(GLOBAL_CONFIG, list_name) or []
    
    if word not in current_list:
        current_list.append(word)
        setattr(GLOBAL_CONFIG, list_name, current_list)
        save_config_to_db(GLOBAL_CONFIG)
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
    """Gets the 'find' text and asks for the 'replace' text."""
    # Check against saved Admin ID AND forced Admin ID
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
    """Gets the 'replace' text and saves the replacement rule."""
    global GLOBAL_CONFIG
    # Check against saved Admin ID AND forced Admin ID
    if GLOBAL_CONFIG.ADMIN_USER_ID is not None and update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID and update.message.chat_id != FORCE_ADMIN_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END

    find_text = context.user_data.pop('find_text')
    replace_text = update.message.text.strip()
    
    replacements = GLOBAL_CONFIG.TEXT_REPLACEMENTS or {}
    replacements[find_text] = replace_text
    GLOBAL_CONFIG.TEXT_REPLACEMENTS = replacements
    save_config_to_db(GLOBAL_CONFIG)
    
    await update.message.reply_text(
        f"**Naya Replacement Niyam** safaltapoorvak set kiya gaya:\n"
        f"**Dhoondhein:** `{find_text}`\n"
        f"**Badlein:** `{replace_text}`\n\n"
        f"Current Settings:\n{get_current_settings_text(GLOBAL_CONFIG)}",
        reply_markup=create_back_keyboard(),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

# 9. Core Forwarding Logic
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks and forwards messages based on configuration."""
    message = update.channel_post or update.message
    config = load_config_from_db() 
    
    if not config.IS_FORWARDING_ACTIVE: return
    source_id_str = config.SOURCE_CHAT_ID
    # Check if the message comes from the set source ID(s)
    if not (source_id_str and str(message.chat.id) in source_id_str): return

    dest_id = config.DESTINATION_CHAT_ID
    if not dest_id:
        if config.ADMIN_USER_ID or FORCE_ADMIN_ID: # Use OR force admin ID for notification
            admin_to_notify = config.ADMIN_USER_ID or FORCE_ADMIN_ID
            await context.bot.send_message(admin_to_notify, f"Source Message aaya, lekin Destination ID set nahi hai!")
        return

    text_to_process = message.text or message.caption or ""
    text_lower = text_to_process.lower()

    # Filters: Links, Usernames
    if config.BLOCK_LINKS and ('http' in text_lower or 't.me' in text_lower): return
    if config.BLOCK_USERNAMES and re.search(r'@[a-zA-Z0-9_]+', text_lower): return

    # Filters: Blacklist
    if config.WORD_BLACKLIST:
        for word in config.WORD_BLACKLIST:
            if word in text_lower: return

    # Filters: Whitelist (MUST contain at least one word)
    if config.WORD_WHITELIST:
        is_whitelisted = False
        for word in config.WORD_WHITELIST:
            if word in text_lower:
                is_whitelisted = True
                break
        if not is_whitelisted: return

    # Text Replacement Logic with modification tracking 
    final_text = text_to_process
    text_modified = False 
    
    if config.TEXT_REPLACEMENTS and final_text:
        for find, replace in config.TEXT_REPLACEMENTS.items():
            if find in final_text:
                final_text = final_text.replace(find, replace)
                text_modified = True
                
    # Apply Delay
    if config.FORWARD_DELAY_SECONDS > 0:
        time.sleep(config.FORWARD_DELAY_SECONDS)

    # --- CORE FORWARDING MODE LOGIC ---
    
    # Decide whether to use copy_message based on 1. Text modification or 2. Explicit 'COPY' mode setting
    force_copy = text_modified or (config.FORWARDING_MODE == 'COPY')
    
    # Check if original message had a parse_mode (using getattr for safety)
    original_parse_mode = getattr(message, 'parse_mode', None)

    # Set final parse mode: Only use original parse mode if mode is FORWARD AND no text modification happened.
    final_parse_mode = None 
    if config.FORWARDING_MODE == 'FORWARD' and not text_modified and original_parse_mode:
        final_parse_mode = original_parse_mode
    
    try:
        if force_copy:
            # --- Case 1: Use copy_message (due to text modification OR 'COPY' mode) ---
            
            # Message is pure text (no media)
            if message.text and not message.caption and not message.photo and not message.video and not message.document:
                if final_text and final_text.strip():
                     await context.bot.send_message(chat_id=dest_id, text=final_text, parse_mode=final_parse_mode, disable_web_page_preview=True)
                # If text became empty, skip.

            # Message has media (photo, video, document, etc.)
            elif message.photo or message.video or message.document or message.audio or message.voice or message.sticker:
                # If final_text is empty, we send media with empty caption (removing original caption)
                # If final_text exists, we send media with new caption
                caption_to_send = final_text if final_text else ""
                await context.bot.copy_message(
                    chat_id=dest_id, 
                    from_chat_id=message.chat.id, 
                    message_id=message.message_id, 
                    caption=caption_to_send, 
                    parse_mode=final_parse_mode
                )
            
        else:
            # --- Case 2: Use forward_message ('FORWARD' mode and no modifications) ---
            await context.bot.forward_message(chat_id=dest_id, from_chat_id=message.chat.id, message_id=message.message_id)

    except Exception as e:
        logger.error(f"Error copying/sending message: {e}")
            
# 10. Main Function 
def main() -> None:
    """Start the bot."""
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
        },
        fallbacks=[
            CallbackQueryHandler(handle_callback),
            CommandHandler("start", start)
        ],
        allow_reentry=True
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Message handler for forwarding logic
    application.add_handler(MessageHandler(filters.ALL, forward_message))
    
    # Webhook Setup for Render/Deployment
    PORT = int(os.environ.get("PORT", "8080")) # Use 8080 as a robust default for web services
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        logger.info(f"Starting bot with Webhook on URL: {WEBHOOK_URL} using port {PORT}")
        application.run_webhook(
            listen="0.0.0.0", # This ensures binding to the host's public IP
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        logger.warning("WEBHOOK_URL not set. Falling back to Polling. (Not recommended for Render Web Services)")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
