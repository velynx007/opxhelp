import os
import time
import asyncio
import traceback
from flask import Flask, request
from pymongo import MongoClient
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, MessageHandler, ChatJoinRequestHandler,
    CommandHandler, CallbackQueryHandler, TypeHandler, ContextTypes, filters,
)

TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
OWNER_ID = int(os.environ.get("OWNER_ID", "8576582616"))
UPDATES_URL = "https://t.me/xi5nxu"
DEV_URL = "https://t.me/Velynx007"
HTML = ParseMode.HTML
BUDGET = 35  # seconds of sending per batch (vercel limit is 60)

app = Flask(__name__)
D = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)["opxhelp"]

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


async def db(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)


# ---------- settings (cached 20s) ----------
DEFAULT = {"clean": True, "links": True, "approve": True, "welcome": True}
LABELS = {
    "clean": "service msg cleaner",
    "links": "link remover",
    "approve": "auto approve",
    "welcome": "welcome dm",
}
_cfg = {"t": 0, "v": dict(DEFAULT)}


async def cfg():
    if time.time() - _cfg["t"] < 20:
        return _cfg["v"]
    d = await db(D.settings.find_one, {"_id": "cfg"}) or {}
    v = {k: d.get(k, DEFAULT[k]) for k in DEFAULT}
    _cfg.update(t=time.time(), v=v)
    return v


async def toggle(key):
    v = dict(await cfg())
    v[key] = not v[key]
    await db(D.settings.update_one, {"_id": "cfg"}, {"$set": {key: v[key]}}, upsert=True)
    _cfg.update(t=time.time(), v=v)


async def is_sudo(uid):
    return uid == OWNER_ID or bool(await db(D.sudo.find_one, {"_id": uid}))


# ---------- texts ----------
WELCOME = msg_text(
    "welcome",
    "i keep your group clean and distraction free.\n"
    "add me as an admin, then send /help to see every command.",
)
HELP = msg_text(
    "how it works",
    "• removes join and left messages\n"
    "• removes voice chat and ownership transfer notices\n"
    "• approves join requests automatically\n"
    "• deletes links sent by members\n"
    "• make me admin with delete messages and invite users rights",
)
PANEL = msg_text("admin panel", "control your bot from here. choose an option below.")


def welcome_markup(username):
    return InlineKeyboardMarkup([
        [btn("add me to your group", "success",
             url=f"https://t.me/{username}?startgroup=true")],
        [btn("all commands", "primary", callback_data="help")],
        [btn("updates", "primary", url=UPDATES_URL),
         btn("developer", "success", url=DEV_URL)],
    ])


def panel_markup():
    return InlineKeyboardMarkup([
        [btn("stats", "primary", callback_data="a:stats"),
         btn("broadcast", "success", callback_data="a:bc")],
        [btn("settings", "primary", callback_data="a:set"),
         btn("manage", "primary", callback_data="a:mng")],
        [btn("close", "danger", callback_data="a:close")],
    ])


BACK = InlineKeyboardMarkup([[btn("back", "primary", callback_data="a:home")]])


async def welcome_dm(bot, chat_id, force=False):
    if not force and not (await cfg())["welcome"]:
        return
    try:
        await bot.send_message(
            chat_id, WELCOME, parse_mode=HTML,
            reply_markup=welcome_markup(bot.username),
        )
    except Exception:
        pass


async def say(update, title, body):
    await update.effective_message.reply_text(msg_text(title, body), parse_mode=HTML)


async def edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
    except BadRequest:
        pass


# ---------- tracking users and chats ----------
_seen = set()


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u, c = update.effective_user, update.effective_chat
    if not c:
        return
    if c.type == "private":
        if u and u.id != OWNER_ID and await db(D.banned.find_one, {"_id": u.id}):
            raise ApplicationHandlerStop
        if u and u.id not in _seen:
            _seen.add(u.id)
            await db(D.users.update_one, {"_id": u.id},
                     {"$set": {"name": u.full_name, "username": u.username},
                      "$setOnInsert": {"ts": int(time.time())}}, upsert=True)
    elif c.id not in _seen:
        _seen.add(c.id)
        await db(D.chats.update_one, {"_id": c.id},
                 {"$set": {"title": c.title, "type": c.type}}, upsert=True)


# ---------- group features ----------
class _OwnerSvc(filters.MessageFilter):
    def filter(self, message):
        k = message.api_kwargs or {}
        return "chat_owner_left" in k or "chat_owner_changed" in k


OWNER_SVC = _OwnerSvc()


async def is_admin(bot, chat_id, user_id):
    m = await bot.get_chat_member(chat_id, user_id)
    return m.status in ("administrator", "creator")


async def clean_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.new_chat_members:
        for u in msg.new_chat_members:
            if not u.is_bot:
                await welcome_dm(context.bot, u.id)
    if (await cfg())["clean"]:
        try:
            await msg.delete()
        except Exception:
            pass


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (await cfg())["approve"]:
        return
    req = update.chat_join_request
    try:
        await req.approve()
    except Exception:
        return
    await welcome_dm(context.bot, req.user_chat_id)


async def link_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return  # handled by _opx
    msg = update.effective_message
    if not msg or msg.sender_chat or not msg.from_user:
        return
    if not (await cfg())["links"]:
        return
    if await is_admin(context.bot, msg.chat_id, msg.from_user.id):
        return
    try:
        await msg.delete()
    except Exception:
        pass


# ---------- user commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await welcome_dm(context.bot, update.effective_chat.id, force=True)


async def help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(HELP, parse_mode=HTML)


# ---------- admin commands ----------
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_sudo(update.effective_user.id):
        return
    await update.message.reply_text(PANEL, parse_mode=HTML, reply_markup=panel_markup())


def _target(update, context):
    if context.args and context.args[0].lstrip("-").isdigit():
        return int(context.args[0])
    r = update.message.reply_to_message
    if r and r.from_user:
        return r.from_user.id
    return None


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_sudo(update.effective_user.id):
        return
    t = _target(update, context)
    if not t:
        return await say(update, "usage", "/ban user id")
    if await is_sudo(t):
        return await say(update, "denied", "you cannot ban an admin.")
    await db(D.banned.update_one, {"_id": t}, {"$set": {"ts": int(time.time())}}, upsert=True)
    await say(update, "banned", f"{t} can no longer use the bot.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_sudo(update.effective_user.id):
        return
    t = _target(update, context)
    if not t:
        return await say(update, "usage", "/unban user id")
    await db(D.banned.delete_one, {"_id": t})
    await say(update, "unbanned", f"{t} can use the bot again.")


async def _sudo_change(update, context, add):
    if update.effective_user.id != OWNER_ID:
        return
    t = _target(update, context)
    if not t:
        return await say(update, "usage", "/addsudo user id" if add else "/rmsudo user id")
    if add:
        await db(D.sudo.update_one, {"_id": t}, {"$set": {"ts": int(time.time())}}, upsert=True)
        await say(update, "sudo added", f"{t} is now a sudo admin.")
    else:
        await db(D.sudo.delete_one, {"_id": t})
        await say(update, "sudo removed", f"{t} is no longer a sudo admin.")


async def addsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _sudo_change(update, context, True)


async def rmsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _sudo_change(update, context, False)


async def sudolist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_sudo(update.effective_user.id):
        return
    rows = await db(lambda: [d["_id"] for d in D.sudo.find({})])
    body = f"owner : {OWNER_ID}\n" + "\n".join(f"sudo : {i}" for i in rows)
    await say(update, "admins", body)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_sudo(update.effective_user.id):
        return
    await db(D.state.delete_one, {"_id": update.effective_user.id})
    await say(update, "cancelled", "broadcast setup cleared.")


# ---------- broadcast ----------
TARGETS = {"users": "users", "chats": "groups", "all": "users and groups"}


async def target_ids(t):
    ids = []
    if t in ("users", "all"):
        ids += await db(lambda: [d["_id"] for d in D.users.find({}, {"_id": 1}).sort("_id", 1)])
    if t in ("chats", "all"):
        ids += await db(lambda: [d["_id"] for d in D.chats.find({}, {"_id": 1}).sort("_id", 1)])
    return ids


def confirm_text(st, n):
    copy = st["mode"] == "copy"
    return msg_text(
        "confirm broadcast",
        f"target : {TARGETS[st['target']]}\n"
        f"recipients : {n}\n"
        f"mode : {'copy, no forward tag' if copy else 'forward, with tag'}\n"
        f"pin in groups : {'on' if st['pin'] else 'off'}",
    )


def confirm_markup(st):
    return InlineKeyboardMarkup([
        [btn(f"mode : {'copy' if st['mode'] == 'copy' else 'forward'}", "primary", callback_data="b:mode"),
         btn(f"pin : {'on' if st['pin'] else 'off'}", "success" if st["pin"] else "danger", callback_data="b:pin")],
        [btn("send now", "success", callback_data="b:go"),
         btn("cancel", "danger", callback_data="b:x")],
    ])


async def admin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    st = await db(D.state.find_one, {"_id": uid})
    if not st or st.get("step") != "await" or not await is_sudo(uid):
        return
    st.update(step="confirm", src_chat=msg.chat_id, src_msg=msg.message_id)
    await db(D.state.replace_one, {"_id": uid}, st)
    n = len(await target_ids(st["target"]))
    await msg.reply_text(confirm_text(st, n), parse_mode=HTML, reply_markup=confirm_markup(st))


async def send_one(bot, cid, st):
    for _ in range(2):
        try:
            if st["mode"] == "fwd":
                m = await bot.forward_message(cid, st["src_chat"], st["src_msg"])
            else:
                m = await bot.copy_message(cid, st["src_chat"], st["src_msg"])
            if st["pin"] and cid < 0:
                try:
                    await bot.pin_chat_message(cid, m.message_id)
                except TelegramError:
                    pass
            return "ok"
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 10) + 1)
        except Forbidden:
            return "dead"
        except BadRequest as e:
            return "dead" if "chat not found" in str(e).lower() else "fail"
        except TelegramError:
            return "fail"
    return "fail"


async def run_job(q, context, uid):
    st = await db(D.state.find_one, {"_id": uid})
    if not st or st.get("step") not in ("confirm", "running"):
        return await edit(q, msg_text("broadcast", "nothing to send. start again from the panel."), BACK)
    ids = await target_ids(st["target"])
    total = len(ids)
    i = st.get("cursor", 0)
    sent, failed, dead = st.get("sent", 0), st.get("failed", 0), st.get("dead", 0)
    dead_ids = []
    await db(D.state.update_one, {"_id": uid}, {"$set": {"step": "running"}})
    t0 = time.time()
    while i < total and time.time() - t0 < BUDGET:
        chunk = ids[i:i + 20]
        res = await asyncio.gather(*(send_one(context.bot, c, st) for c in chunk))
        for c, r in zip(chunk, res):
            if r == "ok":
                sent += 1
            elif r == "dead":
                dead += 1
                dead_ids.append(c)
            else:
                failed += 1
        i += len(chunk)
        await asyncio.sleep(0.7)
    us = [x for x in dead_ids if x > 0]
    cs = [x for x in dead_ids if x < 0]
    if us:
        await db(D.users.delete_many, {"_id": {"$in": us}})
    if cs:
        await db(D.chats.delete_many, {"_id": {"$in": cs}})
    if i >= total:
        await db(D.state.delete_one, {"_id": uid})
        await edit(q, msg_text(
            "broadcast done",
            f"total : {total}\nsent : {sent}\nfailed : {failed}\nremoved dead chats : {dead}"), BACK)
    else:
        await db(D.state.update_one, {"_id": uid},
                 {"$set": {"cursor": i, "sent": sent, "failed": failed, "dead": dead}})
        await edit(q, msg_text(
            "broadcast paused",
            f"progress : {i} of {total}\nsent : {sent}\nfailed : {failed}\n"
            "tap continue to send the next batch."),
            InlineKeyboardMarkup([[btn("continue", "success", callback_data="b:go"),
                                   btn("stop", "danger", callback_data="b:x")]]))


# ---------- admin callbacks ----------
async def settings_view():
    c = await cfg()
    rows = [[btn(f"{LABELS[k]} : {'on' if c[k] else 'off'}",
                 "success" if c[k] else "danger", callback_data=f"t:{k}")] for k in DEFAULT]
    rows.append([btn("back", "primary", callback_data="a:home")])
    return (msg_text("settings", "tap a switch to turn it on or off. it applies to every group."),
            InlineKeyboardMarkup(rows))


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await is_sudo(uid):
        return await q.answer("not allowed", show_alert=True)
    await q.answer()
    d = q.data.split(":")
    kind, act = d[0], d[1]

    if kind == "t":
        await toggle(act)
        text, mk = await settings_view()
        return await edit(q, text, mk)

    if kind == "a":
        if act == "home":
            await edit(q, PANEL, panel_markup())
        elif act == "close":
            await q.message.delete()
        elif act == "stats":
            nu = await db(D.users.count_documents, {})
            ng = await db(D.chats.count_documents, {})
            nb = await db(D.banned.count_documents, {})
            ns = await db(D.sudo.count_documents, {})
            await edit(q, msg_text("stats", f"users : {nu}\ngroups : {ng}\nbanned : {nb}\nsudo admins : {ns}"), BACK)
        elif act == "set":
            text, mk = await settings_view()
            await edit(q, text, mk)
        elif act == "mng":
            rows = [
                ("/ban id", "block a user from the bot"),
                ("/unban id", "unblock a user"),
                ("/addsudo id", "owner only. add a sudo admin"),
                ("/rmsudo id", "owner only. remove a sudo admin"),
                ("/sudolist", "show all admins"),
                ("/cancel", "clear broadcast setup"),
            ]
            body = "\n\n".join(f"<code>{c}</code>\n<b>{sc(t)}</b>" for c, t in rows)
            await edit(q, f"<b>{sc('manage')}</b>\n<blockquote>{body}</blockquote>", BACK)
        elif act == "bc":
            await edit(q, msg_text("broadcast", "who should receive it?"), InlineKeyboardMarkup([
                [btn("users", "primary", callback_data="b:t:users"),
                 btn("groups", "primary", callback_data="b:t:chats")],
                [btn("users and groups", "success", callback_data="b:t:all")],
                [btn("back", "danger", callback_data="a:home")],
            ]))
        return

    # kind == "b"
    if act == "t":
        st = {"_id": uid, "target": d[2], "step": "await", "mode": "copy", "pin": False}
        await db(D.state.replace_one, {"_id": uid}, st, upsert=True)
        return await edit(q, msg_text(
            "send your message",
            "send the message you want to broadcast now. text, photo, video, file, voice, sticker or poll, anything works."),
            InlineKeyboardMarkup([[btn("cancel", "danger", callback_data="b:x")]]))
    if act == "x":
        await db(D.state.delete_one, {"_id": uid})
        return await edit(q, msg_text("cancelled", "broadcast cancelled."), BACK)
    if act == "go":
        return await run_job(q, context, uid)
    if act in ("mode", "pin"):
        st = await db(D.state.find_one, {"_id": uid})
        if not st or st.get("step") != "confirm":
            return await edit(q, msg_text("expired", "start again from the panel."), BACK)
        if act == "mode":
            st["mode"] = "fwd" if st["mode"] == "copy" else "copy"
        else:
            st["pin"] = not st["pin"]
        await db(D.state.replace_one, {"_id": uid}, st)
        n = len(await target_ids(st["target"]))
        await edit(q, confirm_text(st, n), confirm_markup(st))


# ---------- purge ----------
class _QuizBot(filters.MessageFilter):
    def filter(self, message):
        v, f = message.via_bot, message.from_user
        return bool(
            (v and (v.username or "").lower() == "quizbot")
            or (f and f.is_bot and (f.username or "").lower() == "quizbot")
        )


_keep_ix = []


async def keep_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not _keep_ix:
        _keep_ix.append(1)
        try:
            await db(D.keep.create_index, "ts", expireAfterSeconds=172800)
        except Exception:
            pass
    await db(D.keep.update_one, {"c": m.chat_id, "m": m.message_id},
             {"$set": {"ts": int(time.time())}}, upsert=True)


async def _del(bot, chat_id, ids):
    for _ in range(2):
        try:
            await bot.delete_messages(chat_id, ids)
            return
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 10) + 1)
        except TelegramError:
            return


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u, bot = update.effective_message, update.effective_user, context.bot
    if not u or msg.sender_chat:
        return
    ok = await is_sudo(u.id)
    if not ok:
        mem = await bot.get_chat_member(msg.chat_id, u.id)
        ok = mem.status == "creator" or (
            mem.status == "administrator" and mem.can_delete_messages)
    if not ok:
        return
    kw = {"message_thread_id": msg.message_thread_id} if msg.is_topic_message else {}
    r = msg.reply_to_message
    if not r:
        m = await bot.send_message(
            msg.chat_id,
            msg_text("purge", "reply to the message you want to delete from."),
            parse_mode=HTML, **kw)
        await asyncio.sleep(4)
        await _del(bot, msg.chat_id, [m.message_id, msg.message_id])
        return
    end = msg.message_id
    start = max(r.message_id, end - 3000)
    kept = await db(lambda: {d["m"] for d in D.keep.find(
        {"c": msg.chat_id, "m": {"$gte": start, "$lte": end}}, {"m": 1})})
    ids = [i for i in range(start, end + 1) if i not in kept]
    chunks = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    for i in range(0, len(chunks), 5):
        await asyncio.gather(*(_del(bot, msg.chat_id, c) for c in chunks[i:i + 5]))
    done = await bot.send_message(
        msg.chat_id,
        msg_text("purge complete", f"cleaned up to {len(ids)} messages. quiz bot messages were kept."),
        parse_mode=HTML, **kw)
    await asyncio.sleep(4)
    await _del(bot, msg.chat_id, [done.message_id])


# ---------- build once, reuse on every request ----------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

application = Application.builder().token(TOKEN).updater(None).build()
application.add_handler(TypeHandler(Update, track), group=-1)
PRIV = filters.ChatType.PRIVATE
application.add_handler(CommandHandler("start", start, PRIV))
application.add_handler(CommandHandler("admin", admin_cmd, PRIV))
application.add_handler(CommandHandler("ban", ban_cmd, PRIV))
application.add_handler(CommandHandler("unban", unban_cmd, PRIV))
application.add_handler(CommandHandler("addsudo", addsudo_cmd, PRIV))
application.add_handler(CommandHandler("rmsudo", rmsudo_cmd, PRIV))
application.add_handler(CommandHandler("sudolist", sudolist_cmd, PRIV))
application.add_handler(CommandHandler("cancel", cancel_cmd, PRIV))
application.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
application.add_handler(CallbackQueryHandler(admin_cb, pattern="^(a|t|b):"))
application.add_handler(ChatJoinRequestHandler(join_request))
application.add_handler(MessageHandler(PRIV & ~filters.COMMAND, admin_msg))
application.add_handler(MessageHandler(
    filters.ChatType.GROUPS & (filters.StatusUpdate.ALL | OWNER_SVC), clean_service))
_link = (
    filters.Entity(MessageEntity.URL) | filters.Entity(MessageEntity.TEXT_LINK)
    | filters.CaptionEntity(MessageEntity.URL)
    | filters.CaptionEntity(MessageEntity.TEXT_LINK)
)
application.add_handler(MessageHandler(
    filters.ChatType.GROUPS & _link & ~filters.StatusUpdate.ALL & ~OWNER_SVC, link_remover))
application.add_handler(CommandHandler("purge", purge_cmd, filters.ChatType.GROUPS))
application.add_handler(MessageHandler(filters.ChatType.GROUPS & _QuizBot(), keep_quiz), group=2)
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _opx import register
register(application, D, welcome_dm)
loop.run_until_complete(application.initialize())


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def webhook(path):
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), application.bot)
            loop.run_until_complete(application.process_update(update))
        except Exception:
            traceback.print_exc()
    return "ok"
