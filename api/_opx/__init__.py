from telegram.ext import filters
from telegram.ext import (
    CommandHandler, MessageHandler, CallbackQueryHandler, ChatJoinRequestHandler,
)
from . import core, mod, misc, guard


def register(application, db, welcome_dm):
    """Wire the whole package into an existing Application."""
    core.D = db
    core.welcome_dm = welcome_dm
    grp = filters.ChatType.GROUPS
    anywhere = filters.ChatType.PRIVATE | filters.ChatType.GROUPS
    # watcher and join requests run first (group -2) and stop older handlers
    application.add_handler(ChatJoinRequestHandler(guard.join_req), group=-2)
    application.add_handler(MessageHandler(grp, guard.watch), group=-2)
    for table in (mod.COMMANDS, misc.COMMANDS, guard.COMMANDS):
        for name, fn in table.items():
            application.add_handler(CommandHandler(name, fn, grp))
    for name, fn in misc.OWNER_COMMANDS.items():
        application.add_handler(CommandHandler(name, fn, anywhere))
    application.add_handler(CallbackQueryHandler(mod.mod_cb, pattern="^m:"))
    application.add_handler(CallbackQueryHandler(guard.settings_cb, pattern="^s:"))
    application.add_handler(CallbackQueryHandler(guard.captcha_cb, pattern="^cap:"))
