"""Daily feature-channel rotation — storage, Discord side, and the loop.

Layering matches the rest of the bot: everything derivable lives in
``bot_modules.feature_rotation.logic`` as pure functions; this module does the
I/O those functions describe.

Two independent once-a-day actions, each guarded by its own claimed date:

* the **flip** at local midnight — locked there because the economy's quest
  board freezes its pool at ``date.toordinal(local_day)`` and a flip at any
  other hour would leave the open room and the board disagreeing until it
  landed;
* the **announcement** at ``announce_hour`` — configurable, because the flip
  happens while the server is asleep and the post should not.

Hiding is an in-place ``view_channel`` deny on ``@everyone``, keeping every
other overwrite. That preserves the channel's position and category (a daily
category move would reshuffle every member's sidebar twice a day) and leaves
role-level grants intact, so staff keep eyes on a hidden room.

A pool row that names a ``launch_game`` also has its game's lifecycle ridden by
the flip, in the order **end → hide → show → start**: the outgoing room's game
is ended while its channel is still visible, so its recap lands where members
can still read it, and the incoming room's game launches once its channel is
open. Rooms that name no game are untouched — the rotation manages only the
lifecycles it was told about.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.feature_rotation.logic import (
    GamePlan,
    Room,
    RotationDay,
    VisibilityPlan,
    build_announcement,
    local_day,
    local_hour,
    parse_launch_options,
)
from bot_modules.feature_rotation.store import (
    RotationConfig,
    claim_announce,
    claim_flip,
    get_config,
    get_room_snapshot,
    list_pool,
    mark_hidden,
    mark_visible,
    release_announce,
    release_flip,
    rotation_day,
    rotation_tz,
)
from bot_modules.hidden_channels.overwrites import (
    rebuild_overwrites,
    serialize_overwrites,
)

log = logging.getLogger("dungeonkeeper.feature_rotation")

POLL_SECONDS = 60
DEFAULT_ACCENT = discord.Color.blurple()


# ── Discord side ─────────────────────────────────────────────────────────────


def _hidden_overwrites(
    channel: discord.abc.GuildChannel, guild: discord.Guild
) -> dict:
    """The channel's own overwrites with ``@everyone`` denied view.

    Deliberately a copy-and-amend rather than a replacement: role-level grants
    survive, so moderators keep seeing a hidden room, and restoring is a
    question of putting back exactly what was there.
    """
    updated = dict(channel.overwrites)
    target = updated.get(guild.default_role)
    target = discord.PermissionOverwrite() if target is None else target
    target.update(view_channel=False)
    updated[guild.default_role] = target
    me_overwrite = updated.get(guild.me) or discord.PermissionOverwrite()
    me_overwrite.update(view_channel=True)
    updated[guild.me] = me_overwrite
    return updated


async def hide_room(bot, guild: discord.Guild, channel_id: int, reason: str) -> bool:
    """Hide one pool channel in place. Returns whether anything changed."""
    channel = guild.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.abc.GuildChannel):
        return False

    def _read() -> bool:
        with bot.ctx.open_db() as conn:
            _, hidden = get_room_snapshot(conn, guild.id, channel_id)
            return hidden

    if await asyncio.to_thread(_read):
        return False

    stored = serialize_overwrites(channel.overwrites)
    now = time.time()

    def _write() -> None:
        with bot.ctx.open_db() as conn:
            mark_hidden(conn, guild.id, channel_id, stored, now)

    def _rollback() -> None:
        with bot.ctx.open_db() as conn:
            mark_visible(conn, guild.id, channel_id)

    await asyncio.to_thread(_write)
    try:
        await channel.edit(  # type: ignore[union-attr]
            overwrites=_hidden_overwrites(channel, guild), reason=reason
        )
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Rotation: failed to hide channel %s", channel_id)
        with contextlib.suppress(sqlite3.Error):
            await asyncio.to_thread(_rollback)
        return False
    return True


async def show_room(bot, guild: discord.Guild, channel_id: int, reason: str) -> bool:
    """Restore one pool channel's saved overwrites. Returns whether it changed."""
    channel = guild.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.abc.GuildChannel):
        return False

    def _read() -> tuple[list[dict], bool]:
        with bot.ctx.open_db() as conn:
            return get_room_snapshot(conn, guild.id, channel_id)

    stored, hidden = await asyncio.to_thread(_read)
    if not hidden:
        return False
    try:
        await channel.edit(  # type: ignore[union-attr]
            overwrites=rebuild_overwrites(stored, guild), reason=reason
        )
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Rotation: failed to restore channel %s", channel_id)
        return False

    def _clear() -> None:
        with bot.ctx.open_db() as conn:
            mark_visible(conn, guild.id, channel_id)

    await asyncio.to_thread(_clear)
    return True


async def apply_plan(
    bot, guild: discord.Guild, plan: VisibilityPlan, reason: str
) -> tuple[int, int]:
    """Bring every named channel into line with ``plan``. Returns (shown, hidden).

    Takes the plan rather than a day so the dashboard can apply the
    reopen-everything plan when the rotation is switched off, which is exactly
    the moment no derived day exists.
    """
    shown = hidden = 0
    for channel_id in plan.show:
        if await show_room(bot, guild, channel_id, reason):
            shown += 1
    for channel_id in plan.hide:
        if await hide_room(bot, guild, channel_id, reason):
            hidden += 1
    return shown, hidden


async def apply_day(bot, guild: discord.Guild, day: RotationDay, reason: str) -> tuple[int, int]:
    """Bring every pool channel into line with ``day``. Returns (shown, hidden)."""
    return await apply_plan(bot, guild, day.plan, reason)


# ── the game lifecycle ───────────────────────────────────────────────────────


async def end_room_game(
    bot, guild: discord.Guild, channel_id: int, game_key: str = ""
) -> bool:
    """End whatever game is running in one room. Returns whether one ended.

    Three ways out, all tried, because the two games in scope keep their state
    in different places.

    1. A **registered channel closer** (``bot.game_channel_closers[game_key]``)
       for a game whose rounds live outside ``games_active_games`` — Risky
       Rolls keeps its in memory, so no table lookup can see them. Its closer
       routes through the round's real resolution, so a winner is picked and
       the prompts go out; closing early is the same event as the timer running
       out, just sooner.
    2. A **view that exposes** ``close_now`` for a game in that table: the
       game's own completion site, so an AMA posts its recap embed and pays its
       roster exactly as if the host had pressed the button.
    3. ``force_end_active_game`` otherwise — the ``/games end`` path, which
       archives and pays but posts no recap. This is the after-a-restart case,
       where the row outlived its view.

    Duck-typing ``close_now`` rather than branching on ``game_type`` keeps this
    working for the next game that grows one. ``end_game``'s DELETE is the
    exactly-once claim, so racing the 24-hour expiry sweep costs at worst a
    missing recap, never a double payout.
    """
    ended = False

    closer = getattr(bot, "game_channel_closers", {}).get(game_key) if game_key else None
    if closer is not None:
        try:
            ended = bool(await closer(channel_id))
        except Exception:
            log.exception(
                "Rotation: registered closer for %s failed in %s", game_key, channel_id
            )

    db = getattr(bot, "games_db", None)
    if db is None:
        return ended
    from bot_modules.games.utils.game_manager import force_end_active_game

    rows = await db.fetchall(
        "SELECT game_id FROM games_active_games WHERE channel_id = ?", (channel_id,)
    )
    channel = guild.get_channel(channel_id)
    for row in rows:
        game_id = str(row["game_id"])
        view = getattr(bot, "active_views", {}).get(game_id)
        view_closer = getattr(view, "close_now", None)
        try:
            if view_closer is not None and channel is not None:
                await view_closer(channel)
            else:
                await force_end_active_game(bot, db, game_id)
        except Exception:
            log.exception("Rotation: failed to end game %s in %s", game_id, channel_id)
            continue
        ended = True
    return ended


async def start_room_game(
    bot,
    guild: discord.Guild,
    channel_id: int,
    game_key: str,
    options: dict,
) -> str | None:
    """Launch this room's game. Returns the new game id, or None.

    Hosted by nobody on purpose: ``host_id=0`` reaches ``pay_game_rewards`` as
    ``host_id=None``, so an auto-launched game pays no host bounty. A real
    member there would collect one every featured day for hosting nothing.

    Refuses when the channel already has a game running, the same courtesy the
    scheduler extends — a rotation flip must not stomp a session someone is
    mid-way through.
    """
    launcher = getattr(bot, "game_launchers", {}).get(game_key)
    if launcher is None:
        log.error("Rotation: no launcher registered for %s", game_key)
        return None
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("Rotation: cannot launch %s, channel %s missing", game_key, channel_id)
        return None

    db = getattr(bot, "games_db", None)
    if db is not None:
        from bot_modules.games.utils.game_manager import get_active_game

        if await get_active_game(db, channel_id) is not None:
            log.info(
                "Rotation: %s already has a game running; not launching %s",
                channel_id,
                game_key,
            )
            return None
    busy_check = getattr(bot, "game_busy_checks", {}).get(game_key)
    if busy_check is not None:
        try:
            if await busy_check(channel_id):
                return None
        except Exception:
            log.exception("Rotation: busy-check for %s raised", game_key)

    try:
        return await launcher(
            channel=channel,
            host_id=0,
            host_name="Today's feature",
            guild_id=guild.id,
            options=dict(options),
        )
    except Exception:
        log.exception("Rotation: launcher %s raised in channel %s", game_key, channel_id)
        return None


async def apply_game_ends(bot, guild: discord.Guild, plan: GamePlan) -> int:
    """End the games in every room that is not featured today."""
    ended = 0
    for channel_id, game_key in plan.end:
        if await end_room_game(bot, guild, channel_id, game_key):
            ended += 1
    return ended


async def apply_game_starts(
    bot, guild: discord.Guild, plan: GamePlan, rooms: list[Room]
) -> int:
    """Launch today's game in every featured room that declares one."""
    options_by_channel = {r.channel_id: parse_launch_options(r.launch_options) for r in rooms}
    started = 0
    for channel_id, game_key in plan.start:
        gid = await start_room_game(
            bot, guild, channel_id, game_key, options_by_channel.get(channel_id, {})
        )
        if gid:
            started += 1
    return started


async def post_announcement(bot, guild: discord.Guild, day: RotationDay) -> bool:
    """Post today's feature card. Returns whether a message was sent."""

    def _read() -> tuple[RotationConfig, list[Room]]:
        with bot.ctx.open_db() as conn:
            return get_config(conn, guild.id), list_pool(conn, guild.id)

    cfg, rooms = await asyncio.to_thread(_read)
    if not cfg.announce_channel_id:
        return False
    channel = guild.get_channel(cfg.announce_channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning(
            "Rotation: announce channel %s missing in guild %s",
            cfg.announce_channel_id,
            guild.id,
        )
        return False
    copy = build_announcement(rooms, list(day.featured))
    if copy is None:
        return False
    title, body = copy
    accent = await safe_resolve_accent(bot, guild, default=DEFAULT_ACCENT)
    embed = discord.Embed(title=title, description=body, color=accent)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Rotation: failed to announce in guild %s", guild.id)
        return False
    return True


# ── the loop ─────────────────────────────────────────────────────────────────


def _release(bot, guild_id: int, releaser, day: str) -> None:
    """Hand today's claim back after the work under it failed.

    Best-effort by design: this runs on the failure path, and a database
    hiccup here must not replace the original error with its own.
    """
    try:
        with bot.ctx.open_db() as conn:
            releaser(conn, guild_id, day)
    except sqlite3.Error:
        log.exception("Rotation: could not release the %s claim", day)


async def _tick_guild(bot, guild: discord.Guild, now: float) -> None:
    """One guild's flip and announcement checks for this pass."""

    def _read_cfg() -> RotationConfig:
        with bot.ctx.open_db() as conn:
            return get_config(conn, guild.id)

    cfg = await asyncio.to_thread(_read_cfg)
    if not cfg.enabled:
        return

    def _read_tz() -> float:
        with bot.ctx.open_db() as conn:
            return rotation_tz(conn, guild.id)

    tz = await asyncio.to_thread(_read_tz)
    today = local_day(now, tz)

    def _read_day() -> RotationDay | None:
        with bot.ctx.open_db() as conn:
            return rotation_day(conn, guild.id, now)

    day = await asyncio.to_thread(_read_day)
    if day is None:
        return

    if cfg.last_flip_date < today:

        def _claim_flip() -> bool:
            with bot.ctx.open_db() as conn:
                return claim_flip(conn, guild.id, today)

        if await asyncio.to_thread(_claim_flip):

            def _read_rooms() -> list[Room]:
                with bot.ctx.open_db() as conn:
                    return list_pool(conn, guild.id)

            try:
                # end → hide → show → start. The ends run while their rooms are
                # still visible so a recap lands somewhere members can see it,
                # and the starts run after the show so the game's first message
                # posts into a channel that is already open.
                ended = await apply_game_ends(bot, guild, day.games)
                shown, hidden = await apply_day(
                    bot, guild, day, reason=f"Feature rotation — {today}"
                )
                rooms = await asyncio.to_thread(_read_rooms)
                started = await apply_game_starts(bot, guild, day.games, rooms)
            except Exception:
                await asyncio.to_thread(_release, bot, guild.id, release_flip, today)
                raise
            log.info(
                "Rotation %s: %s featured, %d shown, %d hidden, %d games started, "
                "%d ended",
                guild.id,
                ", ".join(str(c) for c in day.featured) or "none",
                shown,
                hidden,
                started,
                ended,
            )

    if (
        cfg.last_announce_date < today
        and local_hour(now, tz) >= cfg.announce_hour
    ):

        def _claim_announce() -> bool:
            with bot.ctx.open_db() as conn:
                return claim_announce(conn, guild.id, today)

        if await asyncio.to_thread(_claim_announce):
            posted = False
            try:
                posted = await post_announcement(bot, guild, day)
            finally:
                if not posted:
                    await asyncio.to_thread(
                        _release, bot, guild.id, release_announce, today
                    )


async def feature_rotation_loop(bot) -> None:
    """Poll every 60s, flipping at local midnight and announcing on the hour.

    Registered as a bot startup task next to ``scheduled_games_loop``. Both
    actions are date-claimed, so the poll interval only decides how promptly
    they land, never whether they run twice.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = time.time()
            for guild in list(bot.guilds):
                try:
                    await _tick_guild(bot, guild, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Rotation: guild %s failed this pass", guild.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("feature_rotation_loop iteration error")
        await asyncio.sleep(POLL_SECONDS)


__all__ = [
    "apply_day",
    "apply_game_ends",
    "apply_game_starts",
    "apply_plan",
    "end_room_game",
    "feature_rotation_loop",
    "hide_room",
    "post_announcement",
    "show_room",
    "start_room_game",
]
