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
from commands.gender_commands import register_gender_commands
from commands.inactivity_prune_commands import register_inactivity_prune_commands
from commands.jail_commands import register_jail_commands
from commands.mod_commands import register_mod_commands
from commands.spoiler_commands import register_spoiler_commands
from commands.watch_commands import init_watch_tables, load_watched_users, register_watch_commands
from commands.foolsday_commands import register_foolsday_commands
from commands.welcome_commands import register_welcome_commands
from commands.wellness_admin_commands import register_wellness_admin_commands
from commands.wellness_commands import register_wellness_commands
from commands.xp_commands import register_xp_commands
from db_utils import init_config_db, init_grant_role_tables, migrate_grant_roles, open_db
from handlers.events import register_events
from services.interaction_graph import init_interaction_tables
from services.invite_tracker import init_invite_tables
from services.message_store import init_known_channels_table, init_known_users_table, init_message_tables
from reports import register_reports
from services.auto_delete_service import auto_delete_loop, init_auto_delete_tables
from services.booster_roles import BoosterRoleDynamicButton, init_booster_role_tables
from services.gender_service import init_gender_tables
from services.member_quality_score import init_quality_score_tables
from services.moderation import init_moderation_tables
from services.inactivity_prune_service import inactivity_prune_loop, init_inactivity_prune_tables
from services.voice_xp_service import voice_xp_loop
from services.wellness_partners import (
    WellnessPartnerAcceptButton,
    WellnessPartnerDeclineButton,
)
from services.wellness_scheduler import (
    wellness_active_list_loop,
    wellness_tick_loop,
    wellness_weekly_report_loop,
)
from services.wellness_service import init_wellness_tables
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
    init_invite_tables(_conn)
    init_message_tables(_conn)
    init_known_users_table(_conn)
    init_known_channels_table(_conn)
    init_grant_role_tables(_conn)
    init_booster_role_tables(_conn)
    init_quality_score_tables(_conn)
    init_gender_tables(_conn)
    init_moderation_tables(_conn)
    init_wellness_tables(_conn)

# ==============================
# Runtime config + context
# ==============================
_cfg = load_runtime_config(DB_PATH)

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True

bot = Bot(intents=intents, debug=_cfg["debug"], guild_id=_cfg["guild_id"])
bot.db_path = DB_PATH  # type: ignore[attr-defined]  # used by persistent button callbacks

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
    greeter_chat_channel_id=_cfg["greeter_chat_channel_id"],
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
    migrate_grant_roles(_conn, _cfg["guild_id"])
    ctx.watched_users = load_watched_users(_conn, _cfg["guild_id"])

ctx.reload_grant_roles()

# ==============================
# Event handlers + commands
# ==============================
register_events(bot, ctx)
register_activity_commands(bot, ctx)
register_interaction_commands(bot, ctx)
register_ai_mod_commands(bot, ctx)
register_xp_commands(bot, ctx)
register_denizen_commands(bot, ctx)
register_spoiler_commands(bot, ctx)
register_auto_delete_commands(bot, ctx)
register_inactivity_prune_commands(bot, ctx)
register_drama_commands(bot, ctx)
register_mod_commands(bot, ctx)
register_config_commands(bot, ctx)
register_welcome_commands(bot, ctx)
register_reports(bot, ctx)
register_watch_commands(bot, ctx)
register_foolsday_commands(bot, ctx)
register_gender_commands(bot, ctx)
register_jail_commands(bot, ctx)
register_wellness_admin_commands(bot, ctx)
register_wellness_commands(bot, ctx)

# Register persistent booster-role buttons so they survive restarts
bot.add_dynamic_items(BoosterRoleDynamicButton)

# Register persistent wellness-partner request buttons so DM Accept/Decline survive restarts
bot.add_dynamic_items(WellnessPartnerAcceptButton, WellnessPartnerDeclineButton)

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

bot.startup_task_factories.append(
    lambda: wellness_tick_loop(bot, DB_PATH)
)

bot.startup_task_factories.append(
    lambda: wellness_active_list_loop(bot, DB_PATH)
)

bot.startup_task_factories.append(
    lambda: wellness_weekly_report_loop(bot, DB_PATH)
)

# ==============================
# Optional web dashboard (LAN, opt-in via DASHBOARD_ENABLED=1)
# ==============================
if os.getenv("DASHBOARD_ENABLED") == "1":
    from web.server import serve_forever as _dashboard_serve_forever
    _dashboard_host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    _dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
    bot.startup_task_factories.append(
        lambda: _dashboard_serve_forever(ctx, _dashboard_host, _dashboard_port)
    )
    log.info("Dashboard enabled — will bind %s:%d", _dashboard_host, _dashboard_port)

# ==============================
# Run
# ==============================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(TOKEN, log_handler=None)
