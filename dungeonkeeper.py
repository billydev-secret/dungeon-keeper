import logging
import logging.handlers
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

from app_context import AppContext, Bot, load_runtime_config
from commands.activity_commands import register_activity_commands
from commands.drama_commands import register_drama_commands
from commands.config_commands import register_config_commands
from commands.interaction_commands import register_interaction_commands
from commands.ai_mod_commands import register_ai_mod_commands
from commands.auto_delete_commands import register_auto_delete_commands
from commands.denizen_commands import register_denizen_commands
from commands.give_role_commands import init_give_role_tables, register_give_role_commands
from commands.inactivity_prune_commands import register_inactivity_prune_commands
from commands.mod_commands import register_mod_commands
from commands.spoiler_commands import register_spoiler_commands
from commands.watch_commands import init_watch_tables, load_watched_users, register_watch_commands
from commands.welcome_commands import register_welcome_commands
from commands.xp_commands import register_xp_commands
from db_utils import init_config_db, open_db
from handlers.events import register_events
from services.interaction_graph import init_interaction_tables
from services.message_store import init_message_tables
from reports import register_reports
from services.auto_delete_service import auto_delete_loop, init_auto_delete_tables
from services.inactivity_prune_service import inactivity_prune_loop, init_inactivity_prune_tables
from services.voice_xp_service import voice_xp_loop
from services.xp_service import handle_level_progress
from xp_system import init_xp_tables

# ==============================
# Bootstrap
# ==============================
load_dotenv()

_log_queue: logging.handlers.QueueHandler
_log_queue_listener: logging.handlers.QueueListener

def _setup_logging() -> None:
    """Route all log records through a queue so stream writes never block the event loop."""
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    import queue
    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(log_queue)
    listener = logging.handlers.QueueListener(log_queue, stream_handler, respect_handler_level=True)
    listener.start()

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(queue_handler)

    # Keep a reference so the listener isn't garbage-collected
    globals()["_log_queue_listener"] = listener

_setup_logging()

log = logging.getLogger("dungeonkeeper.bot")

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = Path(__file__).with_name("dungeonkeeper.db")

# ==============================
# Database initialization
# ==============================
init_config_db(DB_PATH)

with open_db(DB_PATH) as _conn:
    init_xp_tables(_conn)
    init_auto_delete_tables(_conn)
    init_watch_tables(_conn)
    init_inactivity_prune_tables(_conn)
    init_interaction_tables(_conn)
    init_message_tables(_conn)
    init_give_role_tables(_conn)

# ==============================
# Runtime config + context
# ==============================
_cfg = load_runtime_config(DB_PATH)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = Bot(intents=intents, debug=_cfg["debug"], guild_id=_cfg["guild_id"])

ctx = AppContext(
    bot=bot,
    log=log,
    db_path=DB_PATH,
    guild_id=_cfg["guild_id"],
    debug=_cfg["debug"],
    mod_channel_id=_cfg["mod_channel_id"],
    spoiler_required_channels=_cfg["spoiler_required_channels"],
    bypass_role_ids=_cfg["bypass_role_ids"],
    xp_grant_allowed_user_ids=_cfg["xp_grant_allowed_user_ids"],
    xp_excluded_channel_ids=_cfg["xp_excluded_channel_ids"],
    level_5_role_id=_cfg["xp_level_5_role_id"],
    level_5_log_channel_id=_cfg["xp_level_5_log_channel_id"],
    level_up_log_channel_id=_cfg["xp_level_up_log_channel_id"],
    greeter_role_id=_cfg["greeter_role_id"],
    denizen_role_id=_cfg["denizen_role_id"],
    denizen_log_channel_id=_cfg["denizen_log_channel_id"],
    denizen_announce_channel_id=_cfg["denizen_announce_channel_id"],
    denizen_grant_message=_cfg["denizen_grant_message"],
    nsfw_role_id=_cfg["nsfw_role_id"],
    nsfw_log_channel_id=_cfg["nsfw_log_channel_id"],
    nsfw_announce_channel_id=_cfg["nsfw_announce_channel_id"],
    nsfw_grant_message=_cfg["nsfw_grant_message"],
    veteran_role_id=_cfg["veteran_role_id"],
    veteran_log_channel_id=_cfg["veteran_log_channel_id"],
    veteran_announce_channel_id=_cfg["veteran_announce_channel_id"],
    veteran_grant_message=_cfg["veteran_grant_message"],
    kink_role_id=_cfg["kink_role_id"],
    kink_log_channel_id=_cfg["kink_log_channel_id"],
    kink_announce_channel_id=_cfg["kink_announce_channel_id"],
    kink_grant_message=_cfg["kink_grant_message"],
    goldengirl_role_id=_cfg["goldengirl_role_id"],
    goldengirl_log_channel_id=_cfg["goldengirl_log_channel_id"],
    goldengirl_announce_channel_id=_cfg["goldengirl_announce_channel_id"],
    goldengirl_grant_message=_cfg["goldengirl_grant_message"],
    welcome_channel_id=_cfg["welcome_channel_id"],
    welcome_message=_cfg["welcome_message"],
    welcome_ping_role_id=_cfg["welcome_ping_role_id"],
    leave_channel_id=_cfg["leave_channel_id"],
    leave_message=_cfg["leave_message"],
    tz_offset_hours=_cfg["tz_offset_hours"],
)

# ==============================
# Populate runtime state from DB
# ==============================
with open_db(DB_PATH) as _conn:
    ctx.watched_users = load_watched_users(_conn, _cfg["guild_id"])

# ==============================
# Event handlers + commands
# ==============================
register_events(bot, ctx)
register_activity_commands(bot, ctx)
register_interaction_commands(bot, ctx)
register_ai_mod_commands(bot, ctx)
register_xp_commands(bot, ctx)
register_denizen_commands(bot, ctx)
register_give_role_commands(bot, ctx)
register_spoiler_commands(bot, ctx)
register_auto_delete_commands(bot, ctx)
register_inactivity_prune_commands(bot, ctx)
register_drama_commands(bot, ctx)
register_mod_commands(bot, ctx)
register_config_commands(bot, ctx)
register_welcome_commands(bot, ctx)
register_reports(bot, ctx)
register_watch_commands(bot, ctx)

# ==============================
# Background tasks
# ==============================
async def _handle_level_progress_cb(member, award, source):
    await handle_level_progress(
        member,
        award,
        source,
        level_5_role_id=ctx.level_5_role_id,
        level_up_log_channel_id=ctx.level_up_log_channel_id,
        level_5_log_channel_id=ctx.level_5_log_channel_id,
    )

bot.startup_task_factories.append(
    lambda: voice_xp_loop(bot, DB_PATH, _handle_level_progress_cb)
)

bot.startup_task_factories.append(
    lambda: auto_delete_loop(bot, DB_PATH)
)

bot.startup_task_factories.append(
    lambda: inactivity_prune_loop(bot, DB_PATH)
)

# ==============================
# Run
# ==============================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(TOKEN, log_handler=None)
