import re
import time
import asyncio
from collections import defaultdict
from telegram import InlineKeyboardMarkup, ChatPermissions, MessageEntity
from telegram.ext import filters
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop
from . import core as C
from . import misc
from .core import *
from .mod import punish, add_warn

LOCKS = ["sticker", "gif", "forward", "poll", "voice", "media", "contact", "document", "location"]
LINKMODES = ["delete", "warn", "mute", "off"]
SVC = [("clean_join", "join"), ("clean_left", "left"), ("clean_vc", "voice chat"),
       ("clean_pin", "pin"), ("clean_title", "title and photo"),
       ("clean_owner", "ownership"), ("clean_other", "other")]
PROT = [("approve", "auto approve"), ("antibot", "anti bot"), ("antichannel", "anti channel"),
        ("cleancmd", "clean commands"), ("captcha", "captcha")]

_flood = defaultdict(list)
_slow = {}
_known = set()


# ---------- detection helpers ----------
def is_quiz(m):
    v, f = m.via_bot, m.from_user
    return bool((v and (v.username or "").lower() == "quizbot")
                or (f and f.is_bot and (f.username or "").lower() == "quizbot"))


def svc_kind(m):
    if m.new_chat_members:
        return "join"
    if m.left_chat_member:
        return "left"
    if m.video_chat_started or m.video_chat_ended or m.video_chat_scheduled or m.video_chat_participants_invited:
        return "vc"
    if m.pinned_message:
        return "pin"
    if m.new_chat_title or m.new_chat_photo or m.delete_chat_photo:
        return "title"
    k = m.api_kwargs or {}
    if "chat_owner_left" in k or "chat_owner_changed" in k:
        return "owner"
    if filters.StatusUpdate.ALL.filter(m):
        return "other"
    return None


def lock_hits(m):
    h = set()
    if m.sticker:
        h.add("sticker")
    if m.animation:
        h.add("gif")
    if m.forward_origin and not m.is_automatic_forward:
        h.add("forward")
    if m.poll:
        h.add("poll")
    if m.voice or m.video_note:
        h.add("voice")
    if m.photo or m.video:
        h.add("media")
    if m.contact:
        h.add("contact")
    if m.document and not m.animation:
        h.add("document")
    if m.location or m.venue:
        h.add("location")
    return h


def urls_of(m):
    res = []
    for ents, parse in ((m.entities, m.parse_entity), (m.caption_entities, m.parse_caption_entity)):
        for e in ents or ():
            if e.type == MessageEntity.URL:
                res.append(parse(e))
            elif e.type == MessageEntity.TEXT_LINK and e.url:
                res.append(e.url)
    return res


def norm(u):
    u = re.sub(r"^https?://", "", str(u).lower().strip())
    return re.sub(r"^www\.", "", u).rstrip("/")


# ---------- service messages ----------
async def welcome(bot, uid):
    await C.welcome_dm(bot, uid)


async def on_service(context, m, kind):
    bot, cid = context.bot, m.chat_id
    c = await gcfg(cid)
    if kind == "join":
        adder = m.from_user.id if m.from_user else 0
        for u in m.new_chat_members:
            try:
                if u.is_bot:
                    if c["antibot"] and u.id != bot.id and not await has_right(bot, cid, adder):
                        await punish(bot, cid, u.id, "kick")
                    continue
                if await gbanned(u.id):
                    await punish(bot, cid, u.id, "ban")
                    continue
                if c["captcha"]:
                    await bot.restrict_chat_member(cid, u.id, ChatPermissions(can_send_messages=False))
                    await out(bot, m, "verify", f"{ment(u.id, u.full_name)}\n{sc('tap the button to start chatting.')}",
                              markup=InlineKeyboardMarkup([[btn("i am human", "success", callback_data=f"cap:{u.id}")]]))
                await welcome(bot, u.id)
            except TelegramError:
                pass
    if await gflag("clean") and c.get("clean_" + kind, True):
        await rm(bot, cid, m.message_id)


async def join_req(update, context):
    r = update.chat_join_request
    c = await gcfg(r.chat.id)
    if await gflag("approve") and c["approve"]:
        try:
            if await gbanned(r.from_user.id):
                await r.decline()
            else:
                await r.approve()
                await welcome(context.bot, r.user_chat_id)
        except TelegramError:
            pass
    raise ApplicationHandlerStop


async def captcha_cb(update, context):
    q, bot = update.callback_query, context.bot
    uid = int(q.data.split(":")[1])
    if q.from_user.id != uid:
        return await q.answer("this button is not for you", show_alert=True)
    cid = q.message.chat_id
    try:
        await bot.restrict_chat_member(cid, uid, await default_perms(bot, cid))
    except TelegramError as e:
        return await q.answer(str(e)[:150], show_alert=True)
    await q.answer("verified")
    await rm(bot, cid, q.message.message_id)


# ---------- the watcher: runs on every group message ----------
async def watch(update, context):
    m = update.effective_message
    if not m:
        return
    bot, cid = context.bot, m.chat_id
    if cid not in _known:
        _known.add(cid)
        await db(T("chats").update_one, {"_id": cid},
                 {"$set": {"title": m.chat.title, "type": m.chat.type}}, upsert=True)
    kind = svc_kind(m)
    if kind:
        await on_service(context, m, kind)
        raise ApplicationHandlerStop
    c = await gcfg(cid)
    u, s = m.from_user, m.sender_chat
    if s and s.id == cid:
        return  # anonymous admin
    if m.is_automatic_forward:
        return
    if s and s.id != cid:
        if c["antichannel"]:
            await rm(bot, cid, m.message_id)
            raise ApplicationHandlerStop
        return
    if not u or is_quiz(m):
        return
    uid = u.id
    if await gbanned(uid):
        try:
            await punish(bot, cid, uid, "ban")
        except TelegramError:
            pass
        await rm(bot, cid, m.message_id)
        raise ApplicationHandlerStop
    text = m.text or m.caption or ""
    edited = bool(update.edited_message)
    if c["cleancmd"] and text.startswith("/") and not edited:
        await rm(bot, cid, m.message_id)

    state = {}

    async def priv():
        if "p" not in state:
            state["p"] = await protected(bot, cid, uid)
        return state["p"]

    async def slap(note, action=None, secs=None):
        await rm(bot, cid, m.message_id)
        if action:
            try:
                await punish(bot, cid, uid, action, secs)
            except TelegramError:
                pass
        if note:
            asyncio.create_task(out(bot, m, "notice", f"{ment(uid, u.full_name)}\n{sc(note)}", 6))
        raise ApplicationHandlerStop

    hits = lock_hits(m) & set(c["locks"])
    if hits and not await priv():
        await slap("")

    if c["link_mode"] != "off" and (m.entities or m.caption_entities) and await gflag("links"):
        urls = urls_of(m)
        if urls and not await priv() and uid not in await getlist("allow", cid):
            wl = [norm(x) for x in await getlist("whitelist", cid)]
            if any(not any(norm(x).startswith(w) for w in wl) for x in urls):
                mode = c["link_mode"]
                if mode == "warn":
                    await rm(bot, cid, m.message_id)
                    await add_warn(bot, m, uid, u.full_name, "link")
                    raise ApplicationHandlerStop
                if mode == "mute":
                    await slap("links are not allowed. muted for 1 hour.", "mute", 3600)
                await slap("")

    if text:
        words = await getlist("blacklist", cid)
        if words and any(w in text.lower() for w in words) and not await priv():
            await slap("")

    now = time.time()
    if c["flood"] > 0 and not edited:
        k = (cid, uid)
        _flood[k] = [t for t in _flood[k] if now - t < 8] + [now]
        if len(_flood) > 4000:
            _flood.clear()
        if len(_flood[k]) > c["flood"] and not await priv():
            _flood.pop(k, None)
            act = c["flood_action"]
            await slap(f"flooding. {act}.", act, 3600 if act == "mute" else None)
    if c["slow"] > 0 and not edited:
        k = (cid, uid)
        if now - _slow.get(k, 0) < c["slow"] and not await priv():
            await slap("")
        _slow[k] = now
        if len(_slow) > 4000:
            _slow.clear()

    if not edited:
        await misc.on_message(update, context, m)


# ---------- settings panels ----------
def panel(c, which):
    keys = SVC if which == "svc" else PROT
    rows, cur = [], []
    for k, label in keys:
        cur.append(btn(f"{label} : {'on' if c[k] else 'off'}", "success" if c[k] else "danger",
                       callback_data=f"s:{which}:{k}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    if which == "prot":
        rows.append([btn(f"links : {c['link_mode']}", "primary", callback_data="s:prot:link")])
    rows.append([btn("back", "primary", callback_data="s:home:x"),
                 btn("close", "danger", callback_data="s:close:x")])
    return InlineKeyboardMarkup(rows)


def home_markup():
    return InlineKeyboardMarkup([
        [btn("service cleaners", "primary", callback_data="s:go:svc"),
         btn("protection", "primary", callback_data="s:go:prot")],
        [btn("close", "danger", callback_data="s:close:x")]])


HOME = card("group settings", sc("choose a section. changes apply only to this group."))


@guard()
async def cmd_settings(update, context):
    m = update.effective_message
    await send(context.bot, m, HOME, home_markup())


@guard()
async def cmd_cleanservice(update, context):
    m = update.effective_message
    c = await gcfg(m.chat_id)
    await send(context.bot, m, card("service cleaners", sc("tap to switch each cleaner on or off.")), panel(c, "svc"))


async def settings_cb(update, context):
    q, bot = update.callback_query, context.bot
    cid = q.message.chat_id
    if not await has_right(bot, cid, q.from_user.id):
        return await q.answer("admins only", show_alert=True)
    _, act, key = q.data.split(":")
    await q.answer()
    if act == "close":
        return await rm(bot, cid, q.message.message_id)
    try:
        if act in ("svc", "prot"):
            c = await gcfg(cid)
            if key == "link":
                await gset(cid, link_mode=LINKMODES[(LINKMODES.index(c["link_mode"]) + 1) % 4])
            else:
                await gset(cid, **{key: not c[key]})
        c = await gcfg(cid)
        if act == "home":
            await q.edit_message_text(HOME, parse_mode=HTML, reply_markup=home_markup())
        elif act == "go" or act in ("svc", "prot"):
            which = key if act == "go" else act
            title = "service cleaners" if which == "svc" else "protection"
            await q.edit_message_text(card(title, sc("tap to switch on or off.")), parse_mode=HTML,
                                      reply_markup=panel(c, which))
    except TelegramError:
        pass


# ---------- command based switches ----------
def _flag(name, key):
    @guard()
    async def fn(update, context):
        m, bot = update.effective_message, context.bot
        a = context.args[0].lower() if context.args else ""
        if a in ("on", "off"):
            await gset(m.chat_id, **{key: a == "on"})
        state = "on" if (await gcfg(m.chat_id))[key] else "off"
        await out(bot, m, name, sc(f"{name} is {state}. use /{name} on or off."), 8)
    return fn


@guard("can_delete_messages")
async def cmd_antiforward(update, context):
    m, bot = update.effective_message, context.bot
    locks = set((await gcfg(m.chat_id))["locks"])
    a = context.args[0].lower() if context.args else ""
    if a in ("on", "off"):
        (locks.add if a == "on" else locks.discard)("forward")
        await gset(m.chat_id, locks=sorted(locks))
    await out(bot, m, "antiforward", sc(f"antiforward is {'on' if 'forward' in locks else 'off'}. use /antiforward on or off."), 8)


@guard("can_delete_messages")
async def cmd_lock(update, context):
    m, bot = update.effective_message, context.bot
    locks = set((await gcfg(m.chat_id))["locks"])
    t = context.args[0].lower() if context.args else ""
    if t in LOCKS:
        locks.add(t)
        await gset(m.chat_id, locks=sorted(locks))
    body = sc("types : " + ", ".join(LOCKS) + "\nlocked : " + (", ".join(sorted(locks)) or "none"))
    await out(bot, m, "locks", body, 15)


@guard("can_delete_messages")
async def cmd_unlock(update, context):
    m, bot = update.effective_message, context.bot
    locks = set((await gcfg(m.chat_id))["locks"])
    t = context.args[0].lower() if context.args else ""
    if t == "all":
        locks.clear()
    elif t in LOCKS:
        locks.discard(t)
    await gset(m.chat_id, locks=sorted(locks))
    await out(bot, m, "locks", sc("locked : " + (", ".join(sorted(locks)) or "none")), 10)


@guard("can_delete_messages")
async def cmd_antilink(update, context):
    m, bot = update.effective_message, context.bot
    a = context.args[0].lower() if context.args else ""
    if a in LINKMODES:
        await gset(m.chat_id, link_mode=a)
    mode = (await gcfg(m.chat_id))["link_mode"]
    await out(bot, m, "antilink", sc(f"mode is {mode}. use /antilink delete, warn, mute or off."), 10)


@guard("can_delete_messages")
async def cmd_antiflood(update, context):
    m, bot = update.effective_message, context.bot
    a = context.args
    if a:
        if a[0].lower() == "off":
            await gset(m.chat_id, flood=0)
        elif a[0].isdigit() and 3 <= int(a[0]) <= 30:
            kw = {"flood": int(a[0])}
            if len(a) > 1 and a[1].lower() in ("mute", "kick", "ban"):
                kw["flood_action"] = a[1].lower()
            await gset(m.chat_id, **kw)
    c = await gcfg(m.chat_id)
    s = "off" if not c["flood"] else f"{c['flood']} messages in 8 seconds, then {c['flood_action']}"
    await out(bot, m, "antiflood", sc(f"{s}. use /antiflood 6 mute, or /antiflood off."), 10)


@guard("can_delete_messages")
async def cmd_slowmode(update, context):
    m, bot = update.effective_message, context.bot
    a = context.args[0].lower() if context.args else ""
    if a == "off":
        await gset(m.chat_id, slow=0)
    elif a.isdigit() and 1 <= int(a) <= 600:
        await gset(m.chat_id, slow=int(a))
    s = (await gcfg(m.chat_id))["slow"]
    await out(bot, m, "slowmode", sc(f"{s} seconds between messages. use /slowmode 10 or /slowmode off." if s else "off. use /slowmode 10"), 10)


@guard("can_delete_messages")
async def cmd_allow(update, context):
    m, bot = update.effective_message, context.bot
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("reply to a user or send id with /allow"), 6)
    await addlist("allow", m.chat_id, uid)
    await out(bot, m, "allowed", f"{ment(uid, name)}\n{sc('can send links now.')}")


@guard("can_delete_messages")
async def cmd_unallow(update, context):
    m, bot = update.effective_message, context.bot
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("reply to a user or send id with /unallow"), 6)
    await dellist("allow", m.chat_id, uid)
    await out(bot, m, "removed", f"{ment(uid, name)}\n{sc('can no longer send links.')}")


def _list_cmds(coll, one, many, add_title):
    @guard("can_delete_messages")
    async def add(update, context):
        m, bot = update.effective_message, context.bot
        item = " ".join(context.args).lower().strip()
        if not item:
            lst = await getlist(coll, m.chat_id)
            return await out(bot, m, many, "\n".join(esc(x) for x in lst) or sc("nothing added yet."), 30)
        await addlist(coll, m.chat_id, item)
        await out(bot, m, add_title, esc(item), 8)

    @guard("can_delete_messages")
    async def remove(update, context):
        m, bot = update.effective_message, context.bot
        item = " ".join(context.args).lower().strip()
        if not item:
            return await out(bot, m, "usage", sc(f"/un{one} item"), 6)
        await dellist(coll, m.chat_id, item)
        await out(bot, m, "removed", esc(item), 8)

    @guard("can_delete_messages")
    async def show(update, context):
        m, bot = update.effective_message, context.bot
        lst = await getlist(coll, m.chat_id)
        await out(bot, m, many, "\n".join(esc(x) for x in lst) or sc("nothing added yet."), 30)
    return add, remove, show


_wl_add, _wl_rm, _wl_show = _list_cmds("whitelist", "whitelist", "whitelist", "whitelisted")
_bl_add, _bl_rm, _bl_show = _list_cmds("blacklist", "blacklist", "blacklist", "blacklisted")

COMMANDS = {
    "settings": cmd_settings, "cleanservice": cmd_cleanservice,
    "antibot": _flag("antibot", "antibot"), "antichannel": _flag("antichannel", "antichannel"),
    "cleancmd": _flag("cleancmd", "cleancmd"), "captcha": _flag("captcha", "captcha"),
    "antiforward": cmd_antiforward, "lock": cmd_lock, "unlock": cmd_unlock,
    "antilink": cmd_antilink, "antiflood": cmd_antiflood, "slowmode": cmd_slowmode,
    "allow": cmd_allow, "unallow": cmd_unallow,
    "whitelist": _wl_add, "unwhitelist": _wl_rm,
    "blacklist": _bl_add, "unblacklist": _bl_rm, "blacklists": _bl_show,
}
