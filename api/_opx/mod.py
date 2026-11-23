import time
import asyncio
from pymongo import ReturnDocument
from telegram import InlineKeyboardMarkup, ChatPermissions
from telegram.error import TelegramError, RetryAfter
from .core import *

NOPERM = ChatPermissions(can_send_messages=False)
LABEL = {
    "ban": "banned", "tban": "temp banned", "dban": "banned", "kick": "kicked",
    "mute": "muted", "tmute": "temp muted", "unban": "unbanned", "unmute": "unmuted",
}


def _until(secs):
    return int(time.time() + secs) if secs else None


async def punish(bot, cid, uid, action, secs=None):
    if action == "ban":
        await bot.ban_chat_member(cid, uid, until_date=_until(secs))
    elif action == "kick":
        await bot.ban_chat_member(cid, uid)
        await bot.unban_chat_member(cid, uid, only_if_banned=True)
    elif action == "mute":
        await bot.restrict_chat_member(cid, uid, NOPERM, until_date=_until(secs))


# ---------- ban, mute, kick family ----------
async def _moderate(update, context, kind):
    m, bot = update.effective_message, context.bot
    cid = m.chat_id
    uid, name, rest = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc(f"reply to a user or send a user id with /{kind}"), 6)
    if kind not in ("unban", "unmute") and await protected(bot, cid, uid):
        return await out(bot, m, "denied", sc("i cannot do that to an admin."), 6)
    secs, shown = None, ""
    if kind in ("tban", "tmute"):
        secs = parse_time(rest[0] if rest else "")
        if not secs:
            return await out(bot, m, "usage", sc(f"/{kind} 30m or 2h or 1d"), 6)
        shown, rest = rest[0], rest[1:]
    reason = " ".join(rest)
    try:
        if kind in ("ban", "tban", "dban"):
            await punish(bot, cid, uid, "ban", secs)
        elif kind == "kick":
            await punish(bot, cid, uid, "kick")
        elif kind in ("mute", "tmute"):
            await punish(bot, cid, uid, "mute", secs)
        elif kind == "unban":
            await bot.unban_chat_member(cid, uid, only_if_banned=True)
        else:
            await bot.restrict_chat_member(cid, uid, await default_perms(bot, cid))
        if kind == "dban":
            r = m.reply_to_message
            await rm(bot, cid, *([r.message_id] if r else []), m.message_id)
    except TelegramError as e:
        return await out(bot, m, "failed", err(e), 8)
    body = f"{sc(LABEL[kind])} : {ment(uid, name)}"
    if shown:
        body += f"\n{sc('time')} : {esc(shown)}"
    if reason:
        body += f"\n{sc('reason')} : {esc(reason)}"
    mk = None
    if kind in ("ban", "tban", "dban"):
        mk = InlineKeyboardMarkup([[btn("unban", "success", callback_data=f"m:unban:{uid}")]])
    elif kind in ("mute", "tmute"):
        mk = InlineKeyboardMarkup([[btn("unmute", "success", callback_data=f"m:unmute:{uid}")]])
    await out(bot, m, LABEL[kind], body, markup=mk)


def _mk(kind, right):
    @guard(right)
    async def fn(update, context):
        await _moderate(update, context, kind)
    return fn


async def mod_cb(update, context):
    q, bot = update.callback_query, context.bot
    cid = q.message.chat_id
    if not await has_right(bot, cid, q.from_user.id, "can_restrict_members"):
        return await q.answer("admins only", show_alert=True)
    _, act, uid = q.data.split(":")
    uid = int(uid)
    try:
        if act == "unban":
            await bot.unban_chat_member(cid, uid, only_if_banned=True)
        elif act == "unmute":
            await bot.restrict_chat_member(cid, uid, await default_perms(bot, cid))
        elif act == "unwarn":
            await db(T("warns").find_one_and_update,
                     {"_id": f"{cid}:{uid}", "n": {"$gt": 0}}, {"$inc": {"n": -1}})
    except TelegramError as e:
        return await q.answer(str(e)[:150], show_alert=True)
    await q.answer("done")
    try:
        await q.edit_message_reply_markup(None)
    except TelegramError:
        pass


# ---------- warns ----------
async def add_warn(bot, m, uid, name, reason=""):
    cid = m.chat_id
    c = await gcfg(cid)
    r = await db(T("warns").find_one_and_update, {"_id": f"{cid}:{uid}"},
                 {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    n, lim, act = r["n"], c["warn_limit"], c["warn_action"]
    body = f"{sc('warned')} : {ment(uid, name)}\n{sc('warns')} : {n}/{lim}"
    if reason:
        body += f"\n{sc('reason')} : {esc(reason)}"
    mk = None
    if n >= lim:
        await db(T("warns").delete_one, {"_id": f"{cid}:{uid}"})
        try:
            await punish(bot, cid, uid, act)
            body += f"\n{sc('limit reached')} : {sc(act)}"
        except TelegramError as e:
            body += f"\n{sc('failed')} : {err(e)}"
    else:
        mk = InlineKeyboardMarkup([[btn("remove warn", "danger", callback_data=f"m:unwarn:{uid}")]])
    await out(bot, m, "warn", body, markup=mk)


@guard("can_restrict_members")
async def cmd_warn(update, context):
    m, bot = update.effective_message, context.bot
    uid, name, rest = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("reply to a user or send id with /warn"), 6)
    if await protected(bot, m.chat_id, uid):
        return await out(bot, m, "denied", sc("i cannot warn an admin."), 6)
    await add_warn(bot, m, uid, name, " ".join(rest))


@guard("can_restrict_members")
async def cmd_unwarn(update, context):
    m, bot = update.effective_message, context.bot
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("reply to a user or send id with /unwarn"), 6)
    r = await db(T("warns").find_one_and_update,
                 {"_id": f"{m.chat_id}:{uid}", "n": {"$gt": 0}}, {"$inc": {"n": -1}},
                 return_document=ReturnDocument.AFTER)
    left = r["n"] if r else 0
    await out(bot, m, "unwarned", f"{ment(uid, name)}\n{sc('warns left')} : {left}")


async def cmd_warns(update, context):
    m, bot = update.effective_message, context.bot
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        u = update.effective_user
        uid, name = u.id, u.full_name
    c = await gcfg(m.chat_id)
    d = await db(T("warns").find_one, {"_id": f"{m.chat_id}:{uid}"}) or {}
    await out(bot, m, "warns", f"{ment(uid, name)}\n{sc('warns')} : {d.get('n', 0)}/{c['warn_limit']}", 15)


@guard("can_restrict_members")
async def cmd_resetwarns(update, context):
    m, bot = update.effective_message, context.bot
    uid, name, _ = await target(bot, m, context.args)
    if not uid:
        return await out(bot, m, "usage", sc("reply to a user or send id with /resetwarns"), 6)
    await db(T("warns").delete_one, {"_id": f"{m.chat_id}:{uid}"})
    await out(bot, m, "reset", f"{ment(uid, name)}\n{sc('all warns removed')}")


@guard("can_change_info")
async def cmd_setwarn(update, context):
    m, bot = update.effective_message, context.bot
    a = context.args
    if not a or not a[0].isdigit() or not 2 <= int(a[0]) <= 20:
        return await out(bot, m, "usage", sc("/setwarn 3 mute  (limit 2 to 20, action mute ban or kick)"), 10)
    kw = {"warn_limit": int(a[0])}
    if len(a) > 1 and a[1].lower() in ("mute", "ban", "kick"):
        kw["warn_action"] = a[1].lower()
    await gset(m.chat_id, **kw)
    c = await gcfg(m.chat_id)
    await out(bot, m, "warn settings", sc(f"limit {c['warn_limit']}, action {c['warn_action']}"), 10)


# ---------- delete and purge ----------
@guard("can_delete_messages")
async def cmd_del(update, context):
    m, bot = update.effective_message, context.bot
    r = m.reply_to_message
    if not r:
        return await out(bot, m, "usage", sc("reply to the message you want to delete."), 5)
    await rm(bot, m.chat_id, r.message_id, m.message_id)


async def _delchunk(bot, cid, ids):
    for _ in range(2):
        try:
            await bot.delete_messages(cid, ids)
            return
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 10) + 1)
        except TelegramError:
            return


async def purge_range(bot, m, confirm):
    cid, end = m.chat_id, m.message_id
    start = max(m.reply_to_message.message_id, end - 3000)
    kept = await db(lambda: {d["m"] for d in T("keep").find(
        {"c": cid, "m": {"$gte": start, "$lte": end}}, {"m": 1})})
    ids = [i for i in range(start, end + 1) if i not in kept]
    chunks = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    for i in range(0, len(chunks), 5):
        await asyncio.gather(*(_delchunk(bot, cid, c) for c in chunks[i:i + 5]))
    if confirm:
        await out(bot, m, "purge complete",
                  sc(f"cleaned up to {len(ids)} messages. quiz bot messages were kept."), 4)


@guard("can_delete_messages")
async def cmd_spurge(update, context):
    m, bot = update.effective_message, context.bot
    if not m.reply_to_message:
        return await out(bot, m, "usage", sc("reply to the message you want to delete from."), 5)
    await purge_range(bot, m, False)


# ---------- pins ----------
@guard("can_pin_messages")
async def cmd_pin(update, context):
    m, bot = update.effective_message, context.bot
    r = m.reply_to_message
    if not r:
        return await out(bot, m, "usage", sc("reply to a message. add silent to pin quietly."), 6)
    quiet = bool(context.args and context.args[0].lower() in ("silent", "quiet", "off"))
    try:
        await bot.pin_chat_message(m.chat_id, r.message_id, disable_notification=quiet)
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


@guard("can_pin_messages")
async def cmd_unpin(update, context):
    m, bot = update.effective_message, context.bot
    r = m.reply_to_message
    try:
        await bot.unpin_chat_message(m.chat_id, message_id=r.message_id if r else None)
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


@guard("can_pin_messages")
async def cmd_unpinall(update, context):
    m, bot = update.effective_message, context.bot
    try:
        await bot.unpin_all_chat_messages(m.chat_id)
        await out(bot, m, "done", sc("all pinned messages removed."), 6)
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


# ---------- group info ----------
@guard("can_change_info")
async def cmd_setgtitle(update, context):
    m, bot = update.effective_message, context.bot
    t = " ".join(context.args)
    if not t:
        return await out(bot, m, "usage", sc("/setgtitle new group name"), 6)
    try:
        await bot.set_chat_title(m.chat_id, t[:128])
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


@guard("can_change_info")
async def cmd_setgdesc(update, context):
    m, bot = update.effective_message, context.bot
    t = " ".join(context.args) or (m.reply_to_message.text if m.reply_to_message else "")
    if not t:
        return await out(bot, m, "usage", sc("/setgdesc text, or reply to a message"), 6)
    try:
        await bot.set_chat_description(m.chat_id, t[:255])
        await out(bot, m, "done", sc("description updated."), 6)
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


@guard("can_change_info")
async def cmd_setgpic(update, context):
    m, bot = update.effective_message, context.bot
    r = m.reply_to_message
    if not r or not r.photo:
        return await out(bot, m, "usage", sc("reply to a photo with /setgpic"), 6)
    try:
        f = await bot.get_file(r.photo[-1].file_id)
        data = bytes(await f.download_as_bytearray())
        await bot.set_chat_photo(m.chat_id, data)
        await out(bot, m, "done", sc("group photo updated."), 6)
    except TelegramError as e:
        await out(bot, m, "failed", err(e), 8)


@guard("can_invite_users")
async def cmd_invitelink(update, context):
    m, bot = update.effective_message, context.bot
    try:
        link = (await bot.get_chat(m.chat_id)).invite_link or await bot.export_chat_invite_link(m.chat_id)
    except TelegramError as e:
        return await out(bot, m, "failed", err(e), 8)
    await out(bot, m, "invite link", esc(link))


@guard("can_invite_users")
async def cmd_revoke(update, context):
    m, bot = update.effective_message, context.bot
    try:
        link = await bot.export_chat_invite_link(m.chat_id)
    except TelegramError as e:
        return await out(bot, m, "failed", err(e), 8)
    await out(bot, m, "new invite link", esc(link))


COMMANDS = {
    "ban": _mk("ban", "can_restrict_members"),
    "tban": _mk("tban", "can_restrict_members"),
    "dban": _mk("dban", "can_restrict_members"),
    "unban": _mk("unban", "can_restrict_members"),
    "kick": _mk("kick", "can_restrict_members"),
    "mute": _mk("mute", "can_restrict_members"),
    "tmute": _mk("tmute", "can_restrict_members"),
    "unmute": _mk("unmute", "can_restrict_members"),
    "warn": cmd_warn, "unwarn": cmd_unwarn, "warns": cmd_warns,
    "resetwarns": cmd_resetwarns, "setwarn": cmd_setwarn,
    "del": cmd_del, "spurge": cmd_spurge,
    "pin": cmd_pin, "unpin": cmd_unpin, "unpinall": cmd_unpinall,
    "setgtitle": cmd_setgtitle, "setgdesc": cmd_setgdesc, "setgpic": cmd_setgpic,
    "invitelink": cmd_invitelink, "revoke": cmd_revoke,
}
