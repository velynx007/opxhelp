import os
import re
import html
import time
import asyncio
from telegram import InlineKeyboardButton, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import TelegramError

D = None  # set by register()
HTML = ParseMode.HTML
OWNER_ID = int(os.environ.get("OWNER_ID", "8576582616"))
OUR = set()  # command names handled by this package

_N = "abcdefghijklmnopqrstuvwxyz"
_S = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
_MAP = str.maketrans(_N + _N.upper(), _S + _S)


def sc(t):
    return str(t).translate(_MAP)


def esc(t):
    return html.escape(str(t if t is not None else ""))


def card(title, body_html):
    return f"<b>{sc(title)}</b>\n<blockquote><b>{body_html}</b></blockquote>"


def msg_text(title, body):
    return card(title, sc(body))


def ment(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name) or uid}</a>'


def btn(text, style=None, **kw):
    return InlineKeyboardButton(
        sc(text), api_kwargs={"style": style} if style else None, **kw)


def err(e):
    return esc(sc(str(e)[:90]))


def T(name):
    return D[name]


async def db(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)


def parse_time(s):
    m = re.fullmatch(r"(\d+)([smhdw])", (s or "").lower())
    if not m:
        return None
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]


# ---------- global switches (owner panel) ----------
_g = {"t": 0, "v": {}}


async def gflag(key):
    if time.time() - _g["t"] > 20:
        d = await db(T("settings").find_one, {"_id": "cfg"}) or {}
        _g.update(t=time.time(), v=d)
    return _g["v"].get(key, True)


# ---------- per group settings ----------
GDEF = {
    "clean_join": True, "clean_left": True, "clean_vc": True, "clean_pin": True,
    "clean_title": True, "clean_owner": True, "clean_other": True,
    "approve": True, "link_mode": "delete", "flood": 0, "flood_action": "mute",
    "antibot": False, "antichannel": False, "cleancmd": False, "captcha": False,
    "warn_limit": 3, "warn_action": "mute", "locks": [], "slow": 0, "rules": "",
}
_gc = {}


async def gcfg(cid):
    c = _gc.get(cid)
    if c and time.time() - c[0] < 20:
        return c[1]
    d = await db(T("gset").find_one, {"_id": cid}) or {}
    v = {k: d.get(k, GDEF[k]) for k in GDEF}
    _gc[cid] = (time.time(), v)
    return v


async def gset(cid, **kw):
    await db(T("gset").update_one, {"_id": cid}, {"$set": kw}, upsert=True)
    _gc.pop(cid, None)


# ---------- sudo / admin checks ----------
_sd = {"t": 0, "s": set()}


async def is_sudo(uid):
    if uid == OWNER_ID:
        return True
    if time.time() - _sd["t"] > 60:
        s = await db(lambda: {d["_id"] for d in T("sudo").find({}, {"_id": 1})})
        _sd.update(t=time.time(), s=s)
    return uid in _sd["s"]


_adm = {}


async def is_adm(bot, cid, uid):
    k = (cid, uid)
    c = _adm.get(k)
    if c and time.time() - c[0] < 60:
        return c[1]
    try:
        m = await bot.get_chat_member(cid, uid)
        r = m.status in ("administrator", "creator")
    except TelegramError:
        r = False
    if len(_adm) > 5000:
        _adm.clear()
    _adm[k] = (time.time(), r)
    return r


async def has_right(bot, cid, uid, right=None):
    if await is_sudo(uid):
        return True
    try:
        mem = await bot.get_chat_member(cid, uid)
    except TelegramError:
        return False
    if mem.status == "creator":
        return True
    if mem.status != "administrator":
        return False
    return right is None or bool(getattr(mem, right, False))


async def protected(bot, cid, uid):
    return uid == bot.id or await is_adm(bot, cid, uid) or await is_sudo(uid)


def guard(right=None):
    def deco(fn):
        async def w(update, context):
            m, u = update.effective_message, update.effective_user
            if not m or not u:
                return
            anon = m.sender_chat and m.sender_chat.id == m.chat_id
            if not anon and not await has_right(context.bot, m.chat_id, u.id, right):
                return
            return await fn(update, context)
        return w
    return deco


# ---------- messaging helpers ----------
async def send(bot, m, text, markup=None):
    kw = {"message_thread_id": m.message_thread_id} if m.is_topic_message else {}
    return await bot.send_message(
        m.chat_id, text, parse_mode=HTML, reply_markup=markup, **kw)


async def rm(bot, cid, *ids):
    try:
        await bot.delete_messages(cid, list(ids))
    except TelegramError:
        pass


async def out(bot, m, title, body_html, ttl=0, markup=None):
    s = await send(bot, m, card(title, body_html), markup)
    if ttl:
        await asyncio.sleep(ttl)
        await rm(bot, m.chat_id, s.message_id)
    return s


async def target(bot, m, args):
    """returns (user_id, name, rest_args); user_id None when not found."""
    args = list(args)
    r = m.reply_to_message
    if r and r.from_user:
        return r.from_user.id, r.from_user.full_name, args
    if args and args[0].lstrip("-").isdigit():
        uid = int(args.pop(0))
        return uid, str(uid), args
    return None, "", args


async def default_perms(bot, cid):
    try:
        p = (await bot.get_chat(cid)).permissions
        if p:
            return p
    except TelegramError:
        pass
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True)


# ---------- cached lists (blacklist, whitelist) ----------
_lc = {}


async def getlist(coll, cid):
    k = (coll, cid)
    c = _lc.get(k)
    if c and time.time() - c[0] < 30:
        return c[1]
    d = await db(T(coll).find_one, {"_id": cid}) or {}
    v = list(d.get("w", []))
    _lc[k] = (time.time(), v)
    return v


async def addlist(coll, cid, item):
    await db(T(coll).update_one, {"_id": cid}, {"$addToSet": {"w": item}}, upsert=True)
    _lc.pop((coll, cid), None)


async def dellist(coll, cid, item):
    await db(T(coll).update_one, {"_id": cid}, {"$pull": {"w": item}})
    _lc.pop((coll, cid), None)


# ---------- cached key/value (notes, filters) ----------
_kv = {}


async def kv(coll, cid):
    k = (coll, cid)
    c = _kv.get(k)
    if c and time.time() - c[0] < 30:
        return c[1]
    d = await db(lambda: {x["k"]: x["t"] for x in T(coll).find({"c": cid})})
    _kv[k] = (time.time(), d)
    return d


def kv_drop(coll, cid):
    _kv.pop((coll, cid), None)


# ---------- global ban cache ----------
_gb = {"t": 0, "s": set()}


async def gbanned(uid):
    if time.time() - _gb["t"] > 60:
        s = await db(lambda: {d["_id"] for d in T("gban").find({}, {"_id": 1})})
        _gb.update(t=time.time(), s=s)
    return uid in _gb["s"]


def gb_refresh():
    _gb["t"] = 0
