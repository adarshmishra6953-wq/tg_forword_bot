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
from telegram.constants import ParseMode 
from telegram.error import BadRequest # For handling 'Message is not modified' error

from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.orm.exc import DetachedInstanceError # For handling detached instances


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
    except Exception as e:
        logger.error(f"Database connection error during table creation: {e}")

# Example: Use your actual Telegram User ID here to force admin status
# IMPORTANT: Replace 1695450646 with YOUR Telegram User ID to ensure you are the admin!
FORCE_ADMIN_ID = 1695450646 

# 4. Configuration Management Functions
def get_fresh_rule_or_config(model, entity_id=1):
    """Loads a fresh, detached entity (Rule or GlobalConfig) from DB."""
    if not Engine: 
        return GlobalConfig(id=1, ADMIN_USER_ID=None) if model == GlobalConfig else None
        
    session = Session()
    entity = None
    try:
        if model == GlobalConfig:
            entity = session.query(GlobalConfig).filter(GlobalConfig.id == entity_id).first()
            if not entity:
                entity = GlobalConfig(id=1)
                session.add(entity)
                session.commit()
                entity = session.query(GlobalConfig).filter(GlobalConfig.id == entity_id).first()
                logger.info("New GlobalConfig entry created in DB.")
            
        elif model == ForwardingRule:
            entity = session.query(ForwardingRule).filter(ForwardingRule.id == entity_id).first()
            
        if entity:
            # CRITICAL FIX for DetachedInstanceError: Always detach the object
            session.expunge(entity) 
            return entity
            
        if model == GlobalConfig:
             return GlobalConfig(id=1, ADMIN_USER_ID=None)
             
        return entity
        
    except Exception as e:
        logger.error(f"Error loading fresh entity {model.__name__} ID {entity_id} during operation: {e}")
        return GlobalConfig(id=1, ADMIN_USER_ID=None) if model == GlobalConfig else None
    finally:
        if session:
            session.close()

def load_global_config_from_db():
    """Load global configuration from DB."""
    return get_fresh_rule_or_config(GlobalConfig)

def save_global_config_to_db(config):
    """Save the provided detached global configuration object back to the database."""
    if not Engine: return
    session = Session()
    try:
        # CRITICAL FIX for DetachedInstanceError: Use merge
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
    return get_fresh_rule_or_config(ForwardingRule, rule_id)

def save_rule_to_db(rule):
    """Saves or updates a single rule (which is detached)."""
    if not Engine: return
    session = Session()
    try:
        # CRITICAL FIX for DetachedInstanceError: Use merge
        merged_rule = session.merge(rule)
        session.flush() 
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
        rule = session.query(ForwardingRule).filter(ForwardingRule.id == rule_id).first()
        if rule:
            session.delete(rule)
            session.commit()
    except Exception as e:
        logger.error(f"Error deleting rule from DB: {e}")
    finally:
        if session:
            session.close()

# 5. Admin Check Utility
def is_admin(user_id):
    """Checks if the user is the admin or force admin."""
    current_config = load_global_config_from_db()
    
    return (current_config.ADMIN_USER_ID is not None and user_id == current_config.ADMIN_USER_ID) or (FORCE_ADMIN_ID and user_id == FORCE_ADMIN_ID)

# 6. Utility Functions (Inline Keyboard and Text formatting)
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
        [InlineKeyboardButton("🔄 Restart Bot (Reload Config)", callback_data='restart_bot_command')],
    ]
    return InlineKeyboardMarkup(keyboard)

def create_manage_rules_keyboard(rules):
    """Creates a keyboard to select rules for editing."""
    keyboard = []
    if rules:
        for rule in rules:
            status = '✅' if rule.IS_ACTIVE else '⏸️'
            # Display Rule ID and its Source/Destination info
            keyboard.append([InlineKeyboardButton(f"{status} Rule {rule.id} | {rule.SOURCE_CHAT_ID or 'S-Set Nahi'} -> {rule.DESTINATION_CHAT_ID or 'D-Set Nahi'}", callback_data=f'edit_rule_{rule.id}')])
    else:
        keyboard.append([InlineKeyboardButton("Koi Rule Maujood Nahi", callback_data='no_rules')])
        
    keyboard.append([InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

def create_rule_edit_keyboard(rule):
    """Creates the menu for editing a specific rule."""
    if not rule: return create_back_keyboard('manage_rules')
    
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


# 7. Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /start command and sets the admin user."""
    
    user_id = update.effective_user.id
    current_config = load_global_config_from_db() 
    
    # Set Admin ID logic 
    if FORCE_ADMIN_ID and current_config.ADMIN_USER_ID != FORCE_ADMIN_ID:
        current_config.ADMIN_USER_ID = FORCE_ADMIN_ID
        save_global_config_to_db(current_config)
        logger.info(f"Admin User ID forcibly set to: {FORCE_ADMIN_ID}")
    
    elif current_config.ADMIN_USER_ID is None:
        current_config.ADMIN_USER_ID = user_id
        save_global_config_to_db(current_config)
        logger.info(f"Admin User ID set to: {user_id}")
    
    message_text = (
        f"Namaste! Aapka Telegram Auto-Forward Bot shuru ho gaya hai.\n\n"
        f"**Global Settings:**\n{get_global_settings_text(current_config)}"
    )

    if update.callback_query:
         # FIX for BadRequest: Message is not modified
         try:
             await update.callback_query.edit_message_text(
                message_text,
                reply_markup=create_main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
         except BadRequest as e:
            if "Message is not modified" in str(e):
                await update.callback_query.answer("Menu Reload Ho Gaya Hai.")
            else:
                raise 
                
    elif update.message:
        await update.message.reply_text(
            message_text,
            reply_markup=create_main_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    return ConversationHandler.END

# Restart Handler
async def restart_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /restart command or button click."""
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        if update.callback_query:
            await update.callback_query.answer("Aap Bot ke Admin nahi hain.")
        elif update.message:
             await update.message.reply_text("Aap Bot ke Admin nahi hain.")
        return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer("Bot Configuration Reload Ho Raha Hai...")
    elif update.message:
        await update.message.reply_text("Bot Configuration Reload Ho Raha Hai...")

    return await start(update, context)


# 8. Callback Handlers (For Inline Buttons)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all Inline Button presses and transitions conversations."""
    
    query = update.callback_query
    await query.answer()
    data = query.data
    
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return ConversationHandler.END
        
    current_config = load_global_config_from_db() 

    # --- Restart Handler ---
    if data == 'restart_bot_command':
        return await restart_bot_command(update, context)

    # --- Global Menus ---
    if data == 'main_menu':
        try:
            await query.edit_message_text(
                f"**Mukhya Menu (Main Menu)**\n\n**Global Settings:**\n{get_global_settings_text(current_config)}",
                reply_markup=create_main_menu_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
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
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Rules List Reload Ho Gayi Hai.") 
            else:
                raise
        return ConversationHandler.END
        
    elif data == 'new_rule':
        context.user_data['new_rule'] = ForwardingRule() 
        try:
            await query.edit_message_text("Kripya **Naye Rule** ke liye Source Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard('manage_rules'))
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Naya Rule Menu Khul Gaya Hai.")
            else:
                raise
        return NEW_RULE_SET_SOURCE
        
    # --- Rule Edit/Delete ---
    elif data.startswith('edit_rule_'):
        rule_id = int(data.split('_')[2])
        rule = get_rule_by_id(rule_id) 
        context.user_data['current_rule_id'] = rule_id 
        if not rule:
            await query.edit_message_text("Rule maujood nahi hai.", reply_markup=create_back_keyboard('manage_rules'))
            return ConversationHandler.END
            
        try:
            await query.edit_message_text(
                f"**Rule {rule_id} Edit Karein**\n\n{get_rule_settings_text(rule)}",
                reply_markup=create_rule_edit_keyboard(rule),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Rule Menu Reload Ho Gaya Hai.") 
            else:
                raise
        return ConversationHandler.END
        
    elif data.startswith('delete_rule_'):
        rule_id = int(data.split('_')[2])
        delete_rule_from_db(rule_id)
        rules = get_all_rules()
        
        try:
            await query.edit_message_text(
                f"**Rule {rule_id}** safaltapoorvak **Hata Diya Gaya** hai.\n\nAapke pass kul {len(rules)} Rules hain.",
                reply_markup=create_manage_rules_keyboard(rules)
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Delete Safal. Rules List Reload Ho Gayi Hai.") 
            else:
                raise
        return ConversationHandler.END
        
    # --- Global Settings Menu ---
    elif data == 'menu_global_settings':
        keyboard = [
            [InlineKeyboardButton("⬆️ Global Header Set", callback_data='set_global_header_menu')],
            [InlineKeyboardButton("⬇️ Global Footer Set", callback_data='set_global_footer_menu')],
            [InlineKeyboardButton(f"⏰ Schedule Sleep ({'✅' if current_config.SCHEDULE_ACTIVE else '❌'})", callback_data='toggle_schedule_active')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data='main_menu')]
        ]
        
        try:
            await query.edit_message_text(
                f"**Global Settings**\n\n{get_global_settings_text(current_config)}\n\n"
                f"Header Sample: `{'Set' if current_config.GLOBAL_HEADER else 'Nahi'}`\n"
                f"Footer Sample: `{'Set' if current_config.GLOBAL_FOOTER else 'Nahi'}`",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Global Settings Menu Reload Ho Gaya Hai.") 
            else:
                raise
        return ConversationHandler.END

    elif data == 'set_global_header_menu':
        await query.edit_message_text("Kripya vah **Text** bhejein jo **har message ke shuru** mein jodein. (Markup support karta hai)", reply_markup=create_back_keyboard('menu_global_settings'))
        return SET_GLOBAL_HEADER
        
    elif data == 'set_global_footer_menu':
        await query.edit_message_text("Kripya vah **Text** bhejein jo **har message ke ant** mein jodein. (Markup support karta hai)", reply_markup=create_back_keyboard('menu_global_settings'))
        return SET_GLOBAL_FOOTER

    elif data == 'toggle_schedule_active':
        current_config.SCHEDULE_ACTIVE = not current_config.SCHEDULE_ACTIVE
        save_global_config_to_db(current_config)
        
        try:
            await query.edit_message_text(
                f"**Scheduled Sleep** ab **{'Shuru' if current_config.SCHEDULE_ACTIVE else 'Ruka Hua'}** hai.\n\n"
                f"Current Schedule: {current_config.SLEEP_START_HOUR:02d}:00 to {current_config.SLEEP_END_HOUR:02d}:00",
                reply_markup=create_back_keyboard('menu_global_settings'),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Schedule Status Badal Diya Gaya Hai.") 
            else:
                raise
        return ConversationHandler.END


    # --- Rule Toggles (Toggles the specific rule's attribute) ---
    parts = data.split('_')
    if len(parts) > 2 and parts[-1].isdigit():
        rule_id = int(parts[-1])
        rule = get_rule_by_id(rule_id) 
        if not rule:
            await query.edit_message_text("Rule ID galat hai.", reply_markup=create_back_keyboard('manage_rules'))
            return ConversationHandler.END
            
        action = parts[0] + '_' + parts[1] 

        if action == 'toggle_active': rule.IS_ACTIVE = not rule.IS_ACTIVE
        elif action == 'toggle_block_links': rule.BLOCK_LINKS = not rule.BLOCK_LINKS
        elif action == 'toggle_block_usernames': rule.BLOCK_USERNAMES = not rule.BLOCK_USERNAMES
        
        save_rule_to_db(rule)
        rule_after_save = get_rule_by_id(rule_id) 
        
        # FIX: Ensure 'Message is not modified' is handled here!
        try:
            await query.edit_message_text(
                f"**Rule {rule_id} Setting Updated**\n\n{get_rule_settings_text(rule_after_save)}",
                reply_markup=create_rule_edit_keyboard(rule_after_save),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Status/Setting Badal Diya Gaya Hai.") 
            else:
                logger.error(f"Error editing message for rule {rule_id} toggle: {e}")
                raise

        return ConversationHandler.END
        
    # Forwarding Mode Menu (Rule Specific)
    elif data.startswith('menu_forwarding_mode_'):
        rule_id = int(data.split('_')[3])
        rule = get_rule_by_id(rule_id) 
        keyboard = [
            [InlineKeyboardButton(f"1. Forward (Original) {'✅' if rule.FORWARDING_MODE == 'FORWARD' else '❌'}", callback_data=f'set_mode_forward_{rule_id}')],
            [InlineKeyboardButton(f"2. Copy (Caption Editing) {'✅' if rule.FORWARDING_MODE == 'COPY' else '❌'}", callback_data=f'set_mode_copy_{rule_id}')],
            [InlineKeyboardButton("⬅️ Rule Edit", callback_data=f'edit_rule_{rule_id}')]
        ]
        
        try:
            await query.edit_message_text("Message Forwarding ka **Mode** chunein:", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Mode Menu Reload Ho Gaya Hai.") 
            else:
                raise
        return ConversationHandler.END

    elif data.startswith('set_mode_'):
        parts = data.split('_')
        mode = parts[2].upper()
        rule_id = int(parts[3])
        rule = get_rule_by_id(rule_id) 
        rule.FORWARDING_MODE = mode
        save_rule_to_db(rule)
        
        try:
            await query.edit_message_text(
                f"**Rule {rule_id} Forwarding Mode** ab `{mode}` set kar diya gaya hai.",
                reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Mode Safaltapoorvak Set.") 
            else:
                raise
        return ConversationHandler.END

    # --- Simplified Replacement/Blacklist/Whitelist Menu Stubs (For conversation start) ---
    elif data.startswith('menu_replacement_'):
        rule_id = int(data.split('_')[2])
        await query.edit_message_text("Kripya **Search Text** bhejein jise aap **Replace** karna chahte hain.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
        context.user_data['current_rule_id'] = rule_id
        return EDIT_RULE_SET_REPLACEMENT_FIND
        
    elif data.startswith('menu_blacklist_'):
        rule_id = int(data.split('_')[2])
        await query.edit_message_text("Kripya **Blacklist** karne wala **Shabdh** bhejein.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
        context.user_data['current_rule_id'] = rule_id
        return EDIT_RULE_SET_BLACKLIST_WORD
        
    elif data.startswith('menu_whitelist_'):
        rule_id = int(data.split('_')[2])
        await query.edit_message_text("Kripya **Whitelist** karne wala **Shabdh** bhejein.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
        context.user_data['current_rule_id'] = rule_id
        return EDIT_RULE_SET_WHITELIST_WORD

    return ConversationHandler.END


# 9. Conversation Handlers (For User Input) - FIX for NameError: set_new_rule_source_id is not defined

async def set_new_rule_source_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the source ID for a new rule and asks for the destination ID."""
    text = update.message.text.strip()
    rule = context.user_data.get('new_rule')
    
    if not rule:
        await update.message.reply_text("माफ़ करें, Rule खो गया है। कृपया /start से शुरू करें।")
        return ConversationHandler.END

    rule.SOURCE_CHAT_ID = text
    await update.message.reply_text(
        f"✅ Source ID (`{text}`) save ho gaya hai.\n\n"
        f"Ab **Destination Channel** ka **ID** ya **Username** bhejein.", 
        reply_markup=create_back_keyboard('manage_rules')
    )
    return NEW_RULE_SET_DESTINATION

async def set_new_rule_destination_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the destination ID, saves the rule, and returns to the edit menu."""
    text = update.message.text.strip()
    rule = context.user_data.get('new_rule')
    
    if not rule:
        await update.message.reply_text("माफ़ करें, Rule खो गया है। कृपया /start से शुरू करें।")
        return ConversationHandler.END

    rule.DESTINATION_CHAT_ID = text
    save_rule_to_db(rule) 
    
    rule_after_save = get_rule_by_id(rule.id) # Ensure the object is fresh for the next step

    await update.message.reply_text(
        f"✅ Destination ID (`{text}`) save ho gaya hai.\n\n"
        f"**Naya Rule {rule_after_save.id} Safaltapoorvak Banaya Gaya!**", 
        reply_markup=create_rule_edit_keyboard(rule_after_save),
        parse_mode=ParseMode.MARKDOWN
    )
    
    return ConversationHandler.END 


async def set_global_header(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sets the global message header."""
    text = update.message.text
    current_config = load_global_config_from_db()
    current_config.GLOBAL_HEADER = text
    save_global_config_to_db(current_config)

    await update.message.reply_text(
        f"✅ Global Header set kar diya gaya hai.\n\nSample:\n`{text}`",
        reply_markup=create_back_keyboard('menu_global_settings')
    )
    return ConversationHandler.END

async def set_global_footer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sets the global message footer."""
    text = update.message.text
    current_config = load_global_config_from_db()
    current_config.GLOBAL_FOOTER = text
    save_global_config_to_db(current_config)

    await update.message.reply_text(
        f"✅ Global Footer set kar diya gaya hai.\n\nSample:\n`{text}`",
        reply_markup=create_back_keyboard('menu_global_settings')
    )
    return ConversationHandler.END

# --- Rule Input Handlers (Stubs for functionality) ---

async def set_rule_replacement_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sets the find text for replacement."""
    context.user_data['temp_find_text'] = update.message.text
    await update.message.reply_text("Ab **Replace Text** bhejein.", reply_markup=create_back_keyboard(f"edit_rule_{context.user_data['current_rule_id']}"))
    return EDIT_RULE_SET_REPLACEMENT_REPLACE

async def set_rule_replacement_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sets the replace text and saves the replacement rule."""
    replace_text = update.message.text
    find_text = context.user_data.get('temp_find_text')
    rule_id = context.user_data.get('current_rule_id')
    
    rule = get_rule_by_id(rule_id)
    if rule and find_text:
        rule.TEXT_REPLACEMENTS = rule.TEXT_REPLACEMENTS or {}
        rule.TEXT_REPLACEMENTS[find_text] = replace_text
        save_rule_to_db(rule)
        rule_after_save = get_rule_by_id(rule_id) 
        
        await update.message.reply_text(
            f"✅ Replacement **'{find_text}' -> '{replace_text}'** Rule {rule_id} mein save ho gaya hai.",
            reply_markup=create_rule_edit_keyboard(rule_after_save),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("Replacement save nahi ho paya. Kripya fir se shuru karein.", reply_markup=create_back_keyboard('manage_rules'))
        
    return ConversationHandler.END

async def set_rule_blacklist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Adds a word to the rule's blacklist."""
    word = update.message.text.strip()
    rule_id = context.user_data.get('current_rule_id')
    rule = get_rule_by_id(rule_id)
    
    if rule:
        rule.WORD_BLACKLIST = rule.WORD_BLACKLIST or []
        if word not in rule.WORD_BLACKLIST:
            rule.WORD_BLACKLIST.append(word)
            save_rule_to_db(rule)
            rule_after_save = get_rule_by_id(rule_id)
            
            await update.message.reply_text(
                f"✅ Shabdh **'{word}'** Rule {rule_id} ke **Blacklist** mein jod diya gaya hai.",
                reply_markup=create_rule_edit_keyboard(rule_after_save),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(f"Shabdh **'{word}'** pehle se hi Blacklist mein hai.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
    else:
        await update.message.reply_text("Rule nahi mila.", reply_markup=create_back_keyboard('manage_rules'))
        
    return ConversationHandler.END

async def set_rule_whitelist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Adds a word to the rule's whitelist."""
    word = update.message.text.strip()
    rule_id = context.user_data.get('current_rule_id')
    rule = get_rule_by_id(rule_id)
    
    if rule:
        rule.WORD_WHITELIST = rule.WORD_WHITELIST or []
        if word not in rule.WORD_WHITELIST:
            rule.WORD_WHITELIST.append(word)
            save_rule_to_db(rule)
            rule_after_save = get_rule_by_id(rule_id)
            
            await update.message.reply_text(
                f"✅ Shabdh **'{word}'** Rule {rule_id} ke **Whitelist** mein jod diya gaya hai.",
                reply_markup=create_rule_edit_keyboard(rule_after_save),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(f"Shabdh **'{word}'** pehle se hi Whitelist mein hai.", reply_markup=create_back_keyboard(f'edit_rule_{rule_id}'))
    else:
        await update.message.reply_text("Rule nahi mila.", reply_markup=create_back_keyboard('manage_rules'))
        
    return ConversationHandler.END


# 10. Core Forwarding Logic
def is_within_sleep_time(config):
    """Checks if the current time is within the bot's sleep schedule."""
    if not config.SCHEDULE_ACTIVE:
        return False
    
    now = datetime.now()
    start = time_obj(config.SLEEP_START_HOUR, 0)
    end = time_obj(config.SLEEP_END_HOUR, 0)

    if start < end:
        return start <= now.time() < end
    else: 
        return now.time() >= start or now.time() < end

def apply_text_replacements(text, rule):
    """Applies all text replacements defined in the rule."""
    if not text or not rule.TEXT_REPLACEMENTS:
        return text
    
    new_text = text
    for find, replace in rule.TEXT_REPLACEMENTS.items():
        new_text = re.sub(re.escape(find), replace, new_text, flags=re.IGNORECASE)
        
    return new_text

def contains_blacklisted_or_missing_whitelisted(text, rule):
    """Checks for blacklisted words or missing whitelisted words."""
    if not text:
        return (rule.WORD_BLACKLIST and len(rule.WORD_BLACKLIST) > 0) or (rule.WORD_WHITELIST and len(rule.WORD_WHITELIST) > 0)

    # Check Blacklist
    if rule.WORD_BLACKLIST:
        text_lower = text.lower()
        for word in rule.WORD_BLACKLIST:
            if re.search(r'\b' + re.escape(word.lower()) + r'\b', text_lower):
                return True # Blocked by blacklist

    # Check Whitelist
    if rule.WORD_WHITELIST:
        text_lower = text.lower()
        found_whitelist_word = False
        for word in rule.WORD_WHITELIST:
            if re.search(r'\b' + re.escape(word.lower()) + r'\b', text_lower):
                found_whitelist_word = True
                break
        
        if not found_whitelist_word:
            return True # Blocked because no whitelisted word was found
            
    return False 

async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The main function to handle incoming messages and forward them."""
    message = update.channel_post or update.message
    if not message:
        return
    
    global_config = load_global_config_from_db()
    
    # 1. Check Sleep Schedule
    if is_within_sleep_time(global_config):
        logger.info(f"Message from {message.chat_id} ignored due to sleep schedule.")
        return

    # 2. Find matching rules for the source chat ID
    source_chat_id = str(message.chat_id)
    all_rules = get_all_rules()
    matching_rules = [rule for rule in all_rules if str(rule.SOURCE_CHAT_ID) == source_chat_id and rule.IS_ACTIVE]

    if not matching_rules:
        return

    # 3. Process the message for each matching rule
    for rule in matching_rules:
        try:
            text_to_process = message.text or message.caption
            
            # Apply Filters
            if text_to_process:
                if rule.BLOCK_LINKS and re.search(r'https?://\S+|www\.\S+', text_to_process):
                    logger.info(f"Rule {rule.id}: Blocked link in message from {source_chat_id}.")
                    continue
                
                if rule.BLOCK_USERNAMES and re.search(r'@\w+', text_to_process):
                    logger.info(f"Rule {rule.id}: Blocked username in message from {source_chat_id}.")
                    continue
                    
                if contains_blacklisted_or_missing_whitelisted(text_to_process, rule):
                    logger.info(f"Rule {rule.id}: Blocked by word filter in message from {source_chat_id}.")
                    continue

            # Apply delay
            if rule.FORWARD_DELAY_SECONDS > 0:
                time.sleep(rule.FORWARD_DELAY_SECONDS)
            
            # --- Forwarding Mode ---
            if rule.FORWARDING_MODE == 'FORWARD':
                await message.forward(chat_id=rule.DESTINATION_CHAT_ID)
                
            elif rule.FORWARDING_MODE == 'COPY':
                new_caption = message.caption
                
                # 1. Apply Text Replacements
                text_content = new_caption if new_caption else message.text
                if text_content:
                    modified_text = apply_text_replacements(text_content, rule)
                else:
                    modified_text = ""
                
                # 2. Apply Header/Footer
                final_caption = ""
                if global_config.GLOBAL_HEADER:
                    final_caption += global_config.GLOBAL_HEADER + "\n\n"
                
                final_caption += modified_text
                
                if global_config.GLOBAL_FOOTER:
                    if final_caption and not final_caption.endswith(('\n', ' ')):
                        final_caption += "\n"
                    final_caption += global_config.GLOBAL_FOOTER
                
                # 3. Send
                if message.text: # Handles pure text messages
                    await message.copy(chat_id=rule.DESTINATION_CHAT_ID, caption=final_caption, parse_mode=ParseMode.MARKDOWN)
                else: # Handles media messages with or without caption
                    await message.copy(chat_id=rule.DESTINATION_CHAT_ID, caption=final_caption, parse_mode=ParseMode.MARKDOWN)
            
            logger.info(f"Message from {source_chat_id} forwarded to {rule.DESTINATION_CHAT_ID} via Rule {rule.id}")
            
        except Exception as e:
            logger.error(f"Error forwarding message with Rule {rule.id}: {e}")


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
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_callback)],
        states={
            # State definitions linked to functions in Section 9
            NEW_RULE_SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_new_rule_source_id)],
            NEW_RULE_SET_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_new_rule_destination_id)],
            EDIT_RULE_SET_REPLACEMENT_FIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_replacement_find)],
            EDIT_RULE_SET_REPLACEMENT_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_replacement_replace)],
            EDIT_RULE_SET_BLACKLIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_blacklist_word)], 
            EDIT_RULE_SET_WHITELIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_whitelist_word)],
            SET_GLOBAL_HEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_global_header)],
            SET_GLOBAL_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_global_footer)],

        },
        fallbacks=[
            CallbackQueryHandler(handle_callback),
            CommandHandler("start", start),
            CommandHandler("restart", restart_bot_command), 
        ],
        allow_reentry=True
    )
    
    application.add_handler(conv_handler)
    
    # Message Handler for forwarding logic
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
