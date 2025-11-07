import os
import logging
import time
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError, IntegrityError

# 1. Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States (Ensure these are unique integers)
(
    SET_RULE_NAME,
    SET_SOURCE_ID,
    SET_DESTINATION_ID,
    SET_REPLACEMENT_FIND,
    SET_REPLACEMENT_REPLACE,
    SET_BLACKLIST_WORD,
    SET_WHITELIST_WORD,
    SET_PREFIX_TEXT,
    SET_SUFFIX_TEXT,
    SET_MESSAGE_FOR_BUTTONS,
    SET_BUTTON_DATA,
    SELECT_RULE_FOR_ACTION
) = range(12) 

# 2. Database Setup (SQLAlchemy)
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL environment variable is not set. Bot will not save settings.")

# Adjust URL format for Render/Heroku PostgreSQL compatibility
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

Engine = create_engine(DATABASE_URL) if DATABASE_URL else None
Base = declarative_base()
Session = sessionmaker(bind=Engine)

# 3. Database Model
class ForwardRule(Base):
    """Stores configuration for one Source -> Destination forwarding rule."""
    __tablename__ = 'forward_rules'
    id = Column(Integer, primary_key=True)
    # FIX: Ensure rule_name is not null and is unique
    rule_name = Column(String, unique=True, nullable=False) 
    
    # Core
    SOURCE_CHAT_ID = Column(String) # Can be comma-separated list of IDs/Usernames
    DESTINATION_CHAT_ID = Column(String)
    IS_ACTIVE = Column(Boolean, default=True)
    FORWARDING_MODE = Column(String, default='COPY') 
    FORWARD_DELAY_SECONDS = Column(Integer, default=0)
    
    # Text Customization
    PREFIX_TEXT = Column(Text, default="")
    SUFFIX_TEXT = Column(Text, default="")
    
    # Filters/Actions
    BLOCK_LINKS = Column(Boolean, default=False)
    BLOCK_USERNAMES = Column(Boolean, default=False)
    BLOCK_MEDIA = Column(Boolean, default=False)
    BLOCK_FORWARDS = Column(Boolean, default=False)
    REMOVE_WEB_PREVIEW = Column(Boolean, default=False)
    REMOVE_BUTTONS = Column(Boolean, default=True) 
    REMOVE_CAPTION = Column(Boolean, default=False) 
    SILENT_FORWARDING = Column(Boolean, default=False) 
    BLOCK_SERVICE_MESSAGES = Column(Boolean, default=True) 
    BLOCK_REPLIES = Column(Boolean, default=False) 
    
    # List Data
    TEXT_REPLACEMENTS = Column(PickleType, default={})
    WORD_BLACKLIST = Column(PickleType, default=[]) 
    WORD_WHITELIST = Column(PickleType, default=[])

# Create tables if Engine is available
if Engine:
    try:
        Base.metadata.create_all(Engine)
        logger.info("Database table created/updated successfully.")
    except OperationalError as e:
        logger.error(f"Database connection error during table creation/update: {e}")

# Example: Use your actual Telegram User ID here to force admin status
FORCE_ADMIN_ID = 1695450646 

# 4. Configuration Management Functions (No changes needed, kept as is)
def get_all_rules():
    """Load all rules from DB."""
    if not Engine: return []
    session = Session()
    try:
        rules = session.query(ForwardRule).all()
        session.expunge_all() 
        return rules
    except Exception as e:
        logger.error(f"Error loading all rules: {e}")
        return []
    finally:
        session.close()

def get_rule_by_id(rule_id):
    """Load a single rule by its ID."""
    if not Engine: return None
    session = Session()
    try:
        rule = session.query(ForwardRule).filter(ForwardRule.id == rule_id).first()
        if rule:
            session.expunge(rule)
        return rule
    except Exception as e:
        logger.error(f"Error loading rule {rule_id}: {e}")
        return None
    finally:
        session.close()

def save_rule_to_db(rule):
    """Save the provided rule object back to the database."""
    if not Engine: return
    session = Session()
    try:
        session.merge(rule) 
        session.commit()
    except Exception as e:
        logger.error(f"Error saving rule to DB: {e}")
    finally:
        session.close()

# 5. Utility Functions (Inline Keyboard and Text formatting) (No changes needed, kept as is)

def is_admin(user_id):
    """Checks if the user is the admin."""
    return user_id == FORCE_ADMIN_ID

def get_rule_settings_text(rule):
    """Returns a formatted string of current rule settings."""
    status = "Active" if rule.IS_ACTIVE else "Inactive"
    mode_text = "Forward (Original)" if rule.FORWARDING_MODE == 'FORWARD' else "Copy (Full Control)"

    # Existing & New Filters
    links = "✅" if rule.BLOCK_LINKS else "❌"
    usernames = "✅" if rule.BLOCK_USERNAMES else "❌"
    media = "✅" if rule.BLOCK_MEDIA else "❌"
    forwards = "✅" if rule.BLOCK_FORWARDS else "❌"
    preview = "✅" if rule.REMOVE_WEB_PREVIEW else "❌"
    
    # New Features Status
    rem_buttons = "✅" if rule.REMOVE_BUTTONS else "❌"
    rem_caption = "✅" if rule.REMOVE_CAPTION else "❌"
    silent = "✅" if rule.SILENT_FORWARDING else "❌"
    block_service = "✅" if rule.BLOCK_SERVICE_MESSAGES else "❌"
    block_replies = "✅" if rule.BLOCK_REPLIES else "❌"
    
    replacements_list = "\n".join(
        [f"   - '{f}' -> '{r}'" for f, r in (rule.TEXT_REPLACEMENTS or {}).items()]
    ) if (rule.TEXT_REPLACEMENTS and len(rule.TEXT_REPLACEMENTS) > 0) else "Koi Niyam Set Nahi"

    blacklist_list = ", ".join(rule.WORD_BLACKLIST or []) if (rule.WORD_BLACKLIST and len(rule.WORD_BLACKLIST) > 0) else "Koi Shabdh Block Nahi"
    whitelist_list = ", ".join(rule.WORD_WHITELIST or []) if (rule.WORD_WHITELIST and len(rule.WORD_WHITELIST) > 0) else "Koi Shabdh Jaruri Nahi"

    return (
        f"**Rule Name:** `{rule.rule_name}`\n"
        f"**Rule ID:** `{rule.id}`\n"
        f"**Status:** `{status}`\n"
        f"**Mode:** `{mode_text}`\n\n"
        f"**Source ID:** `{rule.SOURCE_CHAT_ID or 'Set Nahi'}`\n"
        f"**Destination ID:** `{rule.DESTINATION_CHAT_ID or 'Set Nahi'}`\n\n"
        f"**Filters & Actions:**\n"
        f" - Delay: `{rule.FORWARD_DELAY_SECONDS}s`\n"
        f" - Links Block: `{links}`\n"
        f" - Usernames Block: `{usernames}`\n"
        f" - Media Block: `{media}`\n"
        f" - Forwards Block: `{forwards}`\n"
        f" - Web Preview Remove: `{preview}`\n"
        f" - **Remove Buttons:** `{rem_buttons}`\n"
        f" - **Remove Caption:** `{rem_caption}`\n"
        f" - **Silent Forward:** `{silent}`\n"
        f" - **Block Service Msgs:** `{block_service}`\n"
        f" - **Block Replies:** `{block_replies}`\n\n"
        f"**Prefix/Suffix:**\n"
        f" - Prefix: `{rule.PREFIX_TEXT[:30] + '...' if rule.PREFIX_TEXT else 'Nahi'}`\n"
        f" - Suffix: `{rule.SUFFIX_TEXT[:30] + '...' if rule.SUFFIX_TEXT else 'Nahi'}`\n\n"
        f"**Word Lists:**\n"
        f" - Blacklist: `{blacklist_list}`\n"
        f" - Whitelist: `{whitelist_list}`\n\n"
        f"**Text Replacement Rules:**\n{replacements_list}"
    )

def create_rule_list_keyboard(rules):
    """Creates the keyboard for selecting and managing rules."""
    keyboard = []
    for rule in rules:
        keyboard.append([
            InlineKeyboardButton(f"⚙️ {rule.rule_name} (ID: {rule.id})", callback_data=f'select_rule_{rule.id}')
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Naya Rule Jodein", callback_data='create_new_rule')])
    keyboard.append([InlineKeyboardButton("💬 Message Mein Button Jodein", callback_data='add_button_menu')]) 
    
    return InlineKeyboardMarkup(keyboard)

def create_rule_settings_keyboard(rule):
    """Creates the main inline keyboard menu for a specific rule."""
    rule_id = rule.id
    keyboard = [
        [
            InlineKeyboardButton("➡️ Source/Destination Set Karein", callback_data=f'menu_core_{rule_id}'),
            InlineKeyboardButton("📝 Text Niyam (Replacements/Lists)", callback_data=f'menu_text_{rule_id}')
        ],
        [
            InlineKeyboardButton(f"📨 Mode: {'COPY' if rule.FORWARDING_MODE == 'COPY' else 'FORWARD'} & Delay", callback_data=f'menu_mode_delay_{rule_id}')
        ],
        [
            InlineKeyboardButton(f"🔗 Links Block ({'✅' if rule.BLOCK_LINKS else '❌'})", callback_data=f'toggle_block_links_{rule_id}'),
            InlineKeyboardButton(f"👤 Usernames Block ({'✅' if rule.BLOCK_USERNAMES else '❌'})", callback_data=f'toggle_block_usernames_{rule_id}')
        ],
        [
            InlineKeyboardButton(f"🖼️ Media/Forward Filters", callback_data=f'menu_media_filters_{rule_id}'),
            InlineKeyboardButton(f"🛠️ Caption/Reply/Silent Actions", callback_data=f'menu_advanced_actions_{rule_id}')
        ],
        [
            InlineKeyboardButton("⏸️ Rokein" if rule.IS_ACTIVE else "▶️ Shuru Karein", callback_data=f'toggle_active_{rule_id}'),
            InlineKeyboardButton("🗑️ Rule Delete Karein", callback_data=f'delete_rule_{rule_id}')
        ],
        [
            InlineKeyboardButton("⬅️ Rule List Par Wapas Jaane", callback_data='main_menu')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_core_settings_keyboard(rule_id):
    """Core settings: Source/Destination."""
    keyboard = [
        [
            InlineKeyboardButton("➡️ Source Set Karein", callback_data=f'set_source_{rule_id}'),
            InlineKeyboardButton("🎯 Destination Set Karein", callback_data=f'set_destination_{rule_id}')
        ],
        [
            InlineKeyboardButton("⬅️ Rule Settings", callback_data=f'select_rule_{rule_id}')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_media_filters_keyboard(rule):
    """New dedicated menu for media-related filters."""
    rule_id = rule.id
    keyboard = [
        [
            InlineKeyboardButton(f"🖼️ Media Block ({'✅' if rule.BLOCK_MEDIA else '❌'})", callback_data=f'toggle_block_media_{rule_id}'),
            InlineKeyboardButton(f"↪️ Forwards Block ({'✅' if rule.BLOCK_FORWARDS else '❌'})", callback_data=f'toggle_block_forwards_{rule_id}')
        ],
        [
            InlineKeyboardButton(f"🗑️ Remove Buttons ({'✅' if rule.REMOVE_BUTTONS else '❌'})", callback_data=f'toggle_remove_buttons_{rule_id}'),
            InlineKeyboardButton(f"🗑️ Remove Caption ({'✅' if rule.REMOVE_CAPTION else '❌'})", callback_data=f'toggle_remove_caption_{rule_id}')
        ],
        [
            InlineKeyboardButton("⬅️ Rule Settings", callback_data=f'select_rule_{rule_id}')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_advanced_actions_keyboard(rule):
    """New dedicated menu for advanced actions."""
    rule_id = rule.id
    keyboard = [
        [
            InlineKeyboardButton(f"🔇 Silent Forward ({'✅' if rule.SILENT_FORWARDING else '❌'})", callback_data=f'toggle_silent_forwarding_{rule_id}'),
            InlineKeyboardButton(f"⛔️ Block Service Msgs ({'✅' if rule.BLOCK_SERVICE_MESSAGES else '❌'})", callback_data=f'toggle_block_service_messages_{rule_id}')
        ],
        [
            InlineKeyboardButton(f"❌ Block Replies ({'✅' if rule.BLOCK_REPLIES else '❌'})", callback_data=f'toggle_block_replies_{rule_id}'),
            InlineKeyboardButton(f"📝 Prefix/Suffix Text", callback_data=f'menu_prefix_suffix_{rule_id}')
        ],
        [
            InlineKeyboardButton("⬅️ Rule Settings", callback_data=f'select_rule_{rule_id}')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_text_menu_keyboard(rule_id):
    """Menu for Text processing: Replacement, Blacklist, Whitelist."""
    keyboard = [
        [
            InlineKeyboardButton("📝 Text Replacement Manage Karein", callback_data=f'menu_replacement_{rule_id}')
        ],
        [
            InlineKeyboardButton("⛔️ Blacklist Manage Karein", callback_data=f'menu_blacklist_{rule_id}'),
            InlineKeyboardButton("✅ Whitelist Manage Karein", callback_data=f'menu_whitelist_{rule_id}')
        ],
        [
            InlineKeyboardButton("⬅️ Rule Settings", callback_data=f'select_rule_{rule_id}')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_replacement_menu_keyboard(rule_id):
    """Menu for Text Replacement: Add, Delete All, Delete Individual."""
    keyboard = [
        [InlineKeyboardButton("➕ Naya Niyam Jodein", callback_data=f'add_replacement_find_{rule_id}')], 
        [
            InlineKeyboardButton("🗑️ Saare Niyam Hatayein", callback_data=f'clear_replacements_{rule_id}'), 
            InlineKeyboardButton("❌ Niyam Hatayein (Individual)", callback_data=f'delete_replacement_select_{rule_id}') 
        ], 
        [InlineKeyboardButton("⬅️ Piche Jaane", callback_data=f'menu_text_{rule_id}')]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_individual_delete_keyboard(replacements, rule_id):
    """Keyboard to select individual replacements for deletion."""
    keyboard = []
    for find_text, _ in replacements.items():
        display_text = find_text[:20] + '...' if len(find_text) > 20 else find_text
        # WARNING: Long find_text will break callback_data limit (64 bytes). 
        # Using a fixed length for display and full text for confirmation in a real-world scenario is risky.
        # For simplicity in this bot, we stick to the user's original logic.
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f'delete_replacement_confirm_{rule_id}_{find_text}')])
    
    keyboard.append([InlineKeyboardButton("⬅️ Piche Jaane", callback_data=f'menu_replacement_{rule_id}')])
    return InlineKeyboardMarkup(keyboard)

def create_back_keyboard(callback_data='main_menu'):
    """Creates a back button keyboard."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Piche Jaane", callback_data=callback_data)]])

# 6. Command Handlers (No changes needed, kept as is)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the /start command and sets the admin user."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return ConversationHandler.END

    rules = get_all_rules()
    
    await update.message.reply_text(
        f"Namaste! Aapka Telegram Auto-Forward Bot shuru ho gaya hai.\n"
        f"Ab aap **Multiple Rules** manage kar sakte hain.\n\n"
        f"**Kul Rules:** {len(rules)}",
        reply_markup=create_rule_list_keyboard(rules),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

# 7. Callback Handlers (For Inline Buttons) (No major changes needed, kept as is)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all Inline Button presses and transitions conversations."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    # Admin Check (Ensures only admin can change settings)
    if not is_admin(chat_id):
        await query.message.reply_text("Aap Bot ke Admin nahi hain. Sirf Admin hi settings badal sakta hai.")
        return

    # --- Rule List Navigation ---
    if data == 'main_menu':
        rules = get_all_rules()
        await query.edit_message_text(
            f"**Rule List**\n\nKul Rules: {len(rules)}",
            reply_markup=create_rule_list_keyboard(rules),
            parse_mode='Markdown'
        )
        return ConversationHandler.END
        
    elif data.startswith('select_rule_'):
        rule_id = int(data.split('_')[2])
        rule = get_rule_by_id(rule_id)
        if not rule:
            await query.edit_message_text("❌ Rule nahi mila!", reply_markup=create_back_keyboard())
            return ConversationHandler.END
            
        context.user_data['current_rule_id'] = rule_id
        await query.edit_message_text(
            f"**Rule Settings**\n\n{get_rule_settings_text(rule)}",
            reply_markup=create_rule_settings_keyboard(rule),
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    elif data == 'create_new_rule':
        await query.edit_message_text("Kripya naye rule ka **Naam** (Jaise: `Cricket_to_News`) bhejein.", reply_markup=create_back_keyboard())
        return SET_RULE_NAME
        
    elif data.startswith('delete_rule_'):
        rule_id = int(data.split('_')[2])
        session = Session()
        try:
            rule = session.query(ForwardRule).filter(ForwardRule.id == rule_id).first()
            if rule:
                session.delete(rule)
                session.commit()
                await query.edit_message_text(f"✅ Rule **{rule.rule_name}** safaltapoorvak **Delete** kar diya gaya hai.", reply_markup=create_back_keyboard())
            else:
                await query.edit_message_text("❌ Rule nahi mila.", reply_markup=create_back_keyboard())
        except Exception as e:
            await query.edit_message_text(f"❌ Rule delete karte samay error hua: {e}", reply_markup=create_back_keyboard())
        finally:
            session.close()
        return ConversationHandler.END

    # --- Button Add Feature Start ---
    elif data == 'add_button_menu':
        await query.edit_message_text(
            "Kripya Target Channel ID aur Message ID is format mein bhejein (Example: `-1001234567890:54321`)",
            reply_markup=create_back_keyboard()
        )
        return SET_MESSAGE_FOR_BUTTONS

    # --- Rule Specific Menus ---
    elif data.startswith('menu_'):
        parts = data.split('_')
        menu_type = parts[1]
        rule_id = int(parts[-1])
        rule = get_rule_by_id(rule_id)

        if menu_type == 'core':
            await query.edit_message_text("Source aur Destination settings.", reply_markup=create_core_settings_keyboard(rule_id))
        elif menu_type == 'text':
            await query.edit_message_text("Text Filters, Lists, aur Replacement settings.", reply_markup=create_text_menu_keyboard(rule_id))
        elif menu_type == 'replacement':
            await query.edit_message_text("Text Replacement Niyam.", reply_markup=create_replacement_menu_keyboard(rule_id))
        elif menu_type == 'media':
            await query.edit_message_text("Media/Forward related filters.", reply_markup=create_media_filters_keyboard(rule))
        elif menu_type == 'advanced':
            await query.edit_message_text("Advanced actions (Caption, Silent, Prefix/Suffix).", reply_markup=create_advanced_actions_keyboard(rule))
        elif menu_type == 'prefix':
            keyboard = [[InlineKeyboardButton("📝 Prefix Set Karein", callback_data=f'set_prefix_{rule_id}')], [InlineKeyboardButton("📝 Suffix Set Karein", callback_data=f'set_suffix_{rule_id}')], [InlineKeyboardButton("⬅️ Piche Jaane", callback_data=f'menu_advanced_actions_{rule_id}')]]
            await query.edit_message_text("Message mein shuru aur ant mein text jodien.", reply_markup=InlineKeyboardMarkup(keyboard))
        elif menu_type == 'mode':
             keyboard = [
                [InlineKeyboardButton(f"1. Forward (Original) {'✅' if rule.FORWARDING_MODE == 'FORWARD' else '❌'}", callback_data=f'set_mode_forward_{rule_id}')],
                [InlineKeyboardButton(f"2. Copy (Full Control) {'✅' if rule.FORWARDING_MODE == 'COPY' else '❌'}", callback_data=f'set_mode_copy_{rule_id}')],
                [InlineKeyboardButton(f"⏰ Delay Set Karein ({rule.FORWARD_DELAY_SECONDS}s)", callback_data=f'menu_schedule_{rule_id}')],
                [InlineKeyboardButton("⬅️ Rule Settings", callback_data=f'select_rule_{rule_id}')]
            ]
             await query.edit_message_text("Message Forwarding ka **Mode** chunein. COPY mode hi zyadatar features (Replacement, Button Remove) ko support karta hai.", reply_markup=InlineKeyboardMarkup(keyboard))

        return ConversationHandler.END

    # --- Rule Specific Toggles and Actions ---
    elif any(data.startswith(f) for f in ['toggle_', 'set_']):
        parts = data.split('_')
        action = parts[0]
        setting = parts[1]
        rule_id = int(parts[-1])
        rule = get_rule_by_id(rule_id)
        
        if not rule:
            await query.edit_message_text("❌ Rule nahi mila!", reply_markup=create_back_keyboard())
            return ConversationHandler.END

        if action == 'toggle':
            attr = f"{setting.upper()}_{'_'.join(parts[2:-1]).upper()}" if len(parts) > 3 else setting.upper()
            
            # Special case for IS_ACTIVE
            if setting == 'active':
                setattr(rule, 'IS_ACTIVE', not rule.IS_ACTIVE)
            # Other Boolean Toggles (BLOCK_LINKS, REMOVE_BUTTONS, etc.)
            elif hasattr(rule, attr):
                 current_value = getattr(rule, attr)
                 setattr(rule, attr, not current_value)

            save_rule_to_db(rule)
            await query.edit_message_text(
                f"✅ **{attr.replace('_', ' ').title()}** updated.\n\n"
                f"Current Settings:\n{get_rule_settings_text(rule)}",
                reply_markup=create_back_keyboard(f'select_rule_{rule_id}'),
                parse_mode='Markdown'
            )
            return ConversationHandler.END
            
        elif action == 'set':
            if setting in ['source', 'destination']:
                context.user_data['current_rule_id'] = rule_id
                state = SET_SOURCE_ID if setting == 'source' else SET_DESTINATION_ID
                await query.edit_message_text(f"Kripya Rule **{rule.rule_name}** ke liye {setting.title()} Channel ka **ID** ya **Username** bhejein.", reply_markup=create_back_keyboard(f'menu_core_{rule_id}'))
                return state
                
            elif setting in ['prefix', 'suffix']:
                 context.user_data['current_rule_id'] = rule_id
                 context.user_data['prefix_or_suffix'] = setting.upper()
                 state = SET_PREFIX_TEXT if setting == 'prefix' else SET_SUFFIX_TEXT
                 await query.edit_message_text(f"Kripya Rule **{rule.rule_name}** ke liye **{setting.title()}** text bhejein (ya `clear` likhein hatane ke liye).", reply_markup=create_back_keyboard(f'menu_advanced_actions_{rule_id}'))
                 return state

            elif setting == 'mode':
                mode = parts[2].upper()
                rule.FORWARDING_MODE = mode
                save_rule_to_db(rule)
                await query.edit_message_text(
                    f"✅ **Forwarding Mode** ab `{mode}` set kar diya gaya hai.\n\n"
                    f"Current Settings:\n{get_rule_settings_text(rule)}",
                    reply_markup=create_back_keyboard(f'select_rule_{rule_id}'),
                    parse_mode='Markdown'
                )
                return ConversationHandler.END
            
    # --- Nested Menus: Schedule ---
    elif data.startswith('menu_schedule_'):
        rule_id = int(data.split('_')[2])
        keyboard = [
            [InlineKeyboardButton("0 Sec (Default)", callback_data=f'set_delay_0_{rule_id}'), InlineKeyboardButton("5 Sec", callback_data=f'set_delay_5_{rule_id}')],
            [InlineKeyboardButton("15 Sec", callback_data=f'set_delay_15_{rule_id}'), InlineKeyboardButton("30 Sec", callback_data=f'set_delay_30_{rule_id}')],
            [InlineKeyboardButton("60 Sec (1 Minute)", callback_data=f'set_delay_60_{rule_id}')],
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data=f'menu_mode_delay_{rule_id}')]
        ]
        await query.edit_message_text("Message Forward hone se pehle kitna **Delay** chahiye?", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
        
    elif data.startswith('set_delay_'):
        parts = data.split('_')
        delay = int(parts[2])
        rule_id = int(parts[3])
        rule = get_rule_by_id(rule_id)
        rule.FORWARD_DELAY_SECONDS = delay
        save_rule_to_db(rule)
        await query.edit_message_text(
            f"✅ **Rule {rule.rule_name}** ka Delay ab `{delay} seconds` set kar diya gaya hai.",
            reply_markup=create_back_keyboard(f'select_rule_{rule_id}'),
            parse_mode='Markdown'
        )
        return ConversationHandler.END
        
    # --- Text Replacement Management ---
    elif data.startswith('add_replacement_find_'):
        rule_id = int(data.split('_')[-1])
        context.user_data['current_rule_id'] = rule_id
        await query.edit_message_text("Vah **Text Bhejein** jise aap Message mein **Dhoondhna** chahte hain (Find Text).", reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}'))
        return SET_REPLACEMENT_FIND
    
    elif data.startswith('clear_replacements_'):
        rule_id = int(data.split('_')[-1])
        rule = get_rule_by_id(rule_id)
        rule.TEXT_REPLACEMENTS = {}
        save_rule_to_db(rule)
        await query.edit_message_text(f"✅ Rule **{rule.rule_name}** ke **Saare Text Replacement Niyam** hata diye gaye hain.", reply_markup=create_back_keyboard(f'select_rule_{rule_id}'))
        return ConversationHandler.END

    # New: Individual Replacement Deletion Flow
    elif data.startswith('delete_replacement_select_'):
        rule_id = int(data.split('_')[-1])
        rule = get_rule_by_id(rule_id)
        if not rule.TEXT_REPLACEMENTS:
            await query.edit_message_text("❌ Is Rule mein koi Replacement Niyam nahi hai.", reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}'))
            return ConversationHandler.END
        
        await query.edit_message_text("Kripya **Vah Niyam Chunein** jise aap hatana chahte hain:", reply_markup=create_individual_delete_keyboard(rule.TEXT_REPLACEMENTS, rule_id))
        return ConversationHandler.END

    elif data.startswith('delete_replacement_confirm_'):
        # Data format: delete_replacement_confirm_{rule_id}_{find_text}
        parts = data.split('_')
        rule_id = int(parts[3])
        find_text = '_'.join(parts[4:]) # Reconstruct the find_text
        
        rule = get_rule_by_id(rule_id)
        
        if find_text in rule.TEXT_REPLACEMENTS:
            del rule.TEXT_REPLACEMENTS[find_text]
            save_rule_to_db(rule)
            await query.edit_message_text(f"✅ Replacement Niyam: **'{find_text[:20]}...'** hata diya gaya hai.", reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}'))
        else:
            await query.edit_message_text("❌ Vah Niyam nahi mila.", reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}'))

        return ConversationHandler.END
        
    # --- Blacklist/Whitelist Management ---
    elif data.startswith('menu_blacklist_') or data.startswith('menu_whitelist_'):
        is_blacklist = data.startswith('menu_blacklist_')
        rule_id = int(data.split('_')[-1])
        rule = get_rule_by_id(rule_id)
        list_name = "Blacklist" if is_blacklist else "Whitelist"
        current_list = rule.WORD_BLACKLIST if is_blacklist else rule.WORD_WHITELIST
        
        keyboard = [
            [InlineKeyboardButton(f"➕ Shabdh {list_name} Karein", callback_data=f'add_{list_name.lower()}_word_{rule_id}')], 
            [InlineKeyboardButton(f"🗑️ Saare {list_name} Hatayein", callback_data=f'clear_{list_name.lower()}_{rule_id}')], 
            [InlineKeyboardButton("⬅️ Piche Jaane", callback_data=f'menu_text_{rule_id}')]
        ]
        
        await query.edit_message_text(
            f"**Word {list_name} Settings (Rule {rule.rule_name})**\n\n"
            f"Current {list_name} Words: {', '.join(current_list or []) or 'Koi nahi'}", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
        
    elif data.startswith('add_blacklist_word_') or data.startswith('add_whitelist_word_'):
        rule_id = int(data.split('_')[-1])
        list_type = data.split('_')[1]
        list_name = "Blacklist" if list_type == 'blacklist' else "Whitelist"
        
        context.user_data['current_rule_id'] = rule_id
        context.user_data['list_type'] = list_type
        
        state = SET_BLACKLIST_WORD if list_type == 'blacklist' else SET_WHITELIST_WORD
        await query.edit_message_text(
            f"Kripya vah **Shabdh** bhejein jise aap **{list_name}** karna chahte hain.", 
            reply_markup=create_back_keyboard(f'menu_{list_type}_{rule_id}')
        )
        return state

    elif data.startswith('clear_blacklist_') or data.startswith('clear_whitelist_'):
        rule_id = int(data.split('_')[-1])
        list_type = data.split('_')[1]
        rule = get_rule_by_id(rule_id)
        
        if list_type == 'blacklist':
            rule.WORD_BLACKLIST = []
        else:
            rule.WORD_WHITELIST = []
            
        save_rule_to_db(rule)
        list_name = "Blacklisted" if list_type == 'blacklist' else "Whitelisted"
        await query.edit_message_text(f"✅ Rule **{rule.rule_name}** ke **Saare {list_name} Shabdh** hata diye gaye hain.", reply_markup=create_back_keyboard(f'menu_text_{rule_id}'))
        return ConversationHandler.END
        
    return ConversationHandler.END

# 8. Conversation Handlers (For User Input)

async def set_rule_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Creates a new rule and returns to rule settings."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    rule_name = update.message.text.strip()
    if not rule_name:
        await update.message.reply_text("Rule ka naam khaali nahi ho sakta.", reply_markup=create_back_keyboard())
        return SET_RULE_NAME
        
    session = Session()
    try:
        new_rule = ForwardRule(rule_name=rule_name)
        session.add(new_rule)
        session.commit()
        session.expunge(new_rule)
        
        await update.message.reply_text(
            f"✅ Naya Rule **'{rule_name}'** safaltapoorvak bana diya gaya hai.",
            reply_markup=create_back_keyboard(f'select_rule_{new_rule.id}'),
            parse_mode='Markdown'
        )
        context.user_data['current_rule_id'] = new_rule.id
        return ConversationHandler.END
    except IntegrityError:
        # Handle unique constraint violation for rule_name
        await update.message.reply_text(f"❌ Error: Rule nahi bana paya. **'{rule_name}'** नाम पहले से ही मौजूद है।", reply_markup=create_back_keyboard())
        session.rollback()
        return SET_RULE_NAME
    except Exception as e:
        await update.message.reply_text(f"❌ Error: Rule nahi bana paya. ({e})", reply_markup=create_back_keyboard())
        session.close()
        return SET_RULE_NAME
        
async def handle_chat_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE, config_attr: str) -> int:
    """Utility function to handle receiving chat ID/Username."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    rule_id = context.user_data.pop('current_rule_id')
    rule = get_rule_by_id(rule_id)
    if not rule:
        await update.message.reply_text("❌ Rule nahi mila.", reply_markup=create_back_keyboard())
        return ConversationHandler.END

    chat_input = update.message.text.strip()
    # Allowing comma-separated list of IDs/Usernames
    if not any(item.startswith('-100') or item.startswith('@') or item.isdigit() for item in chat_input.split(',')):
         await update.message.reply_text("❌ Galat format! Kripya ID (-100...) ya Username (@...) ya comma-separated list bhejein.", reply_markup=create_back_keyboard(f'menu_core_{rule_id}'))
         return ConversationHandler.END

    setattr(rule, config_attr, chat_input)
    save_rule_to_db(rule)
    
    await update.message.reply_text(
        f"✅ Rule **{rule.rule_name}** ka **{config_attr.replace('_', ' ')}** safaltapoorvak `{chat_input}` set kar diya gaya hai.",
        reply_markup=create_back_keyboard(f'select_rule_{rule_id}'),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def set_source_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_chat_id_input(update, context, "SOURCE_CHAT_ID")

async def set_destination_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_chat_id_input(update, context, "DESTINATION_CHAT_ID")

async def set_list_word(update: Update, context: ContextTypes.DEFAULT_TYPE, list_name: str, state_to_return: int) -> int:
    """Adds a word to Blacklist or Whitelist."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    rule_id = context.user_data.pop('current_rule_id')
    list_type = context.user_data.pop('list_type')
    rule = get_rule_by_id(rule_id)
    if not rule: return ConversationHandler.END

    # Allow multiple words separated by comma
    words_input = [w.strip().lower() for w in update.message.text.split(',') if w.strip()]
    
    current_list = getattr(rule, list_name) or []
    words_added = 0
    
    for word in words_input:
        if word not in current_list:
            current_list.append(word)
            words_added += 1

    setattr(rule, list_name, current_list)
    save_rule_to_db(rule)
    
    if words_added > 0:
        msg = f"✅ **{words_added}** Shabdh safaltapoorvak **{list_name.split('_')[1]}** mein jod diye gaye hain."
    else:
        msg = f"❌ Koi naya Shabdh nahi joda gaya. Shayad sabhi pehle se hi **{list_name.split('_')[1]}** mein the."

    await update.message.reply_text(
        msg + f"\n\n{list_name.split('_')[1]}: {', '.join(current_list)}",
        reply_markup=create_back_keyboard(f'menu_text_{rule_id}'),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def set_blacklist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_list_word(update, context, "WORD_BLACKLIST", SET_BLACKLIST_WORD)

async def set_whitelist_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_list_word(update, context, "WORD_WHITELIST", SET_WHITELIST_WORD)

async def set_replacement_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the 'find' text and asks for the 'replace' text."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    context.user_data['find_text'] = update.message.text
    rule_id = context.user_data['current_rule_id']
    await update.message.reply_text(
        f"Ab vah **Text Bhejein** jiske saath aap '{context.user_data['find_text'][:20] + '...'}' ko **Badalna (Replace)** chahte hain (Replace Text).",
        reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}')
    )
    return SET_REPLACEMENT_REPLACE

async def set_replacement_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the 'replace' text and saves the replacement rule."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    find_text = context.user_data.pop('find_text')
    rule_id = context.user_data.pop('current_rule_id')
    replace_text = update.message.text
    
    rule = get_rule_by_id(rule_id)
    if not rule: return ConversationHandler.END
    
    replacements = rule.TEXT_REPLACEMENTS or {}
    replacements[find_text] = replace_text
    rule.TEXT_REPLACEMENTS = replacements
    save_rule_to_db(rule)
    
    await update.message.reply_text(
        f"✅ Rule **{rule.rule_name}** mein Naya Replacement Niyam safaltapoorvak set kiya gaya:\n"
        f"**Dhoondhein:** `{find_text[:20] + '...'}`\n"
        f"**Badlein:** `{replace_text[:20] + '...'}`",
        reply_markup=create_back_keyboard(f'menu_replacement_{rule_id}'),
        parse_mode='Markdown'
    )
    return ConversationHandler.END
    
async def set_prefix_suffix_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sets the Prefix or Suffix text."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END

    text = update.message.text
    rule_id = context.user_data.pop('current_rule_id')
    setting = context.user_data.pop('prefix_or_suffix') # PREFIX or SUFFIX
    
    rule = get_rule_by_id(rule_id)
    if not rule: return ConversationHandler.END

    if text.lower().strip() == 'clear':
        text = ""
    
    if setting == 'PREFIX':
        rule.PREFIX_TEXT = text
    else:
        rule.SUFFIX_TEXT = text
        
    save_rule_to_db(rule)

    action_text = "Hata diya gaya" if not text else "Set kiya gaya"
    await update.message.reply_text(
        f"✅ Rule **{rule.rule_name}** ka **{setting.title()}** text **{action_text}**.",
        reply_markup=create_back_keyboard(f'menu_advanced_actions_{rule_id}'),
        parse_mode='Markdown'
    )
    return ConversationHandler.END
    
# --- Custom Button Addition Conversation (No changes needed, kept as is) ---
async def set_message_for_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the message ID and Channel ID and asks for button data."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END

    message = update.message
    
    # Check if the text matches the format: [Channel_ID]:[Message_ID]
    match = re.match(r'(-100\d+):(\d+)', message.text.strip())
    if match:
        context.user_data['button_target_chat_id'] = match.group(1)
        context.user_data['button_target_message_id'] = int(match.group(2))
    else:
        await message.reply_text(
             "❌ Galat Format. Kripya Target Channel ID aur Message ID is format mein bhejein: `-1001234567890:54321`", 
             reply_markup=create_back_keyboard('add_button_menu')
         )
        return SET_MESSAGE_FOR_BUTTONS

    await message.reply_text(
        f"✅ Target Message ({context.user_data['button_target_chat_id']}:{context.user_data['button_target_message_id']}) mil gaya hai.\n\n"
        f"Ab **Button Data** is format mein bhejein:\n"
        f"`Button Text 1, URL 1 | Button Text 2, URL 2`\n\n"
        f"Example: `Visit Website, https://example.com | Join Channel, https://t.me/yourchannel`",
        reply_markup=create_back_keyboard('add_button_menu')
    )
    return SET_BUTTON_DATA

async def set_button_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes button data and modifies the message."""
    if not is_admin(update.message.chat_id): return ConversationHandler.END
    
    chat_id = context.user_data.pop('button_target_chat_id', None)
    message_id = context.user_data.pop('button_target_message_id', None)
    button_data = update.message.text.strip()
    
    if not chat_id or not message_id:
        await update.message.reply_text("❌ Target Message ka data nahi mila. Kripya shuru se koshish karein.", reply_markup=create_back_keyboard())
        return ConversationHandler.END
        
    # Parse button data
    keyboard = []
    try:
        button_rows = button_data.split('|')
        for row_data in button_rows:
            buttons_in_row = []
            parts = row_data.strip().split(',')
            if len(parts) >= 2:
                text = parts[0].strip()
                url = parts[1].strip()
                buttons_in_row.append(InlineKeyboardButton(text, url=url))
            
            if buttons_in_row:
                 keyboard.append(buttons_in_row)
            
        if not keyboard:
             raise ValueError("No valid buttons found.")

        # Edit the message
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        await update.message.reply_text(
            f"✅ Buttons safaltapoorvak Message ID **{message_id}** mein **{chat_id}** par **Jod** diye gaye hain!",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error adding buttons: {e}")
        await update.message.reply_text(
            f"❌ Buttons jodte samay Error: `{e}`. Kripya format check karein: `Button Text, URL | Button Text 2, URL 2`",
            reply_markup=create_back_keyboard(),
            parse_mode='Markdown'
        )
    
    return ConversationHandler.END

# 9. Core Forwarding Logic
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks and forwards messages based on configuration of all matching rules.
       FIXED: Handles various update types to avoid 'NoneType' error.
    """
    
    # FIX: Get the actual message object, handling various update types
    message = update.channel_post or update.message or update.edited_channel_post or update.edited_message
    
    if not message: 
        # If still None, it's an update type we don't need to process (e.g., chat member changes)
        return
    
    # --- Filter out Service Messages early (If service message has no text/caption, we check later for rule) ---
    if message.service and not message.text and not message.caption:
        pass # Let the rule-specific filter handle it below
    
    # Load all rules
    all_rules = get_all_rules()
    
    # Find rules where Source ID matches the incoming message's chat ID
    matching_rules = []
    source_id = str(message.chat.id) # This line is now safe because 'message' is guaranteed to be non-None
    
    chat_username_lower = str(message.chat.username).lower() if message.chat.username else None
    
    for rule in all_rules:
        if not rule.IS_ACTIVE: continue
        if not rule.DESTINATION_CHAT_ID: continue
        
        # Check if the source matches the rule's source_chat_id
        source_list = [s.strip() for s in (rule.SOURCE_CHAT_ID or "").split(',')]
        
        # Check against ID or username
        is_match = False
        for s in source_list:
            if s == source_id:
                is_match = True
                break
            # Check against username (case-insensitive)
            if s.startswith('@') and chat_username_lower == s.lstrip('@').lower():
                is_match = True
                break
        
        if is_match:
            matching_rules.append(rule)

    if not matching_rules: return

    for rule in matching_rules:
        # --- Rule-Specific Filters ---
        
        # Block Service Messages
        if rule.BLOCK_SERVICE_MESSAGES and message.service:
             continue
             
        # Block Forwards Filter
        if rule.BLOCK_FORWARDS and message.forward_date is not None:
            continue
            
        # Block Replies Filter
        if rule.BLOCK_REPLIES and message.reply_to_message is not None:
            continue
            
        # Get message text/caption
        text_to_process = message.text or message.caption or ""
        text_lower = text_to_process.lower()
        
        # Block Media Filter (Pure Media only)
        is_pure_media = bool(message.photo or message.video or message.document or message.audio or message.voice or message.sticker or message.animation) and not (message.text or message.caption)
        if rule.BLOCK_MEDIA and is_pure_media:
            continue
            
        # Existing Filters: Links, Usernames
        if rule.BLOCK_LINKS and ('http' in text_lower or 't.me' in text_lower): continue
        if rule.BLOCK_USERNAMES and re.search(r'@[a-zA-Z0-9_]+', text_lower): continue

        # Filters: Blacklist
        if rule.WORD_BLACKLIST:
            if any(word in text_lower for word in rule.WORD_BLACKLIST): continue

        # Filters: Whitelist (MUST contain at least one word)
        if rule.WORD_WHITELIST:
            if not any(word in text_lower for word in rule.WORD_WHITELIST): continue

        # --- Text Processing ---
        
        final_text = text_to_process
        text_modified = False 
        
        # Text Replacement Logic
        if rule.TEXT_REPLACEMENTS and final_text:
            for find, replace in rule.TEXT_REPLACEMENTS.items():
                if find in final_text:
                    final_text = final_text.replace(find, replace)
                    text_modified = True
                    
        # Prefix/Suffix Logic
        if rule.PREFIX_TEXT or rule.SUFFIX_TEXT:
            final_text = (rule.PREFIX_TEXT or "") + final_text + (rule.SUFFIX_TEXT or "")
            text_modified = True
            
        # --- Core Forwarding Action ---
        
        # Decide whether to use copy_message (COPY mode or any modification/action required)
        force_copy = (rule.FORWARDING_MODE == 'COPY') or text_modified or rule.REMOVE_BUTTONS or rule.REMOVE_CAPTION or rule.REMOVE_WEB_PREVIEW
        
        # Apply Delay
        if rule.FORWARD_DELAY_SECONDS > 0:
            time.sleep(rule.FORWARD_DELAY_SECONDS)
        
        dest_id = rule.DESTINATION_CHAT_ID
        
        # When using copy_message, MarkdownV2 is generally safer than relying on the original parse_mode
        # as text manipulations can break Markdown or HTML formatting.
        # We only use the original parse_mode if it's explicitly set AND no text modification occurred, 
        # but to keep it simple and robust after text modifications/prefixes, we stick to a safe mode.
        parse_mode_to_use = ParseMode.MARKDOWN_V2 # Safer default for text modifications.
        
        # Determine the text/caption to send
        caption_to_send = final_text
        
        if rule.REMOVE_CAPTION and (message.photo or message.video or message.document or message.animation or message.audio or message.voice):
            caption_to_send = None
        
        # Final check if text is empty after all processing/caption removal
        if not (message.photo or message.video or message.document or message.animation or message.audio or message.voice or message.sticker) and not (caption_to_send and caption_to_send.strip()):
            # If it was a pure text message and text became empty after replacement/prefix/suffix, skip it.
            if not (message.text or message.caption):
                continue
            
        try:
            if force_copy:
                # --- Case 1: Use copy_message (Most Flexible) ---
                
                # If the message is only text (no media)
                if message.text and not message.caption and not message.photo and not message.video:
                    if caption_to_send and caption_to_send.strip():
                         await context.bot.send_message(
                             chat_id=dest_id, 
                             text=caption_to_send, 
                             parse_mode=parse_mode_to_use, # Use the robust parse mode
                             disable_web_page_preview=rule.REMOVE_WEB_PREVIEW,
                             disable_notification=rule.SILENT_FORWARDING
                         )

                # Message has media
                elif message.photo or message.video or message.document or message.audio or message.voice or message.sticker or message.animation:
                    # Determine reply_markup
                    reply_markup_to_send = None
                    if not rule.REMOVE_BUTTONS:
                        # If we are in COPY mode and text was NOT modified, we might try to retain buttons.
                        # However, copy_message only retains buttons if they are explicitly passed.
                        # Since we are forcing copy for most features, we should only pass original buttons if REMOVE_BUTTONS is False.
                        reply_markup_to_send = message.reply_markup
                        
                    await context.bot.copy_message(
                        chat_id=dest_id, 
                        from_chat_id=message.chat.id, 
                        message_id=message.message_id, 
                        caption=caption_to_send if caption_to_send and caption_to_send.strip() else None,
                        parse_mode=parse_mode_to_use, # Use the robust parse mode
                        disable_web_page_preview=rule.REMOVE_WEB_PREVIEW,
                        disable_notification=rule.SILENT_FORWARDING,
                        reply_markup=reply_markup_to_send
                    )
                
            else:
                # --- Case 2: Use forward_message (Original Forwarding) ---
                # This only happens if: Mode is FORWARD AND no text/caption modification/action is set.
                
                await context.bot.forward_message(
                    chat_id=dest_id, 
                    from_chat_id=message.chat.id, 
                    message_id=message.message_id,
                    disable_notification=rule.SILENT_FORWARDING
                )

        except Exception as e:
            logger.error(f"Error copying/sending message for rule {rule.id}: {e}")
            if is_admin(FORCE_ADMIN_ID):
                 # Send a notification to the admin with the error details
                 await context.bot.send_message(
                     FORCE_ADMIN_ID, 
                     f"❌ Forwarding Error for Rule {rule.rule_name} (ID: {rule.id}): `{e}`\n"
                     f"Source: `{message.chat.id}` | Dest: `{dest_id}`",
                     parse_mode='Markdown'
                 )
            
# 10. Main Function 
def main() -> None:
    """Start the bot."""
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set. Bot cannot start.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # --- Conversation Handler for Rule and Action Settings ---
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_callback),
        ],
        states={
            SET_RULE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rule_name)],
            SET_SOURCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source_id)],
            SET_DESTINATION_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_destination_id)],
            SET_REPLACEMENT_FIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_replacement_find)],
            SET_REPLACEMENT_REPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_replacement_replace)],
            SET_BLACKLIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_blacklist_word)], 
            SET_WHITELIST_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_whitelist_word)],
            SET_PREFIX_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_prefix_suffix_text)],
            SET_SUFFIX_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_prefix_suffix_text)],
            SET_MESSAGE_FOR_BUTTONS: [MessageHandler(filters.ALL & ~filters.COMMAND, set_message_for_buttons)], 
            SET_BUTTON_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_button_data)],
        },
        fallbacks=[
            CallbackQueryHandler(handle_callback),
            CommandHandler("start", start)
        ],
        allow_reentry=True
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    # Ensure CallbackQueryHandler is also added for simple actions outside conversation
    application.add_handler(CallbackQueryHandler(handle_callback)) 
    
    # Message handler for core forwarding logic (listens to ALL messages/posts)
    # NOTE: filters.ALL will also handle edited_messages/channel_posts and service messages
    application.add_handler(MessageHandler(filters.ALL, forward_message)) 
    
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

