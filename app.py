import os
import logging
import asyncio
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaDocument, InputMediaVideo
)
from telegram.ext import (
    Application, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
from sqlalchemy import create_engine, Column, Integer, String, Boolean, PickleType
from sqlalchemy.orm import sessionmaker, declarative_base

# ---------------------- Logging ----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------- Database Setup ----------------------
Base = declarative_base()

class Rule(Base):
    __tablename__ = "rules"
    id = Column(Integer, primary_key=True)
    source = Column(String)
    destination = Column(String)
    header = Column(String, default="")
    footer = Column(String, default="")
    replacements = Column(PickleType, default={})
    blacklist = Column(PickleType, default=[])
    whitelist = Column(PickleType, default=[])
    active = Column(Boolean, default=True)
    delay = Column(Integer, default=0)

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///rules.db")
engine = create_engine(DB_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ---------------------- State Definitions ----------------------
(
    MENU, ADD_SOURCE, ADD_DEST, SET_HEADER, SET_FOOTER, ADD_REPLACE_KEY, ADD_REPLACE_VALUE,
    SET_BLACKLIST, SET_WHITELIST, SET_DELAY
) = range(10)

# ---------------------- Start Handler ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Rule", callback_data="add_rule")],
        [InlineKeyboardButton("📜 View Rules", callback_data="view_rules")],
        [InlineKeyboardButton("▶ Start Forwarding", callback_data="start_forward")],
        [InlineKeyboardButton("⏹ Stop Forwarding", callback_data="stop_forward")]
    ]
    await update.message.reply_text("🤖 *Auto Forward Bot Menu:*", parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return MENU

# ---------------------- Add Rule Flow ----------------------
async def add_rule_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Send Source Channel ID (e.g., -100123456789):")
    return ADD_SOURCE

async def add_rule_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["source"] = update.message.text.strip()
    await update.message.reply_text("Now send Destination Channel ID:")
    return ADD_DEST

async def add_rule_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["destination"] = update.message.text.strip()
    await update.message.reply_text("Optional: Send header text (or type 'skip'):")
    return SET_HEADER

async def set_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    header = update.message.text
    context.user_data["header"] = "" if header.lower() == "skip" else header
    await update.message.reply_text("Optional: Send footer text (or type 'skip'):")
    return SET_FOOTER

async def set_footer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    footer = update.message.text
    context.user_data["footer"] = "" if footer.lower() == "skip" else footer
    await update.message.reply_text("Add a word to blacklist (or type 'skip'):")
    return SET_BLACKLIST

async def set_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    blacklist = [] if text.lower() == "skip" else [w.strip() for w in text.split(",")]
    context.user_data["blacklist"] = blacklist
    await update.message.reply_text("Add a word to whitelist (or type 'skip'):")
    return SET_WHITELIST

async def set_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    whitelist = [] if text.lower() == "skip" else [w.strip() for w in text.split(",")]
    context.user_data["whitelist"] = whitelist
    await update.message.reply_text("Set forwarding delay in seconds (e.g., 3):")
    return SET_DELAY

async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text)
    except:
        delay = 0
    context.user_data["delay"] = delay

    rule = Rule(
        source=context.user_data["source"],
        destination=context.user_data["destination"],
        header=context.user_data["header"],
        footer=context.user_data["footer"],
        blacklist=context.user_data["blacklist"],
        whitelist=context.user_data["whitelist"],
        delay=context.user_data["delay"]
    )
    session = Session()
    session.add(rule)
    session.commit()
    session.close()
    await update.message.reply_text("✅ New rule saved successfully!")
    return MENU

# ---------------------- View Rules ----------------------
async def view_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    session = Session()
    rules = session.query(Rule).all()
    session.close()

    if not rules:
        await update.callback_query.edit_message_text("No rules found yet.")
        return MENU

    text = "📜 *Current Rules:*\n\n"
    for r in rules:
        text += (f"🆔 {r.id} | From `{r.source}` → `{r.destination}`\n"
                 f"Header: {r.header or 'None'}\nFooter: {r.footer or 'None'}\n"
                 f"Blacklist: {r.blacklist}\nWhitelist: {r.whitelist}\nDelay: {r.delay}s\nStatus: {'✅ Active' if r.active else '❌ Inactive'}\n\n")

    await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    return MENU

# ---------------------- Forwarding Core ----------------------
async def forward_message(context: ContextTypes.DEFAULT_TYPE, rule: Rule, message):
    text = message.text or message.caption or ""
    if any(b.lower() in text.lower() for b in rule.blacklist):
        return
    if rule.whitelist and not any(w.lower() in text.lower() for w in rule.whitelist):
        return

    for old, new in rule.replacements.items():
        text = text.replace(old, new)

    final_text = f"{rule.header}\n{text}\n{rule.footer}".strip()

    try:
        if message.photo:
            await context.bot.send_photo(chat_id=rule.destination, photo=message.photo[-1].file_id, caption=final_text)
        elif message.video:
            await context.bot.send_video(chat_id=rule.destination, video=message.video.file_id, caption=final_text)
        elif message.document:
            await context.bot.send_document(chat_id=rule.destination, document=message.document.file_id, caption=final_text)
        else:
            await context.bot.send_message(chat_id=rule.destination, text=final_text)
    except Exception as e:
        logger.error(f"Forwarding failed: {e}")

# ---------------------- Forward Loop ----------------------
forwarding_active = False

async def toggle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global forwarding_active
    query = update.callback_query
    await query.answer()

    if query.data == "start_forward":
        forwarding_active = True
        await query.edit_message_text("✅ Forwarding started!")
    elif query.data == "stop_forward":
        forwarding_active = False
        await query.edit_message_text("⏹ Forwarding stopped.")

    session = Session()
    rules = session.query(Rule).all()
    session.close()

    if forwarding_active:
        asyncio.create_task(forward_loop(context, rules))

async def forward_loop(context: ContextTypes.DEFAULT_TYPE, rules):
    while forwarding_active:
        for rule in rules:
            if not rule.active:
                continue
            try:
                updates = await context.bot.get_updates(timeout=5)
                for update in updates:
                    if update.message and update.message.chat.id == int(rule.source):
                        await forward_message(context, rule, update.message)
                        await asyncio.sleep(rule.delay)
            except Exception as e:
                logger.warning(f"Loop error: {e}")
        await asyncio.sleep(2)

# ---------------------- Main Function ----------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌ Please set BOT_TOKEN environment variable.")
        return

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_rule_entry, pattern="^add_rule$")],
        states={
            ADD_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rule_source)],
            ADD_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rule_dest)],
            SET_HEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_header)],
            SET_FOOTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_footer)],
            SET_BLACKLIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_blacklist)],
            SET_WHITELIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_whitelist)],
            SET_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_delay)],
        },
        fallbacks=[],
    )

    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex("^/start$"), start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(view_rules, pattern="^view_rules$"))
    app.add_handler(CallbackQueryHandler(toggle_forward, pattern="^(start_forward|stop_forward)$"))

    print("🚀 Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
