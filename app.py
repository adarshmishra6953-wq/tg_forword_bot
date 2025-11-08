import os
import logging
import time
import re
from datetime import datetime, time as time_obj
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
# FIX 1: Correctly import ParseMode
from telegram.constants import ParseMode 
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, Text
from sqlalchemy.orm import sessionmaker, declarative_base
# FIX 2: Correctly import exceptions for newer SQLAlchemy versions
from sqlalchemy.exc import OperationalError, ObjectNotExecutableError
from sqlalchemy.orm.exc import DetachedInstanceError


# 1. Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
(
    SELECT_RULE, 
    NEW_RULE_SET_SOURCE, 
    NEW_RULE_SET_DESTINATION, 
    EDIT_RULE_SELECT,
    EDIT_RULE_SET_REPLACEMENT_FIND, 
    EDIT_RULE_SET_REPLACEMENT_REPLACE, 
    EDIT_RULE_SET_BLACKLIST_WORD, 
    EDIT_RULE_SET_WHITELIST_WORD,
    SET_GLOBAL_HEADER,
    SET_GLOBAL_FOOTER
) = range(10)

# 2. Database Setup (SQLAlchemy)
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL environment variable is not set. Bot will not save settings.")

# Standardize postgresql:// for SQLAlchemy 2.0+ compatibility
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# 3. Database Models
class GlobalConfig(Base):
    """Stores bot-wide settings."""
    __tablename__ = 'global_config'
    id = Column(Integer, primary_key=True)
    ADMIN_USER_ID = Column(Integer)
    GLOBAL_HEADER = Column(Text, default='')
    GLOBAL_FOOTER = Column(Text, default='')
    SCHEDULE_ACTIVE = Column(Boolean, default=False)
    SLEEP_START_HOUR = Column(Integer, default=0) # 0 = 12 AM
    SLEEP_END_HOUR = Column(Integer, default=6)   # 6 = 6 AM

class ForwardingRule(Base):
    """Stores settings for a single Source-Destination pair."""
    __tablename__ = 'forwarding_rules'
    id = Column(Integer, primary_key=True)
    
    SOURCE_CHAT_ID = Column(String)
    DESTINATION_CHAT_ID = Column(String)
    IS_ACTIVE = Column(Boolean, default=True)
    BLOCK_LINKS = Column(Boolean, default=False)
    BLOCK_USERNAMES = Column(Boolean, default=False)
    FORWARD_DELAY_SECONDS = Column(Integer, default=0)
    
    FORWARDING_MODE = Column(String, default='FORWARD') 
    
    TEXT_REPLACEMENTS = Column(PickleType, default={})
    WORD_BLACKLIST = Column(PickleType, default=[]) 
    WORD_WHITELIST = Column(PickleType, default=[])

# Create tables if Engine is available
if Engine:
    try:
        Base.metadata.create_all(Engine)
        logger.info("Database tables created/recreated successfully.")
    except OperationalError as e:
        logger.error(f"Database connection error during table creation: {e}")

# Example: Use your actual Telegram User ID here to force admin status
FORCE_ADMIN_ID = 1695450646 

# 4. Configuration Management Functions
def get_fresh_rule_or_config(model, entity_id=1):
    """Loads a fresh, detached entity (Rule or GlobalConfig) from DB."""
    if not Engine: 
        # Fallback if DB is not set up
        return GlobalConfig(id=1, ADMIN_USER_ID=None) if model == GlobalConfig else None
        
    session = Session()
    entity = None
    try:
        if model == GlobalConfig:
            entity = session.query(GlobalConfig).filter(GlobalConfig.id == entity_id).first()
            if not entity:
                # Create default config if none exists
                entity = GlobalConfig(id=1)
                session.add(entity)
                session.commit()
                # Reload the created entity right after commit
                entity = session.query(GlobalConfig).filter(GlobalConfig.id == entity_id).first()
                logger.info("New GlobalConfig entry created in DB.")
            
        elif model == ForwardingRule:
            entity = session.query(ForwardingRule).filter(ForwardingRule.id == entity_id).first()
            
        if entity:
            # CRITICAL FIX: Always detach the object using expunge before returning
            session.expunge(entity) 
            return entity
            
        # Fallback for ForwardingRule if ID exists but object not found 
        if not entity and model == ForwardingRule and entity_id:
             return None 
             
        # Fallback for GlobalConfig if something went completely wrong
        if model == GlobalConfig:
             # Return a default detached instance
             return GlobalConfig(id=1, ADMIN_USER_ID=None)
             
        return entity
        
    except Exception as e:
        # Catch all errors (including ObjectNotExecutableError) and log them
        logger.error(f"Error loading fresh entity {model.__name__} ID {entity_id} during operation: {e}")
        # Return a safe fallback object
        return GlobalConfig(id=1, ADMIN_USER_ID=None) if model == GlobalConfig else None
    finally:
        # CRITICAL FIX: Ensure session is always closed
        if session:
            session.close()

def load_global_config_from_db():
    """Load global configuration from DB."""
    # Returns a fresh, detached object (GlobalConfig)
    return get_fresh_rule_or_config(GlobalConfig)

def save_global_config_to_db(config):
    """Save the provided detached global configuration object back to the database."""
    if not Engine: return
    session = Session()
    try:
        # CRITICAL FIX: Use merge to handle the detached instance
        session.merge(config)
        session.commit()
    except Exception as e:
        logger.error(f"Error saving global config to DB: {e}")
    finally:
        if session:
            session.close()

def get_all_rules():
    """Fetches all forwarding rules."""
    if not Engine: return []
    session = Session()
    try:
        rules = session.query(ForwardingRule).all()
        # Detach objects from session before returning
        [session.expunge(rule) for rule in rules]
        return rules
    except Exception as e:
        logger.error(f"Error getting all rules: {e}")
        return []
    finally:
        if session:
            session.close()

def get_rule_by_id(rule_id):
    """Fetches a single rule by its ID."""
    # Returns a fresh, detached object (ForwardingRule)
    return get_fresh_rule_or_config(ForwardingRule, rule_id)

def save_rule_to_db(rule):
    """Saves or updates a single rule (which is detached)."""
    if not Engine: return
    session = Session()
    try:
        # CRITICAL FIX: Use merge to handle both new rules (no ID) and updates (with ID, including detached)
        merged_rule = session.merge(rule)
        session.flush() # Get the ID for new rule
        session.commit()
        
        # Update original rule object ID if it was new
        if hasattr(rule, 'id') and rule.id is None:
            rule.id = merged_rule.id
            
    except Exception as e:
        logger.error(f"Error saving rule to DB: {e}")
    finally:
        if session:
            session.close()

def delete_rule_from_db(rule_id):
    """Deletes a rule by its ID."""
    if not Engine: return
    session = Session()
    try:
        # We query inside this function to attach it to the current session before deleting
        rule = session.query(ForwardingRule).filter(ForwardingRule.id == rule_id).first()
        if rule:
            session.delete(rule)
            session.commit()
    except Exception as e:
        logger.error(f"Error deleting rule from DB: {e}")
    finally:
        if session:
            session.close()

# Global config instance (Loaded on startup, used for display/initial check only)
GLOBAL_CONFIG_INITIAL = load_global_config_from_db() 

# 5. Utility Functions (Inline Keyboard and Text formatting)
def get_rule_settings_text(rule):
    """Returns formatted string of a specific rule's settings."""
    if not rule: return "**Rule Maujood Nahi**"
        
    status = "Shuru" if rule.IS_ACTIVE else "Ruka Hua"
    links = "Haa" if rule.BLOCK_LINKS else "Nahi"
    usernames = "Haa" if rule.BLOCK_USERNAMES else "Nahi"
    mode_text = "Forward (Original)" if rule.FORWARDING_MODE == 'FORWARD' else "Copy (Caption Edit Possible)"

    replacements_list = "\n".join(
        [f"   - '{f}' -> '{r}'" for f, r in (rule.TEXT_REPLACEMENTS or {}).items()]
    ) if (rule.TEXT_REPLACEMENTS and len(rule.TEXT_REPLACEMENTS) > 0) else "Koi Niyam Set Nahi"

    blacklist_list = ", ".join(rule.WORD_BLACKLIST or []) if (rule.WORD_BLACKLIST and len(rule.WORD_BLACKLIST) > 0) else "Koi Shabdh Block Nahi"
    whitelist_list = ", ".join(rule.WORD_WHITELIST or []) if (rule.WORD_WHITELIST and len(rule.WORD_WHITELIST) > 0) else "Koi Shabdh Jaruri Nahi"

    return (
        f"**Rule ID:** `{rule.id}`\n"
        f"**Status:** `{status}`\n"
        f"**Mode:** `{mode_text}`\n\n"
        f"**Source ID:** `{rule.SOURCE_CHAT_ID or 'Set Nahi'}`\n"
        f"**Destination ID:** `{rule.DESTINATION_CHAT_ID or 'Set Nahi'}`\n\n"
        f"**Filters:**\n"
        f" - Links Block: `{links}`\n"
        f" - Usernames Block: `{usernames}`\n"
        f" - Delay: `{rule.FORWARD_DELAY_SECONDS} seconds`\n"
        f" - Blacklist: `{blacklist_list}`\n"
        f" - Whitelist: `{whitelist_list}`\n\n"
        f"**Replacements:**\n{replacements_list}"
    )

def get_global_settings_text(config):
    """Returns formatted string of global bot settings."""
    schedule_status = "✅ Active" if config.SCHEDULE_ACTIVE else "❌ Inactive"
    
    return (
        f"**Admin User ID:** `{config.ADMIN_USER_ID or 'Set Nahi'}`\n"
        f"**Global Header:** `{'Set' if config.GLOBAL_HEADER else 'Nahi'}`\n"
        f"**Global Footer:** `{'Set' if config.GLOBAL_FOOTER else 'Nahi'}`\n\n"
        f"**Scheduled Sleep:** `{schedule_status}`\n"
        f" - Time: `{config.SLEEP_START_HOUR:02d}:00` to `{config.SLEEP_END_HOUR:02d}:00`"
    )
    
def create_main_menu_keyboard():
    """Creates the main inline keyboard menu."""
    keyboard = [
        [InlineKeyboardButton("➕ Naya Rule Jodein", callback_data='new_rule')],
        [InlineKeyboardButton("📝 Rules Manage Karein", callback_data='manage_rules')],
        [InlineKeyboardButton("⚙️ Global Settings", callback_data='menu_global_settings')],
        # Restart Button
        [InlineKeyboardButton("🔄 Restart Bot (Reload Config)", callback_data='restart_bot_command')],
    ]
    return InlineKeyboardMarkup(keyboard)

def create_manage_rules_keyboard(rules):
    """Creates a keyboard to select rules for editing."""
    keyboard = []
    if rules:
        for rule in rules:
            status = '✅' if rule.IS_ACTIVE else '⏸️'
            keyboard.append([InlineKeyboardButton(f"{status} Rule {rule.id} | {rule.SOURCE_CHAT_ID or 'S-Set Nahi'} -> {rule.DESTINATION_CHAT_ID or 'D-Set Nahi'}", callback_data=f'edit_rule_{rule.id}')])
    else:
        keyboard.append([InlineKeyboardButton("Koi Rule Maujood Nahi", callback_data='no_rules')])
        
    keyboard.append([InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

def create_rule_edit_keyboard(rule):
    """Creates the menu for editing a specific rule."""
    if not rule: return create_back_keyboard('manage_rules') # Safety check
    
    keyboard = [
        [
            InlineKeyboardButton(f"Source ID: {rule.SOURCE_CHAT_ID or 'SET KAREIN'}", callback_data=f'edit_source_{rule.id}'),
            InlineKeyboardButton(f"Dest ID: {rule.DESTINATION_CHAT_ID or 'SET KAREIN'}", callback_data=f'edit_destination_{rule.id}')
        ],
        [
            InlineKeyboardButton(f"Links Block ({'✅' if rule.BLOCK_LINKS else '❌'})", callback_data=f'toggle_block_links_{rule.id}'),
            InlineKeyboardButton(f"Usernames Block ({'✅' if rule.BLOCK_USERNAMES else '❌'})", callback_data=f'toggle_block_usernames_{rule.id}')
        ],
        [
            InlineKeyboardButton("📝 Replacement Niyam", callback_data=f'menu_replacement_{rule.id}'),
            InlineKeyboardButton(f"⏰ Delay ({rule.FORWARD_DELAY_SECONDS}s)", callback_data=f'menu_schedule_{rule.id}')
        ],
        [
            InlineKeyboardButton("⛔️ Blacklist", callback_data=f'menu_blacklist_{rule.id}'),
            InlineKeyboardButton("✅ Whitelist", callback_data=f'menu_whitelist_{rule.id}')
        ],
        [
            InlineKeyboardButton(f"📨 Mode: {rule.FORWARDING_MODE}", callback_data=f'menu_forwarding_mode_{rule.id}')
        ],
        [
            InlineKeyboardButton("⏸️ Rokein" if rule.IS_ACTIVE else "▶️ Shuru Karein", callback_data=f'toggle_active_{rule.id}'),
            InlineKeyboardButton("🗑️ Rule Hatayein", callback_data=f'delete_rule_{rule.id}')
        ],
        [InlineKeyboardButton("⬅️ Rules List", callback_data='manage_rules')]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_keyboard(callback_data='main_menu'):
    """Creates a back button keyboard."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data=callback_data)]])


# 6. Admin Check Utility
def is_admin(user_id):
    """Checks if the user is the admin or force admin."""
    # Always load a fresh config for the admin check
    current_config = load_global_config_from_db()
    
    return (current_config.ADMIN_USER_ID is not None and user_id == current_config.ADMIN_USER_ID) or (FORCE_ADMIN_ID and user_id == FORCE_ADMIN_ID)

# 7. Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /start command and sets the admin user."""
    
    user_id = update.effective_user.id
    
    # Reload config and set Admin ID
    current_config = load_global_config_from_db() # Reload fresh config
    
    if FORCE_ADMIN_ID and current_config.ADMIN_USER_ID != FORCE_ADMIN_ID:
        current_config.ADMIN_USER_ID = FORCE_ADMIN_ID
        save_global_config_to_db(current_config)
        logger.info(f"Admin User ID forcibly set to: {FORCE_ADMIN_ID}")
    
    elif current_config.ADMIN_USER_ID is None:
        current_config.ADMIN_USER_ID = user_id
        save_global_config_to_db(current_config)
        logger.info(f"Admin User ID set to: {user_id}")
    
    # Check if we are responding to a message or a callback/command
    if update.callback_query:
         # FIX: Wrap in try/except to avoid 'Message is not modified' on double click/re-entry
         try:
             await update.callback_query.edit_message_text(
                f"Namaste! Aapka Telegram Auto-Forward Bot shuru ho gaya hai.\n\n"
                f"**Global Settings:**\n{get_global_settings_text(current_config)}",
                reply_markup=create_main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
         except Exception as e:
            if "Message is not modified" in str(e):
                await update.callback_query.answer("Menu Reload Ho Gaya Hai.")
            else:
                raise
                
    elif update.message:
        await update.message.reply_text(
            f"Namaste! Aapka Telegram Auto-Forward Bot shuru ho gaya hai.\n\n"
            f"**Global Settings:**\n{get_global_settings_text(current_config)}",
            reply_markup=create_main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    return ConversationHandler.END

# Restart Handler
async def restart_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /restart command or button click."""
    
    # Check admin before performing action
    user_id = update.effective_user.id
    if not is_admin(user_id):
        if update.callback_query:
            await update.callback_query.answer("Aap Bot ke Admin nahi hain.")
        elif update.message:
             await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END

    if update.callback_query:
        # A simple answer is enough before calling start (which handles edit_message_text)
        await update.callback_query.answer("Bot Configuration Reload Ho Raha Hai...")
    elif update.message:
        await update.message.reply_text("Bot Configuration Reload Ho Raha Hai...")

    # Call start to reload config and display menu
    # The start function handles the final edit_message_text/reply_text and returns ConversationHandler.END
    return await start(update, context)


# 8. Callback Handlers (For Inline Buttons)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all Inline Button presses and transitions conversations."""
    
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Admin Check
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return ConversationHandler.END # IMPORTANT: End the conversation if not admin
        
    # FIX: Always load a fresh config for displaying/modifying global settings
    current_config = load_global_config_from_db() 

    # --- Restart Handler ---
    if data == 'restart_bot_command':
        # restart_bot_command calls start and returns ConversationHandler.END
        return await restart_bot_command(update, context)

    # --- Global Menus ---
    if data == 'main_menu':
        # FIX: Wrap in try/except to avoid 'Message is not modified'
        try:
            await query.edit_message_text(
                f"**Mukhya Menu (Main Menu)**\n\n**Global Settings:**\n{get_global_settings_text(current_config)}",
                reply_markup=create_main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                await query.answer("Menu Reload Ho Gaya Hai.") 
            else:
                raise
        return ConversationHandler.END

    elif data == 'manage_rules':
        rules = get_all_rules()
        try:
            await query.edit_message_text(
                f"**Forwarding Rules Manage Karein**\n\nAapke pass kul {len(rules)} Rules hain.",
                reply_markup=create_manage_rules_keyboard(rules)
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                await query.answer("Rules List Reload Ho Gayi Hai.") 
            else:
                raise
        return ConversationHandler.END
        
    elif data == 'new_rule':
        context.user_data['new_rule'] = ForwardingRule() 
        await query.edit_message_text("Kripya **Naye Rule** ke liye Source Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard('manage_rules'))
        return NEW_RULE_SET_SOURCE
        
    # --- Rule Edit/Delete ---
    elif data.startswith('edit_rule_'):
        rule_id = int(data.split('_')[2])
        # FIX: Always get fresh rule object for editing
        rule = get_rule_by_id(rule_id) 
        context.user_data['current_rule_id'] = rule_id 
        if not rule:
            await query.edit_message_text("Rule maujood nahi hai.", reply_markup=create_back_keyboard('manage_rules'))
            return ConversationHandler.END
            
        # FIX: Wrap in try/except to avoid 'Message is not modified'
        try:
            await query.edit_message_text(
                f"**Rule {rule_id} Edit Karein**\n\n{get_rule_settings_text(rule)}",
                reply_markup=create_rule_edit_keyboard(rule),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                await query.answer("Rule Menu Reload Ho Gaya Hai.") 
            else:
                raise
        return ConversationHandler.END
        
    elif data.startswith('delete_rule_'):
        rule_id = int(data.split('_')[2])
        delete_rule_from_db(rule_id)
        rules = get_all_rules()
        await query.edit_message_text(
            f"**Rule {rule_id}** safaltapoorvak **Hata Diya Gaya** hai.\n\nAapke pass kul {len(rules)} Rules hain.",
            reply_markup=create_manage_rules_keyboard(rules)
        )
        return ConversationHandler.END # Deleted, so Conversation must END
        
    # --- Global Settings Menu ---
    elif data == 'menu_global_settings':
        keyboard = [
            [InlineKeyboardButton("⬆️ Global Header Set", callback_data='set_global_header')],
            [InlineKeyboardButton("⬇️ Global Footer Set", callback_data='set_global_footer')],
            [InlineKeyboardButton(f"⏰ Schedule Sleep ({'✅' if current_config.SCHEDULE_ACTIVE else '❌'})", callback_data='toggle_schedule_active')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        await query.edit_message_text(
            f"**Global Settings**\n\n{get_global_settings_text(current_config)}\n\n"
            f"Header Sample: `{'Set' if current_config.GLOBAL_HEADER else 'Nahi'}`\n"
            f"Footer Sample: `{'Set' if current_config.GLOBAL_FOOTER else 'Nahi'}`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END # Navigating to a menu, end the conversation flow if not expecting text input

    elif data == 'set_global_header':
        await query.edit_message_text("Kripya vah **Text** bhejein jo **har message ke shuru** mein jodein. (Markup support karta hai)", reply_markup=create_back_keyboard('menu_global_settings'))
        return SET_GLOBAL_HEADER # Await text input

    elif data == 'set_global_footer':
        await query.edit_message_text("Kripya vah **Text** bhejein jo **har message ke ant** mein jodein. (Markup support karta hai)", reply_markup=create_back_keyboard('menu_global_settings'))
        return SET_GLOBAL_FOOTER # Await text input

    elif data == 'toggle_schedule_active':
        # Modify and save the fresh config
        current_config.SCHEDULE_ACTIVE = not current_config.SCHEDULE_ACTIVE
        save_global_config_to_db(current_config)
        
        await query.edit_message_text(
            f"**Scheduled Sleep** ab **{'Shuru' if current_config.SCHEDULE_ACTIVE else 'Ruka Hua'}** hai.\n\n"
            f"Current Schedule: {current_config.SLEEP_START_HOUR:02d}:00 to {current_config.SLEEP_END_HOUR:02d}:00",
            reply_markup=create_back_keyboard('menu_global_settings'),
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END # Setting toggle, Conversation must END


    # --- Rule Toggles (Toggles the specific rule's attribute) ---
    parts = data.split('_')
    if len(parts) > 2 and parts[-1].isdigit():
        rule_id = int(parts[-1])
        # MUST load fresh rule from DB just before modifying
        rule = get_rule_by_id(rule_id) 
        if not rule:
            await query.edit_message_text("Rule ID galat hai.", reply_markup=create_back_keyboard('manage_rules'))
            return ConversationHandler.END
            
        action = parts[0] + '_' + parts[1] # e.g., 'toggle_active'

        if action == 'toggle_active': rule.IS_ACTIVE = not rule.IS_ACTIVE
        elif action == 'toggle_block_links': rule.BLOCK_LINKS = not rule.BLOCK_LINKS
        elif action == 'toggle_block_usernames': rule.BLOCK_USERNAMES = not rule.BLOCK_USERNAMES
        
        # Save the detached rule object back to DB (merge handles attachment)
        save_rule_to_db(rule)
        
        # Go back to rule edit menu, reload rule for fresh display data
        rule_after_save = get_rule_by_id(rule_id) 
        
        # --- CRITICAL FIX: Wrap in try/except to avoid 'Message is not modified' ---
        try:
            await query.edit_message_text(
                f"**Rule {rule_id} Setting Updated**\n\n{get_rule_settings_text(rule_after_save)}",
                reply_markup=create_rule_edit_keyboard(rule_after_save),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            # Catch the specific error that happens when the message content hasn't changed
            if "Message is not modified" in str(e):
                # Answer the query with a notification popup to confirm success
                await query.answer("Status/Setting Badal Diya Gaya Hai.") 
            else:
                 # Re-raise any other unexpected error
                logger.error(f"Error editing message for rule {rule_id} toggle: {e}")
                raise
        
        # ✅ FIX: Toggles ke baad conversation ko END karna zaroori hai
        return ConversationHandler.END 

    # --- Rule Nested Menus ---
    elif data.startswith('menu_schedule_'):
        rule_id = int(data.split('_')[2])
        context.user_data['current_rule_id'] = rule_id
        rule = get_rule_by_id(rule_id) 
        keyboard = [
            [InlineKeyboardButton("0 Sec (Default)", callback_data=f'set_delay_0_{rule_id}'), InlineKeyboardButton("5 Sec", callback_data=f'set_delay_5_{rule_id}')],
            [InlineKeyboardButton("15 Sec", callback_data=f'set_delay_15_{rule_id}'), InlineKeyboardButton("30 Sec", callback_data=f'set_delay_30_{rule_id}')],
            [InlineKeyboardButton("60 Sec (1 Minute)", callback_data=f'set_delay_60_{rule_id}')],
            [InlineKeyboardButton("⬅️ Rule Edit", callback_data=f'edit_rule_{rule_id}')]
        ]
        await query.edit_message_text(f"Rule **{rule_id}** ke liye Delay chunein. Current: {rule.FORWARD_DELAY_SECONDS}s", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
        
    elif data.startswith('set_delay_'):
        parts = data.split('_')
        delay = int(parts[2])
        rule_id = int(parts[3])
        rule = get_rule_by_id(rule_id) 
        rule.FORWARD_DELAY_SECONDS = delay
        save_rule_to_db(rule)
        await query.edit_message_text(
            f"**Rule {rule_id} Delay:** Ab `{delay} seconds` set kar diya gaya hai.",
            reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'),
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END # Setting delay, Conversation must END

    # Forwarding Mode Menu (Rule Specific)
    elif data.startswith('menu_forwarding_mode_'):
        rule_id = int(data.split('_')[3])
        rule = get_rule_by_id(rule_id) 
        keyboard = [
            [InlineKeyboardButton(f"1. Forward (Original) {'✅' if rule.FORWARDING_MODE == 'FORWARD' else '❌'}", callback_data=f'set_mode_forward_{rule_id}')],
            [InlineKeyboardButton(f"2. Copy (Caption Editing) {'✅' if rule.FORWARDING_MODE == 'COPY' else '❌'}", callback_data=f'set_mode_copy_{rule_id}')],
            [InlineKeyboardButton("⬅️ Rule Edit", callback_data=f'edit_rule_{rule_id}')]
        ]
        await query.edit_message_text("Message Forwarding ka **Mode** chunein:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    elif data.startswith('set_mode_'):
        parts = data.split('_')
        mode = parts[2].upper()
        rule_id = int(parts[3])
        rule = get_rule_by_id(rule_id) 
        rule.FORWARDING_MODE = mode
        save_rule_to_db(rule)
        await query.edit_message_text(
            f"**Rule {rule_id} Forwarding Mode** ab `{mode}` set kar diya gaya hai.",
            reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'),
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END # Setting mode, Conversation must END


    # Rule List/Replacement Menus (Similar structure)
    elif data.startswith('menu_blacklist_'):
        rule_id = int(data.split('_')[2])
        context.user_data['current_rule_id'] = rule_id
        rule = get_rule_by_id(rule_id) 
        keyboard = [[InlineKeyboardButton("➕ Shabdh Blacklist Karein", callback_data=f'add_blacklist_word_{rule_id}')], [InlineKeyboardButton("🗑️ Saare Blacklist Hatayein", callback_data=f'clear_blacklist_{rule_id}')], [InlineKeyboardButton("⬅️ Rule Edit", callback_data=f'edit_rule_{rule_id}')]]
        await query.edit_message_text(f"**Rule {rule_id} Blacklist Settings**\n\nCurrent Blacklisted Words: {', '.join(rule.WORD_BLACKLIST or []) or 'Koi nahi'}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END # Navigating to a menu, end the conversation flow if not expecting text input
        
    elif data.startswith('add_blacklist_word_'):
        rule_id = int(data.split('_')[3])
        context.user_data['current_rule_id'] = rule_id
        await query.edit_message_text("Kripya vah **Shabdh** bhejein jise aap **Block** karna chahte hain.", reply_markup=create_back_keyboard(f'menu_blacklist_{rule_id}'))
        return EDIT_RULE_SET_BLACKLIST_WORD

    elif data.startswith('clear_blacklist_'):
        rule_id = int(data.split('_')[2])
        rule = get_rule_by_id(rule_id) 
        rule.WORD_BLACKLIST = []
        save_rule_to_db(rule)
        await query.edit_message_text(f"**Rule {rule_id}** ke **Saare Blacklisted Shabdh** hata diye gaye hain.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
        return ConversationHandler.END # Clearing list, Conversation must END
        
    # Whitelist Menu (Rule Specific)
    elif data.startswith('menu_whitelist_'):
        rule_id = int(data.split('_')[2])
        context.user_data['current_rule_id'] = rule_id
        rule = get_rule_by_id(rule_id) 
        keyboard = [[InlineKeyboardButton("➕ Shabdh Whitelist Karein", callback_data=f'add_whitelist_word_{rule_id}')], [InlineKeyboardButton("🗑️ Saare Whitelist Hatayein", callback_data=f'clear_whitelist_{rule_id}')], [InlineKeyboardButton("⬅️ Rule Edit", callback_data=f'edit_rule_{rule_id}')]]
        await query.edit_message_text(f"**Rule {rule_id} Whitelist Settings**\n\nCurrent Whitelisted Words: {', '.join(rule.WORD_WHITELIST or []) or 'Koi nahi'} (Inka hona jaruri hai)", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END # Navigating to a menu, end the conversation flow if not expecting text input

    elif data.startswith('add_whitelist_word_'):
        rule_id = int(data.split('_')[3])
        context.user_data['current_rule_id'] = rule_id
        await query.edit_message_text("Kripya vah **Shabdh** bhejein jiska Message mein **Hona Jaruri** hai.", reply_markup=create_back_keyboard(f'menu_whitelist_{rule_id}'))
        return EDIT_RULE_SET_WHITELIST_WORD

    elif data.startswith('clear_whitelist_'):
        rule_id = int(data.split('_')[2])
        rule = get_rule_by_id(rule_id) 
        rule.WORD_WHITELIST = []
        save_rule_to_db(rule)
        await query.edit_message_text(f"**Rule {rule_id}** ke **Saare Whitelisted Shabdh** hata diye gaye hain.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
        return ConversationHandler.END # Clearing list, Conversation must END
        
    # Replacement Menu (Rule Specific)
    elif data.startswith('menu_replacement_'):
        rule_id = int(data.split('_')[2])
        context.user_data['current_rule_id'] = rule_id
        rule = get_rule_by_id(rule_id) 
        keyboard = [[InlineKeyboardButton("➕ Naya Niyam Jodein", callback_data=f'add_replacement_find_{rule_id}')], [InlineKeyboardButton("🗑️ Saare Niyam Hatayein", callback_data=f'clear_replacements_{rule_id}')], [InlineKeyboardButton("⬅️ Rule Edit", callback_data=f'edit_rule_{rule_id}')]]
        await query.edit_message_text(f"**Rule {rule_id} Text Replacement Niyam**\n\n{get_rule_settings_text(rule)}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END # Navigating to a menu, end the conversation flow if not expecting text input

    elif data.startswith('add_replacement_find_'):
        rule_id = int(data.split('_')[3])
        context.user_data['current_rule_id'] = rule_id
        await query.edit_message_text("Vah **Text Bhejein** jise aap Message mein **Dhoondhna** chahte hain (Find Text).", reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}'))
        return EDIT_RULE_SET_REPLACEMENT_FIND
    
    elif data.startswith('clear_replacements_'):
        rule_id = int(data.split('_')[2])
        rule = get_rule_by_id(rule_id) 
        rule.TEXT_REPLACEMENTS = {}
        save_rule_to_db(rule)
        await query.edit_message_text(f"**Rule {rule_id}** ke **Saare Replacement Niyam** hata diye gaye hain.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
        return ConversationHandler.END # Clearing list, Conversation must END

    return ConversationHandler.END # Default fallback for any unhandled callback should END the conversation


# 9. Conversation Handlers (For User Input)

async def handle_chat_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE, config_attr: str, new_rule: bool) -> int:
    """Utility function to handle receiving chat ID/Username for a rule."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END

    chat_input = update.message.text.strip()
    
    # Validation
    if not (chat_input.startswith('-100') or chat_input.startswith('@') or chat_input.isdigit()):
         fallback_data = 'manage_rules' if new_rule else f'edit_rule_{context.user_data.get("current_rule_id")}'
         await update.message.reply_text("Galat format! Kripya ID (-100...) ya Username (@...) bhejein.", reply_markup=create_back_keyboard(fallback_data))
         return ConversationHandler.END

    if new_rule:
        # For new rule creation
        new_rule_obj = context.user_data.get('new_rule')
        if not new_rule_obj:
            new_rule_obj = ForwardingRule()
            context.user_data['new_rule'] = new_rule_obj 
            
        setattr(new_rule_obj, config_attr, chat_input)
        
        if config_attr == 'SOURCE_CHAT_ID':
            await update.message.reply_text("Ab Destination Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard('manage_rules'))
            return NEW_RULE_SET_DESTINATION
        
        elif config_attr == 'DESTINATION_CHAT_ID':
            save_rule_to_db(new_rule_obj)
            context.user_data.pop('new_rule', None)
            
            await update.message.reply_text(
                f"**Naya Rule** safaltapoorvak ban gaya hai (ID: `{new_rule_obj.id}`).\n"
                f"Source: `{new_rule_obj.SOURCE_CHAT_ID}`\n"
                f"Destination: `{new_rule_obj.DESTINATION_CHAT_ID}`",
                reply_markup=create_back_keyboard('manage_rules'),
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END
    
    else:
        # For editing existing rule
        rule_id = context.user_data.get('current_rule_id')
        rule = get_rule_by_id(rule_id) 
        if not rule:
            await update.message.reply_text("Rule ID lost/galat hai.", reply_markup=create_back_keyboard('manage_rules'))
            return ConversationHandler.END

        setattr(rule, config_attr, chat_input)
        save_rule_to_db(rule)
        
        await update.message.reply_text(
            f"**Rule {rule_id} {config_attr.replace('_', ' ')}** safaltapoorvak `{chat_input}` set kar diya gaya hai.",
            reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'),
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

async def set_new_rule_source_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_chat_id_input(update, context, "SOURCE_CHAT_ID", new_rule=True)

async def set_new_rule_destination_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_chat_id_input(update, context, "DESTINATION_CHAT_ID", new_rule=True)

# Global Header/Footer Handlers
async def set_global_header(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    current_config = load_global_config_from_db() 
    current_config.GLOBAL_HEADER = update.message.text
    save_global_config_to_db(current_config)
    
    await update.message.reply_text(
        f"**Global Header** safaltapoorvak set kiya gaya:\n`{update.message.text}`",
        reply_markup=create_back_keyboard('menu_global_settings'),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def set_global_footer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    current_config = load_global_config_from_db() 
    current_config.GLOBAL_FOOTER = update.message.text
    save_global_config_to_db(current_config)
    
    await update.message.reply_text(
        f"**Global Footer** safaltapoorvak set kiya gaya:\n`{update.message.text}`",
        reply_markup=create_back_keyboard('menu_global_settings'),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


# Rule List/Replacement Input Handlers 
async def set_rule_list_word(update: Update, context: ContextTypes.DEFAULT_TYPE, list_name: str, fallback_menu: str) -> int:
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    rule_id = context.user_data.get('current_rule_id')
    rule = get_rule_by_id(rule_id) 
    if not rule:
        await update.message.reply_text("Rule ID lost/galat hai.", reply_markup=create_back_keyboard('manage_rules'))
        return ConversationHandler.END
        
    word = update.message.text.strip().lower()
    current_list = getattr(rule, list_name) or []
    
    if word not in current_list:
        current_list.append(word)
        setattr(rule, list_name, current_list)
        save_rule_to_db(rule)
        msg = f"Rule {rule_id} - Shabdh: **'{word}'** safaltapoorvak **{list_name.split('_')[1]}** mein jod diya gaya hai."
    else:
        msg = f"Rule {rule_id} - Shabdh: **'{word}'** pehle se hi **{list_name.split('_')[1]}** mein hai."

    await update.message.reply_text(
        msg + f"\n\n{list_name.split('_')[1]}: {', '.join(current_list)}",
        reply_markup=create_back_keyboard(fallback_menu.replace('_0', f'_{rule_id}')),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def set_rule_blacklist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_rule_list_word(update, context, "WORD_BLACKLIST", 'menu_blacklist_0')

async def set_rule_whitelist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_rule_list_word(update, context, "WORD_WHITELIST", 'menu_whitelist_0')

async def set_rule_replacement_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    rule_id = context.user_data.get('current_rule_id')
    context.user_data['find_text'] = update.message.text.strip()
    await update.message.reply_text(
        f"Ab vah **Text Bhejein** jiske saath aap '{context.user_data['find_text']}' ko **Badalna (Replace)** chahte hain (Replace Text).",
        reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}')
    )
    return EDIT_RULE_SET_REPLACEMENT_REPLACE

async def set_rule_replacement_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    rule_id = context.user_data.get('current_rule_id')
    rule = get_rule_by_id(rule_id) 
    if not rule:
        await update.message.reply_text("Rule ID lost/galat hai.", reply_markup=create_back_keyboard('manage_rules'))
        return ConversationHandler.END
        
    find_text = context.user_data.pop('find_text')
    replace_text = update.message.text.strip()
    
    replacements = rule.TEXT_REPLACEMENTS or {}
    replacements[find_text] = replace_text
    rule.TEXT_REPLACEMENTS = replacements
    save_rule_to_db(rule)
    
    await update.message.reply_text(
        f"**Rule {rule_id} Replacement Niyam** safaltapoorvak set kiya gaya:\n"
        f"**Dhoondhein:** `{find_text}`\n"
        f"**Badlein:** `{replace_text}`",
        reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

# 10. Core Forwarding Logic (Updated for Multiple Rules, Header/Footer, and Schedule)
def is_scheduled_sleep_time(config: GlobalConfig) -> bool:
    """Checks if the current time is within the scheduled sleep hours (inclusive start, exclusive end)."""
    if not config.SCHEDULE_ACTIVE:
        return False
        
    now = datetime.now().time()
    
    start = time_obj(config.SLEEP_START_HOUR, 0)
    end = time_obj(config.SLEEP_END_HOUR, 0)
    
    # If sleep crosses midnight (e.g., 23:00 to 06:00)
    if start > end:
        return now >= start or now < end
    # If sleep is within the same day (e.g., 00:00 to 06:00)
    else:
        return start <= now < end

async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks and forwards messages based on ALL applicable configurations/rules."""
    # Use update.message for regular messages, update.channel_post for channel posts
    message = update.channel_post or update.message 
    
    # Ensure there is a message to process
    if not message:
        return
        
    # Ignore messages from the Admin (to prevent bot feedback loops)
    if message.chat.type == 'private' and is_admin(message.chat.id):
        return
        
    # 1. Schedule Check (Global Stop)
    global_config = load_global_config_from_db() # Load fresh config for forwarding logic
    if is_scheduled_sleep_time(global_config):
        logger.info("Message received but scheduled sleep is active. Skipping.")
        return
        
    # Get all active rules
    all_rules = get_all_rules()
    
    # 2. Process message against all relevant rules
    for rule in all_rules:
        # Check if rule is active
        if not rule.IS_ACTIVE: continue
        
        source_id_str = rule.SOURCE_CHAT_ID
        dest_id = rule.DESTINATION_CHAT_ID
        
        # Check if the message comes from the rule's source ID(s)
        is_source_id_match = (source_id_str and str(message.chat.id) == source_id_str)
        is_source_username_match = (source_id_str and source_id_str.startswith('@') and message.chat.username and message.chat.username.lower() == source_id_str[1:].lower())
        
        if not (is_source_id_match or is_source_username_match):
            continue 
            
        if not dest_id:
            logger.warning(f"Rule {rule.id} matches source but no destination is set.")
            continue 
            
        # --- Filtering & Replacement ---
        text_to_process = message.text or message.caption or ""
        text_lower = text_to_process.lower()

        # Filters: Links, Usernames (Rule Specific)
        if rule.BLOCK_LINKS and ('http' in text_lower or 't.me' in text_lower): continue
        if rule.BLOCK_USERNAMES and re.search(r'@[a-zA-Z0-9_]+', text_lower): continue

        # Filters: Blacklist (Rule Specific)
        if rule.WORD_BLACKLIST:
            if any(word in text_lower for word in rule.WORD_BLACKLIST): continue

        # Filters: Whitelist (Rule Specific)
        if rule.WORD_WHITELIST:
            if not any(word in text_lower for word in rule.WORD_WHITELIST): continue

        # Text Replacement Logic
        final_text = text_to_process
        text_modified = False 
        
        if rule.TEXT_REPLACEMENTS and final_text:
            for find, replace in rule.TEXT_REPLACEMENTS.items():
                if find in final_text:
                    final_text = final_text.replace(find, replace)
                    text_modified = True
                    
        # Apply Global Header/Footer
        if final_text or message.photo or message.video or message.document or message.audio or message.voice or message.sticker or message.animation or message.poll:
            if global_config.GLOBAL_HEADER:
                # Only prepend header if it's not already there (to avoid double headers on edits/multiple forwards)
                if final_text and not final_text.strip().startswith(global_config.GLOBAL_HEADER.strip()):
                    final_text = global_config.GLOBAL_HEADER + "\n\n" + final_text
                    text_modified = True
                elif not final_text:
                    final_text = global_config.GLOBAL_HEADER
                    text_modified = True
            
            if global_config.GLOBAL_FOOTER:
                # Only append footer if it's not already there
                if final_text and not final_text.strip().endswith(global_config.GLOBAL_FOOTER.strip()):
                    final_text = final_text + "\n\n" + global_config.GLOBAL_FOOTER
                    text_modified = True
                elif not final_text:
                     final_text = global_config.GLOBAL_FOOTER
                     text_modified = True

        # Apply Delay (Rule Specific)
        if rule.FORWARD_DELAY_SECONDS > 0:
            time.sleep(rule.FORWARD_DELAY_SECONDS)

        # --- CORE FORWARDING MODE LOGIC ---
        force_copy = text_modified or (rule.FORWARDING_MODE == 'COPY')
        
        original_parse_mode = getattr(message, 'parse_mode', None)

        final_parse_mode = None 
        # If we are using FORWARD mode AND no text was modified, try to keep the original parse mode
        if rule.FORWARDING_MODE == 'FORWARD' and not text_modified and original_parse_mode:
            final_parse_mode = original_parse_mode
        # If we are COPYing (forced or by mode) and we have text, use MARKDOWN (Telegram.ext default is often HTML)
        elif force_copy and (final_text or original_parse_mode):
            final_parse_mode = ParseMode.MARKDOWN 

        try:
            if force_copy:
                # Pure Text Message
                if message.text and not message.caption:
                    if final_text and final_text.strip():
                         await context.bot.send_message(chat_id=dest_id, text=final_text, parse_mode=final_parse_mode, disable_web_page_preview=True)
                
                # Message has media
                elif message.photo or message.video or message.document or message.audio or message.voice or message.sticker or message.animation or message.poll:
                    caption_to_send = final_text if final_text else None 
                    await context.bot.copy_message(
                        chat_id=dest_id, 
                        from_chat_id=message.chat.id, 
                        message_id=message.message_id, 
                        caption=caption_to_send, 
                        parse_mode=final_parse_mode,
                    )
                
            else:
                # Case 2: Use forward_message (Original behavior, no modifications)
                await context.bot.forward_message(chat_id=dest_id, from_chat_id=message.chat.id, message_id=message.message_id)

        except Exception as e:
            logger.error(f"Error processing message for Rule {rule.id} to {dest_id}: {e}")
            
# 11. Main Function 
def main() -> None:
    """Start the bot."""
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set. Bot cannot start.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # ----------------------------------------------------------------------
    # Command and Conversation Handler Setup
    # ----------------------------------------------------------------------
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("restart", restart_bot_command))
    
    # CallbackQueryHandler is the entry point for all button clicks (menus)
    conv_handler = ConversationHandler(
        # start command ko bhi entry point bana dete hain, yeh /start se menu kholne me madad karta hai
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(handle_callback)], 
        states={
            # Rule Creation Steps
            NEW_RULE_SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_new_rule_source_id)],
            NEW_RULE_SET_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_new_rule_destination_id)],
            
            # Rule Editing Text Input Steps
            EDIT_RULE_SET_REPLACEMENT_FIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_replacement_find)],
            EDIT_RULE_SET_REPLACEMENT_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_replacement_replace)],
            EDIT_RULE_SET_BLACKLIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_blacklist_word)], 
            EDIT_RULE_SET_WHITELIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_whitelist_word)],
            
            # Global Setting Text Input Steps
            SET_GLOBAL_HEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_global_header)],
            SET_GLOBAL_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_global_footer)],

        },
        fallbacks=[
            # Fallbacks mein CallbackQueryHandler aur Commands ka hona zaroori hai
            CallbackQueryHandler(handle_callback),
            CommandHandler("start", start),
            CommandHandler("restart", restart_bot_command), 
        ],
        allow_reentry=True
    )
    
    application.add_handler(conv_handler)
    
    # Message handler for forwarding logic (must be last)
    # Exclude EDITED_CHANNEL_POST to avoid issues with message reprocessing
    application.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED_CHANNEL_POST, forward_message))
    
    # Webhook Setup for Render/Deployment
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
