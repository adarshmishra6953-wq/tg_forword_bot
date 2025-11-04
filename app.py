import os
import json
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
from sqlalchemy import create_engine, Column, Integer, String, Boolean, Text, PickleType
from sqlalchemy.orm import sessionmaker, declarative_base

# 1. Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
SET_SOURCE, SET_DESTINATION, SET_REPLACEMENT_FIND, SET_REPLACEMENT_REPLACE, SET_BLACKLIST_WORD, SET_WHITELIST_WORD = range(6)

# 2. Database Setup (SQLAlchemy)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///bot_config.db")
# Fix Heroku/Render DATABASE_URL format if needed
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Engine = create_engine(DATABASE_URL)
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# 3. Database Model (Updated with Blacklist/Whitelist)
class BotConfig(Base):
    __tablename__ = 'config'
    id = Column(Integer, primary_key=True)
    
    # Bot Settings (Previous)
    SOURCE_CHAT_ID = Column(String)
    DESTINATION_CHAT_ID = Column(String)
    IS_FORWARDING_ACTIVE = Column(Boolean, default=True)
    BLOCK_LINKS = Column(Boolean, default=False)
    BLOCK_USERNAMES = Column(Boolean, default=False)
    FORWARD_DELAY_SECONDS = Column(Integer, default=0)
    ADMIN_USER_ID = Column(Integer)
    
    # New Filter Settings
    TEXT_REPLACEMENTS = Column(PickleType, default={})
    WORD_BLACKLIST = Column(PickleType, default=[]) # List of words to block
    WORD_WHITELIST = Column(PickleType, default=[]) # List of words that MUST be present

# Create tables if they don't exist
Base.metadata.create_all(Engine)

# 4. Configuration Management Functions
def load_config_from_db():
    """Load configuration from the database. Creates a default entry if none exists."""
    session = Session()
    config = session.query(BotConfig).first()
    if not config:
        config = BotConfig(
            id=1,
            IS_FORWARDING_ACTIVE=True,
            TEXT_REPLACEMENTS={},
            WORD_BLACKLIST=[],
            WORD_WHITELIST=[]
        )
        session.add(config)
        session.commit()
        logger.info("New BotConfig entry created in DB.")
    session.close()
    return config

def save_config_to_db(config):
    """Save the provided configuration object back to the database."""
    session = Session()
    session.merge(config)
    session.commit()
    session.close()

# Global config instance (Loaded on startup)
GLOBAL_CONFIG = load_config_from_db()

# 5. Utility Functions (using GLOBAL_CONFIG)

def get_current_settings_text(config):
    """Returns a formatted string of current bot settings."""
    status = "Shuru" if config.IS_FORWARDING_ACTIVE else "Ruka Hua"
    links = "Haa" if config.BLOCK_LINKS else "Nahi"
    usernames = "Haa" if config.BLOCK_USERNAMES else "Nahi"
    
    replacements_list = "\n".join(
        [f"   - '{f}' -> '{r}'" for f, r in config.TEXT_REPLACEMENTS.items()]
    ) if config.TEXT_REPLACEMENTS else "Koi Niyam Set Nahi"

    blacklist_list = ", ".join(config.WORD_BLACKLIST) if config.WORD_BLACKLIST else "Koi Shabdh Block Nahi"
    whitelist_list = ", ".join(config.WORD_WHITELIST) if config.WORD_WHITELIST else "Koi Shabdh Jaruri Nahi"

    return (
        f"**Bot Status:** `{status}`\n\n"
        f"**Source ID:** `{config.SOURCE_CHAT_ID or 'Set Nahi'}`\n"
        f"**Destination ID:** `{config.DESTINATION_CHAT_ID or 'Set Nahi'}`\n\n"
        f"**Filters:**\n"
        f" - Links Block: `{links}`\n"
        f" - Usernames Block: `{usernames}`\n"
        f" - Forwarding Delay: `{config.FORWARD_DELAY_SECONDS} seconds`\n"
        f" - **Blacklist:** `{blacklist_list}`\n"
        f" - **Whitelist:** `{whitelist_list}`\n\n"
        f"**Text Replacement Rules:**\n{replacements_list}"
    )

def create_main_menu_keyboard(config):
    """Creates the main inline keyboard menu."""
    keyboard = [
        [
            InlineKeyboardButton("➡️ Source Channel Set Karein", callback_data='set_source'),
            InlineKeyboardButton("🎯 Destination Channel Set Karein", callback_data='set_destination')
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
            InlineKeyboardButton("⏸️ Rokein" if config.IS_FORWARDING_ACTIVE else "▶️ Shuru Karein", callback_data='toggle_forwarding'),
            InlineKeyboardButton("🔄 Bot Refresh", callback_data='refresh_config')
        ],
        [InlineKeyboardButton("⚙️ Current Settings Dekhein", callback_data='show_settings')]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_keyboard():
    """Creates a back button keyboard."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]])

# 6. Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /start command and sets the admin user."""
    global GLOBAL_CONFIG
    user_id = update.effective_user.id
    if GLOBAL_CONFIG.ADMIN_USER_ID is None:
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

# 7. Callback Handlers (Inline Buttons) - ONLY NEW/MODIFIED HANDLERS SHOWN

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all Inline Button presses."""
    global GLOBAL_CONFIG
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # Admin Check
    if chat_id != GLOBAL_CONFIG.ADMIN_USER_ID:
        await query.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return

    # Navigation (Ensure main_menu reload is present)
    if data == 'main_menu':
        GLOBAL_CONFIG = load_config_from_db()
        await query.edit_message_text(
            f"**Mukhya Menu (Main Menu)**\n\n{get_current_settings_text(GLOBAL_CONFIG)}",
            reply_markup=create_main_menu_keyboard(GLOBAL_CONFIG),
            parse_mode='Markdown'
        )
        return
    
    # --- Blacklist Menu ---
    elif data == 'menu_blacklist':
        keyboard = [
            [InlineKeyboardButton("➕ Shabdh Blacklist Karein", callback_data='add_blacklist_word')],
            [InlineKeyboardButton("🗑️ Saare Blacklist Hatayein", callback_data='clear_blacklist')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text(
            f"**Word Blacklist Settings**\n\n"
            f"Current Blacklisted Words: {', '.join(GLOBAL_CONFIG.WORD_BLACKLIST) or 'Koi nahi'}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
        
    elif data == 'add_blacklist_word':
        await query.edit_message_text(
            "Kripya vah **Shabdh** bhejein jise aap **Block** karna chahte hain (Ek baar mein ek hi shabdh).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data='menu_blacklist')]])
        )
        return SET_BLACKLIST_WORD

    elif data == 'clear_blacklist':
        GLOBAL_CONFIG.WORD_BLACKLIST = []
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text(
            "**Saare Blacklisted Shabdh** hata diye gaye hain.",
            reply_markup=create_back_keyboard()
        )
        return

    # --- Whitelist Menu ---
    elif data == 'menu_whitelist':
        keyboard = [
            [InlineKeyboardButton("➕ Shabdh Whitelist Karein", callback_data='add_whitelist_word')],
            [InlineKeyboardButton("🗑️ Saare Whitelist Hatayein", callback_data='clear_whitelist')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text(
            f"**Word Whitelist Settings**\n\n"
            f"Current Whitelisted Words: {', '.join(GLOBAL_CONFIG.WORD_WHITELIST) or 'Koi nahi'} (Inka hona jaruri hai)",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif data == 'add_whitelist_word':
        await query.edit_message_text(
            "Kripya vah **Shabdh** bhejein jiska Message mein **Hona Jaruri** hai (Ek baar mein ek hi shabdh).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data='menu_whitelist')]])
        )
        return SET_WHITELIST_WORD

    elif data == 'clear_whitelist':
        GLOBAL_CONFIG.WORD_WHITELIST = []
        save_config_to_db(GLOBAL_CONFIG)
        await query.edit_message_text(
            "**Saare Whitelisted Shabdh** hata diye gaye hain.",
            reply_markup=create_back_keyboard()
        )
        return
    
    # The rest of the handlers (set_source, set_destination, toggle_block_links, etc.) remain the same.

# 8. Conversation Handlers (New)

async def set_blacklist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Adds a word to the blacklist."""
    global GLOBAL_CONFIG
    if update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END
    
    word = update.message.text.strip().lower()
    if word not in GLOBAL_CONFIG.WORD_BLACKLIST:
        GLOBAL_CONFIG.WORD_BLACKLIST.append(word)
        save_config_to_db(GLOBAL_CONFIG)
        msg = f"Shabdh: **'{word}'** safaltapoorvak **Blacklist** mein jod diya gaya hai."
    else:
        msg = f"Shabdh: **'{word}'** pehle se hi **Blacklist** mein hai."

    await update.message.reply_text(
        msg + f"\n\nBlacklist: {', '.join(GLOBAL_CONFIG.WORD_BLACKLIST)}",
        reply_markup=create_back_keyboard(),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def set_whitelist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Adds a word to the whitelist."""
    global GLOBAL_CONFIG
    if update.message.chat_id != GLOBAL_CONFIG.ADMIN_USER_ID:
        await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END
    
    word = update.message.text.strip().lower()
    if word not in GLOBAL_CONFIG.WORD_WHITELIST:
        GLOBAL_CONFIG.WORD_WHITELIST.append(word)
        save_config_to_db(GLOBAL_CONFIG)
        msg = f"Shabdh: **'{word}'** safaltapoorvak **Whitelist** mein jod diya gaya hai. Ab iska hona **jaruri** hai."
    else:
        msg = f"Shabdh: **'{word}'** pehle se hi **Whitelist** mein hai."

    await update.message.reply_text(
        msg + f"\n\nWhitelist: {', '.join(GLOBAL_CONFIG.WORD_WHITELIST)}",
        reply_markup=create_back_keyboard(),
        parse_mode='Markdown'
    )
    return ConversationHandler.END


# 9. Core Forwarding Logic (Modified to include Blacklist/Whitelist checks)

async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks and forwards messages based on configuration."""
    message = update.channel_post or update.message
    config = load_config_from_db() # Load latest config
    
    # ... (Checks 1 & 2 for activity and source ID remain the same)
    if not config.IS_FORWARDING_ACTIVE: return
    source_id_str = config.SOURCE_CHAT_ID
    if not (source_id_str and str(message.chat.id) in source_id_str): return

    dest_id = config.DESTINATION_CHAT_ID
    if not dest_id:
        if config.ADMIN_USER_ID:
            await context.bot.send_message(config.ADMIN_USER_ID, f"Destination ID set nahi hai!")
        return

    # Prepare text for filtering and processing (lower case for comparison)
    text_to_process = message.text or message.caption or ""
    text_lower = text_to_process.lower()

    # --- Check 3: Apply Filters (Links and Usernames) ---
    
    if config.BLOCK_LINKS and ('http' in text_lower or 't.me' in text_lower):
        logger.info("Message blocked: Contains link.")
        return
        
    if config.BLOCK_USERNAMES and re.search(r'@[a-zA-Z0-9_]+', text_lower):
        logger.info("Message blocked: Contains username.")
        return

    # --- NEW CHECK 4: Blacklist Check ---
    if config.WORD_BLACKLIST:
        for word in config.WORD_BLACKLIST:
            if word in text_lower:
                logger.info(f"Message blocked: Contains blacklisted word '{word}'.")
                return

    # --- NEW CHECK 5: Whitelist Check ---
    if config.WORD_WHITELIST:
        is_whitelisted = False
        for word in config.WORD_WHITELIST:
            if word in text_lower:
                is_whitelisted = True
                break
        
        if not is_whitelisted:
            logger.info("Message blocked: Does not contain any whitelisted word.")
            return


    # --- Remaining steps (Text Replacement, Delay, Final Forwarding) remain the same ---

    # Check 6: Apply Text Replacement
    final_text = text_to_process
    if config.TEXT_REPLACEMENTS:
        for find, replace in config.TEXT_REPLACEMENTS.items():
            final_text = final_text.replace(find, replace)

    # Apply Delay/Schedule
    if config.FORWARD_DELAY_SECONDS > 0:
        time.sleep(config.FORWARD_DELAY_SECONDS)

    # Final Forwarding (Use copy_message)
    try:
        if final_text != text_to_process and message.text: # Text message modified
            await context.bot.send_message(chat_id=dest_id, text=final_text, parse_mode=message.parse_mode, disable_web_page_preview=True)
        elif final_text != text_to_process and message.caption: # Media caption modified
             await context.bot.copy_message(chat_id=dest_id, from_chat_id=message.chat.id, message_id=message.message_id, caption=final_text, parse_mode=message.parse_mode)
        else:
            await message.copy(chat_id=dest_id)
    except Exception as e:
        logger.error(f"Error copying message: {e}")
            
# 10. Main Function (Handlers Update)
def main() -> None:
    # ... (Bot Token and Application setup remain the same)
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set. Bot cannot start.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation Handler for Settings Flow (Updated range to include new states)
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback)],
        states={
            SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source_id)],
            SET_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_destination_id)],
            SET_REPLACEMENT_FIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_replacement_find)],
            SET_REPLACEMENT_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_replacement_replace)],
            SET_BLACKLIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_blacklist_word)], # New
            SET_WHITELIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_whitelist_word)], # New
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
    application.add_handler(MessageHandler(filters.ALL, forward_message, channel_post_updates=True))
    
    # ... (Webhook/Polling setup remains the same)
    PORT = int(os.environ.get("PORT", "8443"))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        logger.info(f"Starting bot with Webhook on URL: {WEBHOOK_URL}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        logger.warning("WEBHOOK_URL not set. Falling back to Polling.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
