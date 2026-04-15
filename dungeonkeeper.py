import asyncio
import logging
import logging.handlers
import os
import signal
from pathlib import Path

import discord
from dotenv import load_dotenv

from app_context import AppContext, Bot, load_runtime_config
from commands.activity_commands import register_activity_commands
from commands.ai_mod_commands import register_ai_mod_commands
from commands.auto_delete_commands import register_auto_delete_commands
from commands.config_commands import register_config_commands
from commands.denizen_commands import register_denizen_commands
from commands.drama_commands import register_drama_commands
from commands.foolsday_commands import register_foolsday_commands
from commands.gender_commands import register_gender_commands
from commands.inactivity_prune_commands import register_inactivity_prune_commands
from commands.interaction_commands import register_interaction_commands
from commands.invite_commands import register_invite_commands
from commands.jail_commands import register_jail_commands
from commands.mod_commands import register_mod_commands
from commands.spoiler_commands import register_spoiler_commands
from commands.watch_commands import (
    init_watch_tables,
    load_watched_users,
    register_watch_commands,
)
from commands.welcome_commands import register_welcome_commands
from commands.wellness_admin_commands import register_wellness_admin_commands
from commands.wellness_commands import register_wellness_commands
from commands.xp_commands import register_xp_commands
from db_utils import (
    init_config_db,
    init_grant_role_tables,
    migrate_grant_roles,
    open_db,
)
from handlers.events import register_events
from reports import register_reports
from services.auto_delete_service import auto_delete_loop, init_auto_delete_tables
from services.booster_roles import BoosterRoleDynamicButton, init_booster_role_tables
from services.gender_service import init_gender_tables
from services.health_service import init_health_tables
from services.inactivity_prune_service import (
    inactivity_prune_loop,
    init_inactivity_prune_tables,
)
from services.interaction_graph import init_interaction_tables
from services.invite_tracker import init_invite_tables
from services.member_quality_score import init_quality_score_tables
from services.message_store import (
    init_known_channels_table,
    init_known_users_table,
    init_message_tables,
)
from services.moderation import init_moderation_tables
from services.voice_xp_service import voice_xp_loop
from services.wellness_partners import (
    WellnessPartnerAcceptButton,
    WellnessPartnerDeclineButton,
)
from services.db_backup import db_backup_loop
from services.wellness_scheduler import (
    wellness_active_list_loop,
    wellness_tick_loop,
    wellness_weekly_report_loop,
)
from services.wellness_service import init_wellness_tables
from services.xp_service import handle_level_progress
from utils import format_guild_for_log
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

    file_handler = logging.handlers.RotatingFileHandler(
        Path(__file__).with_name("log.txt"),
        encoding="utf-8",
        maxBytes=10_000 * 200,
        backupCount=1,  # ~10 000 lines
    )
    file_handler.setFormatter(formatter)

    import queue

    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(log_queue)
    listener = logging.handlers.QueueListener(
        log_queue, stream_handler, file_handler, respect_handler_level=True
    )
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
    init_health_tables(_conn)

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
    recorded_bot_user_ids=_cfg["recorded_bot_user_ids"],
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
ctx.reload_xp_settings()

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
register_invite_commands(bot, ctx)
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
        settings=ctx.xp_settings,
    )


bot.startup_task_factories.append(
    lambda: voice_xp_loop(
        bot, DB_PATH, _handle_level_progress_cb, settings_getter=lambda: ctx.xp_settings
    )
)

bot.startup_task_factories.append(lambda: auto_delete_loop(bot, DB_PATH))

bot.startup_task_factories.append(lambda: inactivity_prune_loop(bot, DB_PATH))

bot.startup_task_factories.append(lambda: wellness_tick_loop(bot, DB_PATH))

bot.startup_task_factories.append(lambda: wellness_active_list_loop(bot, DB_PATH))

bot.startup_task_factories.append(lambda: wellness_weekly_report_loop(bot, DB_PATH))

bot.startup_task_factories.append(lambda: db_backup_loop(bot, DB_PATH))


# ==============================
# Startup backfill — score messages missing calculated fields
# ==============================
async def _startup_backfill() -> None:
    """One-time check on startup for messages missing VADER sentiment scores.

    Iterates every guild the bot is in so multi-guild deployments all get
    backfilled.
    """
    await bot.wait_until_ready()

    guild_ids = [g.id for g in bot.guilds]
    if not guild_ids:
        log.info("Startup backfill: bot is not in any guilds")
        return

    def _run(gid: int) -> None:
        from services.sentiment_service import backfill

        with open_db(DB_PATH) as conn:
            missing = conn.execute(
                "SELECT COUNT(*) FROM messages m "
                "LEFT JOIN message_sentiment ms ON m.message_id = ms.message_id "
                "WHERE m.guild_id = ? AND ms.message_id IS NULL "
                "AND m.content IS NOT NULL AND m.content != ''",
                (gid,),
            ).fetchone()[0]
            if missing == 0:
                log.info(
                    "Startup backfill [%d]: all messages have sentiment scores", gid
                )
                return
            log.info(
                "Startup backfill [%d]: %d messages missing sentiment scores, backfilling...",
                gid,
                missing,
            )
            scored = backfill(conn, gid, max_messages=missing)
            log.info("Startup backfill [%d]: scored %d messages", gid, scored)

    for gid in guild_ids:
        try:
            await asyncio.to_thread(_run, gid)
        except Exception:
            log.exception("Startup backfill failed for guild %d", gid)


bot.startup_task_factories.append(lambda: _startup_backfill())


# ==============================
# Health dashboard batch loop
# ==============================
async def _health_batch_loop() -> None:
    """Refresh health metrics cache and run sentiment scoring every 15 min.

    Runs the batch once per guild the bot is in so multi-guild deployments
    have up-to-date metrics everywhere.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in list(bot.guilds):
            try:
                gid = guild.id
                _member_count = guild.member_count or 0
                _voice_active = 0
                _nsfw_ids: list[int] = []
                _mod_ids: list[int] = []
                _recent_joins: dict[int, float] = {}
                import time as _t

                _now = _t.time()
                for _vc in guild.voice_channels:
                    _voice_active += sum(1 for _m in _vc.members if not _m.bot)
                _nsfw_ids = [
                    ch.id for ch in guild.channels if getattr(ch, "nsfw", False)
                ]
                for _m in guild.members:
                    if _m.bot:
                        continue
                    _perms = _m.guild_permissions
                    if (
                        _perms.administrator
                        or _perms.manage_guild
                        or _perms.kick_members
                        or _perms.ban_members
                    ):
                        _mod_ids.append(_m.id)
                for _m in guild.members:
                    if not _m.bot and _m.joined_at:
                        _age = _now - _m.joined_at.timestamp()
                        if _age < 90 * 86400:
                            _recent_joins[_m.id] = _m.joined_at.timestamp()

                def _batch(
                    gid: int = gid,
                    _member_count: int = _member_count,
                    _voice_active: int = _voice_active,
                    _nsfw_ids: list[int] = _nsfw_ids,
                    _mod_ids: list[int] = _mod_ids,
                    _recent_joins: dict[int, float] = _recent_joins,
                ) -> None:
                    from services.health_metrics import (
                        compute_channel_health,
                        compute_churn_risk,
                        compute_cohort_retention,
                        compute_dau_mau,
                        compute_gini,
                        compute_heatmap,
                        compute_incidents,
                        compute_mod_workload,
                        compute_newcomer_funnel,
                        compute_sentiment,
                        compute_social_graph,
                    )
                    from services.health_service import set_cached
                    from services.incident_detection import update_baselines
                    from services.sentiment_service import analyze_batch

                    with open_db(DB_PATH) as conn:
                        analyze_batch(conn, gid, batch_size=500)
                        update_baselines(conn, gid)
                        data = compute_dau_mau(
                            conn,
                            gid,
                            member_count=_member_count,
                            voice_active_count=_voice_active,
                        )
                        set_cached(conn, gid, "dau_mau", data)
                        data = compute_heatmap(conn, gid)
                        set_cached(conn, gid, "heatmap", data)
                        data = compute_channel_health(
                            conn, gid, nsfw_channel_ids=_nsfw_ids
                        )
                        set_cached(conn, gid, "channel_health", data)
                        data = compute_gini(conn, gid)
                        set_cached(conn, gid, "gini", data)
                        data = compute_social_graph(
                            conn, gid, nsfw_channel_ids=_nsfw_ids
                        )
                        set_cached(conn, gid, "social_graph", data)
                        data = compute_sentiment(conn, gid)
                        set_cached(conn, gid, "sentiment", data)
                        data = compute_newcomer_funnel(
                            conn, gid, recent_join_ids=_recent_joins
                        )
                        set_cached(conn, gid, "newcomer_funnel", data)
                        data = compute_cohort_retention(
                            conn, gid, join_times=_recent_joins
                        )
                        set_cached(conn, gid, "cohort_retention", data)
                        data = compute_churn_risk(conn, gid)
                        set_cached(conn, gid, "churn_risk", data)
                        data = compute_mod_workload(conn, gid, mod_ids=_mod_ids)
                        set_cached(conn, gid, "mod_workload", data)
                        data = compute_incidents(conn, gid)
                        set_cached(conn, gid, "incidents", data)

                await asyncio.to_thread(_batch)
                log.info(
                    "Health metrics batch completed for guild %s",
                    format_guild_for_log(guild),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Health metrics batch failed for guild %s",
                    format_guild_for_log(guild),
                )
        await asyncio.sleep(900)  # 15 minutes


bot.startup_task_factories.append(lambda: _health_batch_loop())

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
# Graceful shutdown
# ==============================
async def _shutdown() -> None:
    """Cancel background tasks, close the bot, and stop the log listener."""
    log.info("Shutting down gracefully...")

    # Cancel all background tasks and give them a moment to finish
    for task in bot.startup_tasks:
        if not task.done():
            task.cancel()
    if bot.startup_tasks:
        await asyncio.gather(*bot.startup_tasks, return_exceptions=True)
    log.info("Background tasks cancelled")

    # Close the interaction graph thread pool if loaded
    try:
        from services.interaction_graph import _layout_executor

        _layout_executor.shutdown(wait=False)
    except (ImportError, AttributeError):
        pass

    # Close the bot (disconnects from Discord gateway)
    if not bot.is_closed():
        await bot.close()
    log.info("Bot closed")

    # Flush and stop the log listener
    listener = globals().get("_log_queue_listener")
    if listener:
        listener.stop()


async def _run_bot() -> None:
    """Start the bot with proper signal handling for graceful shutdown."""
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    loop = asyncio.get_running_loop()

    # Install signal handlers (Unix-style; on Windows only SIGINT works via loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler; fall back to
            # KeyboardInterrupt handling below
            pass

    try:
        async with bot:
            await bot.start(TOKEN)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await _shutdown()


# ==============================
# Run
# ==============================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    asyncio.run(_run_bot())
