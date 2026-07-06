import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
from pathlib import Path

import discord
from dotenv import load_dotenv

from bot_modules.core.app_context import AppContext, Bot, resolve_guild_id
from bot_modules.core.config import load_config
from bot_modules.core.id_remap import build_remap
from migrations import apply_migrations_sync
from bot_modules.core.safety import check_bot_identity, check_db_path, check_guild_membership, print_startup_banner
from bot_modules.services.watch_service import load_watched_users
from bot_modules.core.db_utils import get_tz_offset_hours, migrate_grant_roles, open_db
from bot_modules.services.auto_delete_service import auto_delete_loop
from bot_modules.services.bulk_cleanup_service import bulk_cleanup_loop
from bot_modules.services.scheduled_games_service import scheduled_games_loop
from bot_modules.services.booster_roles import BoosterRoleDynamicButton
from bot_modules.services.inactivity_prune_service import inactivity_prune_loop
from bot_modules.services.voice_xp_service import voice_xp_loop
from bot_modules.services.wellness_partners import (
    WellnessPartnerAcceptButton,
    WellnessPartnerDeclineButton,
)
from bot_modules.services.db_backup import db_backup_loop
from bot_modules.services.xp_service import handle_level_progress
from bot_modules.core.utils import format_guild_for_log
from bot_modules.services.games_db import GamesDb

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_log_queue_listener: logging.handlers.QueueListener | None = None


def _setup_logging() -> None:
    """Route all log records through a queue so stream writes never block the event loop."""
    global _log_queue_listener

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    log_path = _PROJECT_ROOT / "log.txt"
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

    llm_level = os.getenv("LOG_LEVEL_LLM", "").upper()
    if llm_level:
        logging.getLogger("dungeonkeeper.llm").setLevel(
            getattr(logging, llm_level, logging.DEBUG)
        )

    _log_queue_listener = listener


def main() -> None:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="DungeonKeeper Discord bot")
    parser.add_argument("--debug", action="store_true", help="Sync commands to dev guild only")
    args = parser.parse_args()

    _setup_logging()

    log = logging.getLogger("dungeonkeeper.bot")

    boot_cfg = load_config()
    check_db_path(boot_cfg)

    db_path = Path(boot_cfg.db_path)

    # ==============================
    # Database initialization
    # ==============================
    apply_migrations_sync(db_path)

    # ==============================
    # Runtime config + context
    # ==============================
    guild_id = resolve_guild_id(db_path, default_guild_id=boot_cfg.guild_id)

    intents = discord.Intents.default()
    intents.members = True
    intents.presences = True
    intents.message_content = True

    bot = Bot(intents=intents, debug=args.debug, guild_id=guild_id)

    ctx = AppContext(
        bot=bot,
        log=log,
        db_path=db_path,
        guild_id=guild_id,
        debug=args.debug,
    )

    # ==============================
    # Populate runtime state from DB
    # ==============================
    with open_db(db_path) as conn:
        migrate_grant_roles(conn, guild_id)
        ctx.watched_users = load_watched_users(conn, guild_id)

    # ==============================
    # Cog extensions
    # ==============================
    bot.ctx = ctx
    bot.games_db = GamesDb(db_path)
    bot.extension_names = [
        "bot_modules.cogs.events_cog",
        "bot_modules.cogs.role_grant_cog",
        "bot_modules.cogs.invite_cog",
        "bot_modules.cogs.support_cog",
        "bot_modules.cogs.jail_cog",
        "bot_modules.cogs.inactive_cog",
        "bot_modules.cogs.mod_cog",
        "bot_modules.cogs.hidden_channels_cog",
        "bot_modules.cogs.rename_cog",
        "bot_modules.cogs.privacy_cog",
        "bot_modules.cogs.reports_cog",
        "bot_modules.cogs.todo_cog",
        "bot_modules.cogs.docs_cog",
        "bot_modules.cogs.ai_mod_cog",
        "bot_modules.cogs.watch_cog",
        "bot_modules.cogs.rules_watch_cog",
        "bot_modules.rules_watch.monitor",
        "bot_modules.cogs.wellness_cog",
        "bot_modules.cogs.xp_cog",
        "bot_modules.cogs.confessions_cog",
        "bot_modules.cogs.dm_perms_cog",
        "bot_modules.cogs.birthday_cog",
        "bot_modules.cogs.dev_cog",
        "bot_modules.cogs.setup_cog",
        "bot_modules.cogs.starboard_cog",
        "bot_modules.cogs.music_cog",
        "bot_modules.cogs.voice_master_cog",
        "bot_modules.cogs.guess_cog",
        "bot_modules.cogs.quote_cog",
        "bot_modules.cogs.whisper_cog",
        "bot_modules.cogs.needle_cog",
        "bot_modules.cogs.auto_react_cog",
        "bot_modules.cogs.bump_tracker_cog",
        "bot_modules.cogs.emoji_stealer_cog",
        "bot_modules.cogs.risky_roll_cog",
        "bot_modules.cogs.pressure_cooker",
        "bot_modules.cogs.quickdraw",
        "bot_modules.cogs.hot_potato",
        "bot_modules.cogs.hot_potato_group",
        "bot_modules.cogs.musical_chairs",
        "bot_modules.cogs.chicken",
        "bot_modules.cogs.bios_cog",
        # ── Party Games (PoppyBot) ────────────────────────────────
        "bot_modules.cogs.games_session_cog",
        "bot_modules.cogs.games_config_cog",
        "bot_modules.cogs.games_help_cog",
        "bot_modules.cogs.games_ffa_cog",
        "bot_modules.cogs.games_photo_cog",
        "bot_modules.cogs.games_traditional_cog",
        "bot_modules.cogs.games_compliment_cog",
        "bot_modules.cogs.games_mfk_cog",
        "bot_modules.cogs.games_wyr_cog",
        "bot_modules.cogs.games_nhie_cog",
        "bot_modules.cogs.games_mlt_cog",
        "bot_modules.cogs.games_ttl_cog",
        "bot_modules.cogs.games_hottakes_cog",
        "bot_modules.cogs.games_story_cog",
        "bot_modules.cogs.games_ama_cog",
        "bot_modules.cogs.games_fantasies_cog",
        "bot_modules.cogs.games_price_cog",
        "bot_modules.cogs.games_rushmore_cog",
        "bot_modules.cogs.games_clapback_cog",
        "bot_modules.cogs.games_legitlibs",
        "bot_modules.cogs.pen_pals_cog",
        "bot_modules.cogs.voice_transcription_cog",
        "bot_modules.cogs.games_dev_cog",
        "bot_modules.cogs.games_external_cog",
    ]

    # ==============================
    # Safety on_ready checks (spec §8)
    # ==============================
    @bot.event
    async def on_ready() -> None:
        if bot.user:
            check_bot_identity(boot_cfg, bot.user)
        print_startup_banner(boot_cfg, bot.user)
        await check_guild_membership(boot_cfg, bot)

        if boot_cfg.is_dev:
            guild = bot.get_guild(boot_cfg.guild_id)
            if guild:
                import aiosqlite
                async with aiosqlite.connect(str(db_path)) as remap_db:
                    remap_db.row_factory = aiosqlite.Row
                    await build_remap(remap_db, guild)

    # Register persistent booster-role buttons so they survive restarts.
    bot.add_dynamic_items(BoosterRoleDynamicButton)

    # Register persistent wellness-partner request buttons so DM Accept/Decline survive restarts
    bot.add_dynamic_items(WellnessPartnerAcceptButton, WellnessPartnerDeclineButton)

    # Register persistent Rules Watch label buttons for unlabeled events
    from bot_modules.rules_watch.alert import register_persistent_views as _rw_register_views
    _rw_register_views(bot, db_path)

    # ==============================
    # Background tasks
    # ==============================
    async def _handle_level_progress_cb(member, award, source):
        cfg = ctx.guild_config(member.guild.id)
        await handle_level_progress(
            member,
            award,
            source,
            level_5_role_id=cfg.level_5_role_id,
            level_up_log_channel_id=cfg.level_up_log_channel_id,
            level_5_log_channel_id=cfg.level_5_log_channel_id,
            settings=cfg.xp_settings,
        )

    bot.startup_task_factories.append(
        lambda: voice_xp_loop(
            bot,
            db_path,
            _handle_level_progress_cb,
            settings_for=lambda gid: ctx.guild_config(gid).xp_settings,
        )
    )

    bot.startup_task_factories.append(lambda: auto_delete_loop(bot, db_path))

    bot.startup_task_factories.append(lambda: bulk_cleanup_loop(bot, db_path))

    bot.startup_task_factories.append(lambda: scheduled_games_loop(bot))

    bot.startup_task_factories.append(lambda: inactivity_prune_loop(bot, db_path))

    bot.startup_task_factories.append(lambda: db_backup_loop(bot, db_path))

    # ==============================
    # Games — crash recovery (re-register in-flight views/timers on boot)
    # ==============================
    async def _game_recovery() -> None:
        from bot_modules.games.utils.recovery import recover_active_games

        await bot.wait_until_ready()
        try:
            await recover_active_games(bot)
        except Exception:
            log.exception("Game recovery sweep failed")

    bot.startup_task_factories.append(lambda: _game_recovery())

    # ==============================
    # Games — 24-hour cleanup sweep
    # ==============================
    async def _game_cleanup_loop() -> None:
        """Hourly sweep — end any party game older than 24 hours."""
        from bot_modules.games.utils.game_manager import end_game  # type: ignore[import-untyped]

        await bot.wait_until_ready()
        while not bot.is_closed():
            await asyncio.sleep(3600)
            try:
                rows = await bot.games_db.fetchall(  # type: ignore[attr-defined]
                    "SELECT game_id, channel_id, game_type FROM games_active_games "
                    "WHERE created_at <= datetime('now', '-24 hours')"
                )
                for row in rows:
                    game_id = row["game_id"]
                    await end_game(bot.games_db, game_id)  # type: ignore[attr-defined]
                    if row["game_type"] == "ama":
                        ama_cog = bot.get_cog("AMACog")
                        if ama_cog and hasattr(ama_cog, "cleanup_ended_game"):
                            await ama_cog.cleanup_ended_game(row["channel_id"], game_id)  # type: ignore[attr-defined]
                    bot.active_views.pop(game_id, None)  # type: ignore[attr-defined]
                    log.info("Auto-expired game %s (24 h limit)", game_id)
            except Exception:
                log.exception("Game cleanup error")

    bot.startup_task_factories.append(lambda: _game_cleanup_loop())

    # ==============================
    # Startup backfill — score messages missing calculated fields
    # ==============================
    async def _startup_backfill() -> None:
        await bot.wait_until_ready()

        guild_ids = [g.id for g in bot.guilds]
        if not guild_ids:
            log.info("Startup backfill: bot is not in any guilds")
            return

        def _run(gid: int) -> None:
            from bot_modules.services.sentiment_service import backfill

            with open_db(db_path) as conn:
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
                        from bot_modules.services.health_metrics import (
                            compute_channel_health,
                            compute_churn_risk,
                            compute_cohort_retention,
                            compute_dau_mau,
                            compute_gini,
                            compute_heatmap,
                            compute_mod_workload,
                            compute_newcomer_funnel,
                            compute_sentiment,
                            compute_social_graph,
                        )
                        from bot_modules.services.health_service import set_cached
                        from bot_modules.services.sentiment_service import analyze_batch

                        with open_db(db_path) as conn:
                            analyze_batch(conn, gid, batch_size=500)
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
        await bot.wait_until_ready()
        while not bot.is_closed():
            for guild in list(bot.guilds):
                try:
                    from bot_modules.services.reports_data import MemberSnapshot

                    gid = guild.id
                    with open_db(db_path) as conn:
                        tz = get_tz_offset_hours(conn, gid)
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
                        from bot_modules.services import reports_data
                        from web_server.deps import store_report_result

                        TTL = 3600

                        def _put(name, params, result):
                            store_report_result(name, gid, params, result, TTL)

                        with open_db(db_path) as conn:
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
        from web_server.server import serve_forever as _dashboard_serve_forever

        # Default to loopback — public access should go through the Cloudflare
        # tunnel, and open-auth on 0.0.0.0 would expose a full-admin dashboard.
        _dashboard_host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
        _dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
        bot.startup_task_factories.append(
            lambda: _dashboard_serve_forever(ctx, _dashboard_host, _dashboard_port)
        )
        log.info("Dashboard enabled — will bind %s:%d", _dashboard_host, _dashboard_port)

    # ==============================
    # Graceful shutdown
    # ==============================
    async def _shutdown() -> None:
        log.info("Shutting down gracefully...")

        for task in bot.startup_tasks:
            if not task.done():
                task.cancel()
        if bot.startup_tasks:
            await asyncio.gather(*bot.startup_tasks, return_exceptions=True)
        log.info("Background tasks cancelled")

        try:
            from bot_modules.services.interaction_graph import _layout_executor

            _layout_executor.shutdown(wait=False)
        except (ImportError, AttributeError):
            pass

        try:
            await asyncio.wait_for(
                bot.unload_extension("bot_modules.cogs.music_cog"), timeout=15.0
            )
        except (asyncio.TimeoutError, Exception) as exc:
            log.warning("music cog unload failed/timed out: %s", exc)

        if not bot.is_closed():
            await bot.close()
        log.info("Bot closed")

        if _log_queue_listener:
            _log_queue_listener.stop()

    async def _run_bot() -> None:
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))
            except NotImplementedError:
                pass

        try:
            async with bot:
                await bot.start(boot_cfg.token)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await _shutdown()

    asyncio.run(_run_bot())


if __name__ == "__main__":
    main()
