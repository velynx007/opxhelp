import os
import asyncio
from flask import Flask, request
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, MessageHandler, ChatJoinRequestHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

TOKEN = os.environ["BOT_TOKEN"]
app = Flask(__name__)

# ---------- small caps ----------
_N = "abcdefghijklmnopqrstuvwxyz"
_S = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
_MAP = str.maketrans(_N + _N.upper(), _S + _S)


def sc(text):
    return text.translate(_MAP)


def msg_text(title, body):
    return f"<b>{sc(title)}</b>\n<blockquote><b>{sc(body)}</b></blockquote>"


def btn(text, style=None, **kw):
    return InlineKeyboardButton(
        sc(text), api_kwargs={"style": style} if style else None, **kw
    )


def welcome_markup(username):
    return InlineKeyboardMarkup([
        [btn("add me to your group", "success",
             url=f"https://t.me/{username}?startgroup=true")],
        [btn("how it works", "primary", callback_data="help")],
    ])


WELCOME = msg_text(
    "welcome",
    "i keep your group clean and distraction free.\n"
    "add me as an admin and i will take care of the rest.",
)
HELP = msg_text(
    "how it works",
    "• removes join and left messages\n"
    "• removes voice chat start and end alerts\n"
    "• removes ownership transfer notices\n"
    "• approves join requests automatically\n"
    "• deletes links from members\n"
    "• make me admin with delete messages and invite users rights",
)


async def welcome_dm(bot, chat_id):
    try:
        await bot.send_message(
            chat_id, WELCOME, parse_mode=ParseMode.HTML,
            reply_markup=welcome_markup(bot.username),
        )
    except Exception:
        pass


async def is_admin(bot, chat_id, user_id):
    m = await bot.get_chat_member(chat_id, user_id)
    return m.status in ("administrator", "creator")


async def clean_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.new_chat_members:
        for u in msg.new_chat_members:
            if not u.is_bot:
                await welcome_dm(context.bot, u.id)
    try:
        await msg.delete()
    except Exception:
        pass


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    try:
        await req.approve()
    except Exception:
        return
    await welcome_dm(context.bot, req.user_chat_id)


async def link_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.sender_chat or not msg.from_user:
        return
    if await is_admin(context.bot, msg.chat_id, msg.from_user.id):
        return
    try:
        await msg.delete()
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await welcome_dm(context.bot, update.effective_chat.id)


async def help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(HELP, parse_mode=ParseMode.HTML)


# ---------- build once, reuse on every request ----------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

application = Application.builder().token(TOKEN).updater(None).build()
application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
application.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
application.add_handler(ChatJoinRequestHandler(join_request))
application.add_handler(MessageHandler(
    filters.ChatType.GROUPS & filters.StatusUpdate.ALL, clean_service))
_link = (
    filters.Entity(MessageEntity.URL) | filters.Entity(MessageEntity.TEXT_LINK)
    | filters.CaptionEntity(MessageEntity.URL)
    | filters.CaptionEntity(MessageEntity.TEXT_LINK)
)
application.add_handler(MessageHandler(
    filters.ChatType.GROUPS & _link & ~filters.StatusUpdate.ALL, link_remover))
loop.run_until_complete(application.initialize())


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def webhook(path):
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        loop.run_until_complete(application.process_update(update))
    return "ok"
