import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
from pathlib import Path

import discord
from dotenv import load_dotenv

from app_context import AppContext, Bot, load_runtime_config
from config import load_config
from id_remap import build_remap
from migrations import apply_migrations_sync
from safety import check_bot_identity, check_db_path, check_guild_membership, print_startup_banner
from commands.watch_commands import load_watched_users
from db_utils import migrate_grant_roles, open_db
from services.auto_delete_service import auto_delete_loop
from services.booster_roles import BoosterRoleDynamicButton
from services.inactivity_prune_service import inactivity_prune_loop
from services.voice_xp_service import voice_xp_loop
from services.wellness_partners import (
    WellnessPartnerAcceptButton,
    WellnessPartnerDeclineButton,
)
from services.db_backup import db_backup_loop
from services.xp_service import handle_level_progress
from utils import format_guild_for_log

# ==============================
# Bootstrap
# ==============================
load_dotenv()

_parser = argparse.ArgumentParser(description="DungeonKeeper Discord bot")
_parser.add_argument("--debug", action="store_true", help="Sync commands to dev guild only")
_args = _parser.parse_args()

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

    log_path = Path(__file__).with_name("log.txt")
    log_path.write_text("", encoding="utf-8")  # wipe on boot
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
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

_boot_cfg = load_config()
check_db_path(_boot_cfg)

DB_PATH = Path(_boot_cfg.db_path)

# ==============================
# Database initialization
# ==============================
apply_migrations_sync(DB_PATH)

# ==============================
# Runtime config + context
# ==============================
_cfg = load_runtime_config(DB_PATH, debug=_args.debug)

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
    join_leave_log_channel_id=_cfg["join_leave_log_channel_id"],
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
ctx.reload_permission_roles()

# ==============================
# Cog extensions
# ==============================
bot.ctx = ctx
bot.extension_names = [
    "cogs.events_cog",
    "cogs.ai_mod_cog",
    "cogs.denizen_cog",
    "cogs.foolsday_cog",
    "cogs.interaction_cog",
    "cogs.invite_cog",
    "cogs.jail_cog",
    "cogs.mod_cog",
    "cogs.privacy_cog",
    "cogs.reports_cog",
    "cogs.todo_cog",
    "cogs.watch_cog",
    "cogs.welcome_cog",
    "cogs.wellness_admin_cog",
    "cogs.wellness_cog",
    "cogs.xp_cog",
    "cogs.confessions_cog",
    "cogs.dm_perms_cog",
    "cogs.birthday_cog",
    "cogs.dev_cog",
    "cogs.setup_cog",
    "cogs.starboard_cog",
]

# ==============================
# Safety on_ready checks (spec §8)
# ==============================
@bot.event
async def on_ready() -> None:
    if bot.user:
        check_bot_identity(_boot_cfg, bot.user)
    print_startup_banner(_boot_cfg, bot.user)
    await check_guild_membership(_boot_cfg, bot)

    if _boot_cfg.is_dev:
        guild = bot.get_guild(_boot_cfg.guild_id)
        if guild:
            import aiosqlite
            async with aiosqlite.connect(str(DB_PATH)) as _remap_db:
                _remap_db.row_factory = aiosqlite.Row
                await build_remap(_remap_db, guild)


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
# Reports page cache warmer
# ==============================
async def _reports_batch_loop() -> None:
    """Pre-warm the metrics/reports page cache every hour.

    Runs each default-param DB query in a background thread and writes the
    result directly into ``web.deps._report_cache`` so the first page load
    after a restart is also instant.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in list(bot.guilds):
            try:
                from services.reports_data import MemberSnapshot

                gid = guild.id
                tz = getattr(ctx, "tz_offset_hours", 0.0)
                nsfw_ids: list[int] = [
                    ch.id for ch in guild.channels if getattr(ch, "nsfw", False)
                ]
                member_snapshots = [
                    MemberSnapshot(
                        user_id=m.id,
                        display_name=m.display_name,
                        is_bot=m.bot,
                        joined_at=m.joined_at.timestamp() if m.joined_at else None,
                        role_ids=tuple(r.id for r in m.roles),
                    )
                    for m in guild.members
                ]

                def _warm(
                    gid: int = gid,
                    tz: float = tz,
                    nsfw_ids: list[int] = nsfw_ids,
                    member_snapshots: list = member_snapshots,
                ) -> None:
                    from services import reports_data
                    from web.deps import store_report_result

                    TTL = 3600

                    def _put(name, params, result):
                        store_report_result(name, gid, params, result, TTL)

                    with open_db(DB_PATH) as conn:
                        for fn, name, params, kwargs in [
                            (reports_data.get_role_growth_data,
                             "role-growth", {"resolution": "week", "roles": None},
                             {"resolution": "week", "role_filter": None, "utc_offset_hours": tz}),
                            (reports_data.get_message_cadence_data,
                             "message-cadence", {"resolution": "hour", "channel_id": None},
                             {"resolution": "hour", "utc_offset_hours": tz, "channel_id": None}),
                            (reports_data.get_message_rate_data,
                             "message-rate", {"days": 30},
                             {"days": 30, "utc_offset_hours": tz}),
                            (reports_data.get_activity_data,
                             "activity",
                             {"resolution": "day", "mode": "xp", "user_id": None,
                              "channel_id": None, "exclude_channel_ids": "", "exclude_user_ids": ""},
                             {"resolution": "day", "utc_offset_hours": tz, "mode": "xp"}),
                            (reports_data.get_invite_effectiveness_data,
                             "invite-effectiveness", {"days": None, "active_days": 30},
                             {"days": None, "active_days": 30}),
                            (reports_data.get_interaction_graph_data,
                             "interaction-graph",
                             {"days": None, "limit": 50, "metrics": False, "res": 1.2},
                             {"days": None, "limit": 50, "include_metrics": False, "clustering_resolution": 1.2}),
                            (reports_data.get_retention_data,
                             "retention", {"period_days": 3, "min_previous": 5},
                             {"period_days": 3, "min_previous": 5}),
                            (reports_data.get_voice_activity_data,
                             "voice-activity", {"days": None},
                             {"days": None, "utc_offset_hours": tz}),
                            (reports_data.get_xp_leaderboard_data,
                             "xp-leaderboard", {"days": None},
                             {"days": None}),
                            (reports_data.get_reaction_analytics_data,
                             "reaction-analytics", {"days": None},
                             {"days": None}),
                            (reports_data.get_message_rate_drops_data,
                             "message-rate-drops", {"period_days": 2, "min_previous": 100},
                             {"period_days": 2, "min_previous": 100}),
                            (reports_data.get_burst_ranking_data,
                             "burst-ranking", {"min_sessions": 3, "days": None},
                             {"min_sessions": 3, "days": None}),
                            (reports_data.get_channel_comparison_data,
                             "channel-comparison", {"days": 1},
                             {"days": 1}),
                            (reports_data.get_animated_heatmap_data,
                             "interaction-heatmap",
                             {"resolution": "week", "days": 90, "top_n": 20},
                             {"resolution": "week", "days": 90, "top_n": 20}),
                        ]:
                            try:
                                _put(name, params, fn(conn, gid, **kwargs))
                            except Exception:
                                log.exception("Reports cache warming failed for %s", name)

                        # join-times needs the member snapshot list, not a conn arg
                        try:
                            _put(
                                "join-times",
                                {"resolution": "hour_of_day"},
                                reports_data.get_join_times_data(member_snapshots, "hour_of_day", tz),
                            )
                        except Exception:
                            log.exception("Reports cache warming failed for join-times")

                        if nsfw_ids:
                            try:
                                _put(
                                    "nsfw-gender",
                                    {"resolution": "week", "media_only": False, "channel_id": None},
                                    reports_data.get_nsfw_gender_data(conn, gid, "week", nsfw_ids, tz, False),
                                )
                            except Exception:
                                log.exception("Reports cache warming failed for nsfw-gender")

                await asyncio.to_thread(_warm)
                log.info("Reports cache warmed for guild %s", format_guild_for_log(guild))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Reports cache warming failed for guild %s", format_guild_for_log(guild)
                )
        await asyncio.sleep(3600)  # 1 hour


bot.startup_task_factories.append(lambda: _reports_batch_loop())

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
            await bot.start(_boot_cfg.token)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await _shutdown()


# ==============================
# Run
# ==============================
if __name__ == "__main__":
    asyncio.run(_run_bot())
