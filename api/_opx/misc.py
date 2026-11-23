import re
import time
import asyncio
from telegram import filters
from telegram.error import TelegramError
from . import core as C
from .core import *
from .mod import punish


def _code(x):
    return f"<code>{esc(x)}</code>"


# ---------- id, info, admins, ping ----------
async def cmd_id(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    body = f"{sc('you')} : {_code(u.id)}\n{sc('chat')} : {_code(m.chat_id)}"
    r = m.reply_to_message
    if r and r.from_user:
        body += f"\n{sc('replied user')} : {_code(r.from_user.id)}"
    await out(bot, m, "ids", body, 30)


async def cmd_info(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        uid, name = u.id, u.full_name
    status, uname = "unknown", ""
    try:
        mem = await bot.get_chat_member(m.chat_id, uid)
        status = mem.status
        name = mem.user.full_name
        uname = f"@{mem.user.username}" if mem.user.username else ""
    except TelegramError:
        pass
    d = await db(T("warns").find_one, {"_id": f"{m.chat_id}:{uid}"}) or {}
    body = (f"{sc('user')} : {ment(uid, name)}\n{sc('id')} : {_code(uid)}\n"
            f"{sc('username')} : {esc(uname) or '-'}\n{sc('status')} : {sc(status)}\n"
            f"{sc('warns')} : {d.get('n', 0)}\n"
            f"{sc('globally banned')} : {sc('yes' if await gbanned(uid) else 'no')}")
    await out(bot, m, "user info", body, 30)


async def cmd_admins(update, context):
    m, bot = update.effective_message, context.bot
    try:
        lst = await bot.get_chat_administrators(m.chat_id)
    except TelegramError as e:
        return await out(bot, m, "failed", err(e), 8)
    lines = []
    for a in sorted(lst, key=lambda a: a.status != "creator"):
        if a.user.is_bot:
            continue
        t = f" · {esc(a.custom_title)}" if getattr(a, "custom_title", None) else ""
        lines.append(("👑 " if a.status == "creator" else "• ") + esc(a.user.full_name) + t)
    await out(bot, m, "admins", "\n".join(lines) or "-", 60)


async def cmd_ping(update, context):
    m, bot = update.effective_message, context.bot
    t0 = time.time()
    s = await out(bot, m, "pong", sc("measuring..."))
    ms = int((time.time() - t0) * 1000)
    try:
        await s.edit_text(card("pong", f"{ms} {sc('ms')}"), parse_mode=HTML)
    except TelegramError:
        pass


# ---------- rules ----------
async def cmd_rules(update, context):
    m, bot = update.effective_message, context.bot
    r = (await gcfg(m.chat_id))["rules"]
    if not r:
        return await out(bot, m, "rules", sc("no rules set yet."), 10)
    await send(bot, m, f"<b>{sc('rules')}</b>\n<blockquote>{esc(r)}</blockquote>")


@guard("can_change_info")
async def cmd_setrules(update, context):
    m, bot = update.effective_message, context.bot
    t = " ".join(context.args) or (m.reply_to_message.text if m.reply_to_message else "")
    if not t:
        return await out(bot, m, "usage", sc("/setrules text, or reply to a message"), 6)
    await gset(m.chat_id, rules=t[:3000])
    await out(bot, m, "done", sc("rules saved."), 6)


@guard("can_change_info")
async def cmd_clearrules(update, context):
    m, bot = update.effective_message, context.bot
    await gset(m.chat_id, rules="")
    await out(bot, m, "done", sc("rules cleared."), 6)


# ---------- notes and filters ----------
def _name_text(m, args):
    if not args:
        return None, None
    name = args[0].lower().lstrip("#")
    t = " ".join(args[1:]) or (
        (m.reply_to_message.text or m.reply_to_message.caption) if m.reply_to_message else "")
    return name, (t or "")[:3500]


@guard()
async def cmd_save(update, context):
    m, bot = update.effective_message, context.bot
    name, t = _name_text(m, context.args)
    if not name or not t:
        return await out(bot, m, "usage", sc("/save name text, or reply to a message with /save name"), 8)
    await db(T("notes").update_one, {"c": m.chat_id, "k": name}, {"$set": {"t": t}}, upsert=True)
    kv_drop("notes", m.chat_id)
    await out(bot, m, "saved", sc(f"use #{name} or /get {name}"), 8)


async def cmd_get(update, context):
    m, bot = update.effective_message, context.bot
    if not context.args:
        return await out(bot, m, "usage", sc("/get name"), 6)
    t = (await kv("notes", m.chat_id)).get(context.args[0].lower().lstrip("#"))
    if t is None:
        return await out(bot, m, "not found", sc("no such note."), 6)
    try:
        await m.reply_text(t)
    except TelegramError:
        pass


async def cmd_notes(update, context):
    m, bot = update.effective_message, context.bot
    n = await kv("notes", m.chat_id)
    body = "\n".join(f"#{esc(k)}" for k in sorted(n)) or sc("no notes yet.")
    await out(bot, m, "notes", body, 60)


@guard()
async def cmd_clear(update, context):
    m, bot = update.effective_message, context.bot
    if not context.args:
        return await out(bot, m, "usage", sc("/clear name"), 6)
    k = context.args[0].lower().lstrip("#")
    await db(T("notes").delete_one, {"c": m.chat_id, "k": k})
    kv_drop("notes", m.chat_id)
    await out(bot, m, "cleared", sc(f"note {k} removed."), 6)


@guard()
async def cmd_filter(update, context):
    m, bot = update.effective_message, context.bot
    name, t = _name_text(m, context.args)
    if not name or not t:
        return await out(bot, m, "usage", sc("/filter word reply text, or reply to a message with /filter word"), 8)
    await db(T("filters").update_one, {"c": m.chat_id, "k": name}, {"$set": {"t": t}}, upsert=True)
    kv_drop("filters", m.chat_id)
    await out(bot, m, "filter added", sc(f"i will reply when someone says {name}."), 8)


@guard()
async def cmd_stop(update, context):
    m, bot = update.effective_message, context.bot
    if not context.args:
        return await out(bot, m, "usage", sc("/stop word"), 6)
    k = context.args[0].lower()
    await db(T("filters").delete_one, {"c": m.chat_id, "k": k})
    kv_drop("filters", m.chat_id)
    await out(bot, m, "filter removed", sc(f"{k} is no longer filtered."), 6)


async def cmd_filters(update, context):
    m, bot = update.effective_message, context.bot
    f = await kv("filters", m.chat_id)
    await out(bot, m, "filters", "\n".join(f"• {esc(k)}" for k in sorted(f)) or sc("no filters yet."), 60)


# ---------- report ----------
async def cmd_report(update, context):
    m, bot = update.effective_message, context.bot
    r = m.reply_to_message
    if not r or not r.from_user:
        return await out(bot, m, "usage", sc("reply to the message you want to report."), 6)
    try:
        lst = await bot.get_chat_administrators(m.chat_id)
    except TelegramError:
        return
    tags = "".join(f'<a href="tg://user?id={a.user.id}">​</a>' for a in lst if not a.user.is_bot)
    body = f"{sc('reported')} : {ment(r.from_user.id, r.from_user.full_name)}\n{sc('admins have been notified.')}"
    await send(bot, m, card("report", body) + tags)


# ---------- afk ----------
_afk = {"t": 0, "d": {}}


async def afk_map():
    if time.time() - _afk["t"] > 45:
        d = await db(lambda: {x["_id"]: x for x in T("afk").find({})})
        _afk.update(t=time.time(), d=d)
    return _afk["d"]


async def cmd_afk(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    why = " ".join(context.args)[:100]
    await db(T("afk").update_one, {"_id": u.id},
             {"$set": {"r": why, "n": u.full_name, "ts": int(time.time())}}, upsert=True)
    (await afk_map())[u.id] = {"_id": u.id, "r": why, "n": u.full_name}
    await out(bot, m, "afk", f"{ment(u.id, u.full_name)} {sc('is now afk')}" + (f"\n{esc(why)}" if why else ""), 10)


# ---------- hook called for every normal group message ----------
async def on_message(update, context, m):
    bot, cid, u = context.bot, m.chat_id, m.from_user
    text = m.text or m.caption or ""
    low = text.lower()
    afk = await afk_map()
    if u.id in afk and not low.startswith("/afk"):
        afk.pop(u.id, None)
        await db(T("afk").delete_one, {"_id": u.id})
        await out(bot, m, "welcome back", ment(u.id, u.full_name), 6)
    r = m.reply_to_message
    if r and r.from_user and r.from_user.id in afk and r.from_user.id != u.id:
        d = afk[r.from_user.id]
        why = f"\n{esc(d.get('r'))}" if d.get("r") else ""
        await out(bot, m, "afk", f"{ment(r.from_user.id, d.get('n'))} {sc('is away')}{why}", 8)
    if low.startswith("#") and len(low) > 1:
        t = (await kv("notes", cid)).get(low[1:].split()[0])
        if t is not None:
            try:
                await m.reply_text(t)
            except TelegramError:
                pass
            return
    if low and not low.startswith("/"):
        for k, t in (await kv("filters", cid)).items():
            if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low):
                try:
                    await m.reply_text(t)
                except TelegramError:
                    pass
                break


# ---------- owner and sudo ----------
async def cmd_gban(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    if not u or not await is_sudo(u.id):
        return
    uid, name, rest = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("/gban user id reason"), 6)
    if await is_sudo(uid):
        return await out(bot, m, "denied", sc("you cannot gban an admin of this bot."), 6)
    await db(T("gban").update_one, {"_id": uid},
             {"$set": {"r": " ".join(rest), "ts": int(time.time())}}, upsert=True)
    gb_refresh()
    if m.chat.type != "private":
        try:
            await punish(bot, m.chat_id, uid, "ban")
        except TelegramError:
            pass
    await out(bot, m, "globally banned", f"{ment(uid, name)}\n{sc('removed whenever they speak or join.')}")


async def cmd_ungban(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    if not u or not await is_sudo(u.id):
        return
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("/ungban user id"), 6)
    await db(T("gban").delete_one, {"_id": uid})
    gb_refresh()
    await out(bot, m, "ungbanned", ment(uid, name))


async def cmd_chats(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    if not u or not await is_sudo(u.id):
        return
    n = await db(T("chats").count_documents, {})
    rows = await db(lambda: list(T("chats").find({}).limit(40)))
    body = "\n".join(f"{esc(r.get('title') or '-')} {_code(r['_id'])}" for r in rows)
    await out(bot, m, "chats", f"{sc('total')} : {n}\n{body}")


async def cmd_leave(update, context):
    m, u, bot = update.effective_message, update.effective_user, context.bot
    if not u or u.id != OWNER_ID:
        return
    a = context.args
    cid = int(a[0]) if a and a[0].lstrip("-").isdigit() else (m.chat_id if m.chat.type != "private" else None)
    if not cid:
        return await out(bot, m, "usage", sc("/leave chat id"), 6)
    try:
        await bot.leave_chat(cid)
        await db(T("chats").delete_one, {"_id": cid})
        await out(bot, m, "left", _code(cid))
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


COMMANDS = {
    "id": cmd_id, "info": cmd_info, "admins": cmd_admins, "ping": cmd_ping,
    "rules": cmd_rules, "setrules": cmd_setrules, "clearrules": cmd_clearrules,
    "save": cmd_save, "get": cmd_get, "notes": cmd_notes, "clear": cmd_clear,
    "filter": cmd_filter, "stop": cmd_stop, "filters": cmd_filters,
    "report": cmd_report, "afk": cmd_afk,
}
OWNER_COMMANDS = {"gban": cmd_gban, "ungban": cmd_ungban, "chats": cmd_chats, "leave": cmd_leave}
