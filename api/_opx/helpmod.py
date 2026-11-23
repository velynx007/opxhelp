from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop
from .core import *

CATS = {
    "mod": ("moderation", [
        ("/ban /unban", "ban or unban, reply or user id"),
        ("/tban 1h", "temporary ban"),
        ("/dban", "delete the replied message and ban"),
        ("/kick", "remove a user, they can rejoin"),
        ("/mute /unmute", "mute or unmute"),
        ("/tmute 30m", "temporary mute"),
        ("/warn /unwarn", "give or remove a warn"),
        ("/warns /resetwarns", "check or clear warns"),
        ("/setwarn 3 mute", "warn limit and action"),
    ]),
    "clean": ("cleanup", [
        ("/purge", "delete from the replied message down to here"),
        ("/spurge", "same as purge, silently"),
        ("/del", "delete the replied message"),
        ("/cleanservice", "switch join, left, vc, pin cleaners"),
        ("/cleancmd on", "delete commands after use"),
        ("/antibot on", "remove bots added by members"),
        ("/antichannel on", "delete messages sent as channels"),
    ]),
    "spam": ("anti spam", [
        ("/antilink", "delete, warn, mute or off"),
        ("/allow /unallow", "let a trusted user send links"),
        ("/whitelist /unwhitelist", "allow a domain such as t.me/xi5nxu"),
        ("/blacklist /unblacklist", "block words, /blacklists shows all"),
        ("/lock /unlock", "sticker, gif, forward, poll, voice, media, contact, document, location"),
        ("/antiforward on", "delete forwarded messages"),
        ("/antiflood 6 mute", "limit fast messages"),
        ("/slowmode 10", "seconds between messages"),
        ("/captcha on", "new members must tap a button"),
    ]),
    "group": ("group tools", [
        ("/settings", "per group switches panel"),
        ("/pin /unpin /unpinall", "manage pinned messages"),
        ("/setgtitle /setgdesc /setgpic", "change group name, bio and photo"),
        ("/invitelink /revoke", "get or reset the invite link"),
        ("/setrules /clearrules", "set the group rules"),
        ("/save name text", "save a note, /clear name removes it"),
        ("/filter word text", "auto reply, /stop word removes it"),
    ]),
    "all": ("for everyone", [
        ("/id", "your id and the chat id"),
        ("/info", "details about a user"),
        ("/admins", "list of admins"),
        ("/rules", "group rules"),
        ("/notes", "saved notes, use #name or /get name"),
        ("/filters", "active auto replies"),
        ("/warns", "check warns"),
        ("/report", "call the admins on a message"),
        ("/afk reason", "set away status"),
        ("/ping", "bot speed"),
    ]),
    "owner": ("owner", [
        ("/admin", "open the admin panel in private chat"),
        ("/gban /ungban", "ban a user from every group"),
        ("/chats", "groups the bot is in"),
        ("/leave id", "make the bot leave a group"),
        ("/ban /unban id", "block a user from the bot, in private chat"),
        ("/addsudo /rmsudo /sudolist", "manage sudo admins"),
    ]),
}
ORDER = ["mod", "clean", "spam", "group", "all"]
HOME = card("all commands", sc("pick a section. most commands work by replying to a user or message."))


async def markup(uid):
    keys = ORDER + (["owner"] if await is_sudo(uid) else [])
    rows, cur = [], []
    for k in keys:
        cur.append(btn(CATS[k][0], "success" if k == "owner" else "primary", callback_data=f"h:{k}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([btn("close", "danger", callback_data="h:x")])
    return InlineKeyboardMarkup(rows)


def section(k):
    title, rows = CATS[k]
    body = "\n".join(f"<code>{esc(c)}</code> {sc(d)}" for c, d in rows)
    return card(title, body)


async def cmd_help(update, context):
    m, u = update.effective_message, update.effective_user
    await send(context.bot, m, HOME, await markup(u.id))


async def entry(update, context):
    q = update.callback_query
    await q.answer()
    await context.bot.send_message(q.message.chat_id, HOME, parse_mode=HTML,
                                   reply_markup=await markup(q.from_user.id))
    raise ApplicationHandlerStop


async def cb(update, context):
    q = update.callback_query
    k = q.data.split(":")[1]
    if k == "owner" and not await is_sudo(q.from_user.id):
        return await q.answer("owner only", show_alert=True)
    await q.answer()
    try:
        if k == "x":
            return await q.message.delete()
        if k == "home":
            return await q.edit_message_text(HOME, parse_mode=HTML, reply_markup=await markup(q.from_user.id))
        if k in CATS:
            back = InlineKeyboardMarkup([[btn("back", "primary", callback_data="h:home"),
                                          btn("close", "danger", callback_data="h:x")]])
            await q.edit_message_text(section(k), parse_mode=HTML, reply_markup=back)
    except TelegramError:
        pass
