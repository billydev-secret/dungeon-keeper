"""Event Echo — the I/O half: cooldown store, the sender, and the poll loop.

Nine sources feed one sender, in three shapes.

**"This just started"** — echo it because someone might want to join:

  * **Party games** (and therefore scheduled games, which launch down the same
    path) — the ``event_echo_loop`` below.
  * **Cards Against Humanity** — ``echo_gamebot_lobby``, called from the
    ``on_message`` listener ``games_external_cog`` already runs over Gamebot.
  * **Discord's native scheduled events** — ``echo_discord_event``, called
    from ``events_cog`` when an event goes ``scheduled → active``.
  * **New bounties** — posted within the freshness window.

**"Last chance"** — echo it because a deadline is about to pass:

  * **Auctions** closing within the hour.
  * **Prediction-market rounds** whose betting window shuts within the hour.
  * **The weekly raffle**, before ticket sales shut at the ISO-week roll.

The last four are swept by :func:`econ_candidates`. Their queries live in the
services that own those tables, so Event Echo consumes rows it doesn't shape.

**"This just happened"** — echo it because the server crossed a boundary
worth marking. Neither an invitation nor a warning; nobody has to act:

  * **A new quest period going live** — ``echo_quest_flip``, called from the
    economy loop at the ISO-week roll.
  * **A community goal crossing a tier** — ``echo_community_tier``, called
    from the same loop's hourly beat pass.

The distinction is not cosmetic: deadline echoes bypass the cooldowns, because
skip-don't-queue is right for a game start and wrong for a deadline. The two
"just happened" sources bypass them for a different reason — their dedupe
lives *outside* Event Echo (the ISO week; ``econ_community_progress.
notified_tier``, which the beat pass advances in the same transaction that
detects the crossing), so a suppressed echo is never re-offered and the news
is gone for good rather than merely late. See ``SourceSpec.exempt``.

Every one of them ends at :func:`echo_event`, which owns the destination, the
cooldowns and the dedupe claim. A tenth source means a function returning
:class:`EchoCandidate` — or, for a push source, one thin wrapper like the two
below — and a ``SOURCE_SPECS`` row, not another dispatch path.

**Why a poll loop for party games.** Not because hooking would mean touching
28 call sites — those funnel through one ``update_game_message``, and
``end_game``'s ``bot=`` kwarg is precedent for threading a side effect into a
shared manager function. The real reason is that ``update_game_message`` isn't
the only way a lobby's message id gets recorded: ``games_ffa_cog`` and
``games_photo_cog`` pass ``message_id=`` straight to ``create_game`` and never
call it at all, so a hook there would silently miss them — exactly the class
of gap that let three schedulable games go unechoed until it was caught in
review. A sweep sees whatever ended up in the table, however it got there,
which is the property worth having. ``game_start_ping_service`` polls the same
table on the same cadence for the same reason.

The cost of polling is that a game which opens *and finishes* inside one tick
is never echoed. That is the correct trade: an echo pointing at a game already
over is worse than no echo. (Restart survival is *not* a benefit worth
claiming — ``FRESHNESS_SECONDS`` deliberately discards the post-restart
backlog, so nothing is recovered that the freshness bound doesn't then throw
away.)
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import (
    get_config_value,
    get_tz_offset_hours,
    open_db,
    open_db_immediate,
)
from bot_modules.core.utils import jump_url, resolve_bot_channel
from bot_modules.economy import logic as econ_logic
from bot_modules.economy import quests
from bot_modules.games.constants import GAME_NAMES
from bot_modules.games_external import parser
from bot_modules.services import economy_auction_service as auction_svc
from bot_modules.services import economy_bounty_service as bounty_svc
from bot_modules.services import pools_metrics
from bot_modules.services import pools_service as pools_svc
from bot_modules.services.economy_raffle_service import raffle_enabled
from bot_modules.services.economy_service import load_econ_settings
from bot_modules.services.event_echo_logic import (
    CLOSING_LEAD_SECONDS,
    FRESHNESS_SECONDS,
    GAMEBOT_ECHO_GAMES,
    RETENTION_SECONDS,
    SOURCE_AUCTION_CLOSING,
    SOURCE_BOUNTY,
    SOURCE_COMMUNITY_TIER,
    SOURCE_DISCORD_EVENT,
    SOURCE_GAMEBOT,
    SOURCE_PARTY_GAME,
    SOURCE_POOLS_CLOSING,
    SOURCE_QUEST_FLIP,
    SOURCE_RAFFLE_CLOSING,
    build_echo_embed,
    closing_due,
    decide,
    is_fresh,
    spec_for,
)

log = logging.getLogger(__name__)

# The destination. Its own key rather than reusing `denizen_announce_channel_id`
# (which is legacy role-grant plumbing that merely happens to point at the same
# channel) so either can move without dragging the other along.
CONFIG_CHANNEL_KEY = "event_echo_channel_id"

# Matches game_start_ping_service. An echo landing up to 15s after the lobby
# opens is immaterial for "come and join this".
POLL_SECONDS = 15


# ── Cooldown store (sync, over a plain connection — directly testable) ───────

def last_echo_times(
    conn: sqlite3.Connection, guild_id: int, echo_key: str
) -> tuple[float | None, float | None]:
    """``(last echo of this key, last echo of anything)`` for one guild.

    Suppressed rows are excluded from both: a refusal must not push the window
    out, or a single busy minute would cascade into a window that keeps
    receding and the feature would go quiet for good.
    """
    row = conn.execute(
        "SELECT MAX(CASE WHEN echo_key = ? THEN echoed_at END) AS same_type, "
        "       MAX(echoed_at) AS any_type "
        "FROM event_echo_log WHERE guild_id = ? AND suppressed = 0",
        (echo_key, guild_id),
    ).fetchone()
    if row is None:
        return None, None
    return row["same_type"], row["any_type"]


def claim_echo(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    source: str,
    echo_key: str,
    ref: str,
    now: float,
    suppressed: bool,
) -> bool:
    """Record this echo, returning False if this ``ref`` was already handled.

    The unique index on ``(guild_id, source, ref)`` is what makes this the
    dedupe point: the poll loop sees the same open lobby every 15 seconds and
    only the first tick gets True. Claiming *before* sending means a crash
    between the two loses an echo rather than repeating one — the failure
    direction that matters in a channel this busy.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO event_echo_log "
        "(guild_id, source, echo_key, ref, echoed_at, suppressed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, source, echo_key, ref, now, 1 if suppressed else 0),
    )
    return cur.rowcount > 0


def already_claimed(
    conn: sqlite3.Connection, *, guild_id: int, source: str, ref: str
) -> bool:
    """Has this ref been decided already (echoed or suppressed)?

    A cheap read that exists to keep the common case off the write lock: the
    sweep re-offers the same lobby on every one of the ~40 ticks it stays
    fresh, and only the first has anything to insert.
    """
    row = conn.execute(
        "SELECT 1 FROM event_echo_log WHERE guild_id = ? AND source = ? AND ref = ?",
        (guild_id, source, ref),
    ).fetchone()
    return row is not None


def release_echo(
    conn: sqlite3.Connection, *, guild_id: int, source: str, ref: str, retry: bool
) -> None:
    """Undo a claim after the send didn't land.

    The claim is taken before the send so a crash loses an echo rather than
    repeating one — but a send we *know* failed must not go on counting as a
    posted echo, or an unreachable destination silently burns both cooldowns
    and refuses the next real game on behalf of a message nobody ever saw.

    ``retry`` decides how far to undo, and the two answers are opposites for a
    reason:

    * **Start sources** (``retry=False``) keep the row, flagged. Their value
      expires in minutes, so a failed lobby echo is not worth re-attempting —
      and leaving the ref claimed is what stops the sweep hammering an
      unreachable channel every 15 seconds for the life of the lobby.
    * **Deadline sources** (``retry=True``) drop the row entirely, so the next
      tick tries again. Not retrying would defeat the point of exempting them
      from the cooldowns: one 429 on the first tick of the final hour would
      lose the last call outright, with hundreds of usable ticks still inside
      the window. The deadline bounds the retries on its own — once it passes,
      the sweep stops offering the candidate at all.
    * **"Just happened" sources** (``retry=False``) also keep the flagged row,
      but for the opposite reason to a lobby: nothing re-offers them at all,
      since they are pushed once from the event itself. Dropping the row would
      leave no record that the send was ever attempted, and there is no later
      tick for it to help.
    """
    if retry:
        conn.execute(
            "DELETE FROM event_echo_log WHERE guild_id = ? AND source = ? AND ref = ?",
            (guild_id, source, ref),
        )
        return
    conn.execute(
        "UPDATE event_echo_log SET suppressed = 1 "
        "WHERE guild_id = ? AND source = ? AND ref = ?",
        (guild_id, source, ref),
    )


def prune_echo_log(conn: sqlite3.Connection, now: float) -> int:
    """Drop rows past the retention window; returns how many went."""
    cur = conn.execute(
        "DELETE FROM event_echo_log WHERE echoed_at < ?", (now - RETENTION_SECONDS,)
    )
    return cur.rowcount


def echo_channel_id(conn: sqlite3.Connection, guild_id: int) -> int | None:
    """The configured destination, or None when the feature isn't set up.

    Unset means off — the echo has no sensible default channel to invent, and
    picking one for an admin who never asked is how a bot ends up posting
    somewhere nobody expected.
    """
    raw = get_config_value(conn, CONFIG_CHANNEL_KEY, "", guild_id)
    try:
        return int(raw) or None
    except (TypeError, ValueError):
        return None


# ── The sender ──────────────────────────────────────────────────────────────

async def echo_event(
    bot,
    *,
    guild: discord.Guild,
    source: str,
    echo_key: str,
    ref: str,
    name: str,
    origin_channel_id: int | None,
    origin_channel_name: str | None = None,
    url: str,
    host_name: str | None = None,
    deadline_epoch: float | None = None,
    detail: str | None = None,
    now: float | None = None,
) -> bool:
    """Post one echo, subject to config, cooldowns and dedupe.

    Returns True only when a message actually went out. Every other path —
    unconfigured, unreachable destination, cooldown, already-seen ref —
    returns False, and the caller is not expected to care which.
    """
    now = time.time() if now is None else now
    db_path: Path = bot.ctx.db_path

    def _claim() -> tuple[int | None, bool]:
        # Cheap read first. The sweep re-offers every fresh lobby on all ~40
        # ticks it stays fresh, and when the feature is unconfigured no row is
        # ever written, so nothing self-limits — without this pre-check each of
        # those visits took the database-wide write lock just to re-read one
        # config key and insert nothing. Now only a genuinely new echo pays.
        with open_db(db_path) as conn:
            dest = echo_channel_id(conn, guild.id)
            if dest is None:
                return None, False
            if already_claimed(conn, guild_id=guild.id, source=source, ref=ref):
                return dest, False

        # BEGIN IMMEDIATE for the decision itself: this is a read-then-write on
        # the cooldown window, and all three sources reach it concurrently (the
        # sweep's worker thread, the Gamebot listener, the scheduled-event
        # listener). Under a deferred transaction two overlapping claims can
        # both read "nothing echoed yet", both pass decide(), and both insert —
        # two echoes inside the 10-minute floor this feature exists to enforce.
        # It also avoids the SQLITE_BUSY_SNAPSHOT that a deferred read→write
        # upgrade raises when another writer commits in between (see
        # open_db_immediate's docstring). The pre-check above is only an
        # optimisation; claim_echo's unique index remains the dedupe guarantee.
        with open_db_immediate(db_path) as conn:
            last_same, last_any = last_echo_times(conn, guild.id, echo_key)
            verdict = decide(
                now=now,
                last_same_type=last_same,
                last_any=last_any,
                exempt=spec_for(source).exempt,
            )
            claimed = claim_echo(
                conn,
                guild_id=guild.id,
                source=source,
                echo_key=echo_key,
                ref=ref,
                now=now,
                suppressed=not verdict.allowed,
            )
            if not verdict.allowed and claimed:
                log.debug(
                    "event echo: suppressed %s/%s (%s cooldown)",
                    source,
                    echo_key,
                    verdict.reason,
                )
            return dest, (verdict.allowed and claimed)

    def _release() -> None:
        with open_db(db_path) as conn:
            release_echo(
                conn,
                guild_id=guild.id,
                source=source,
                ref=ref,
                retry=spec_for(source).retry,
            )

    dest_id, go = await asyncio.to_thread(_claim)
    if dest_id is None or not go:
        return False

    # From here the row is claimed as *posted*. Every failure path below has
    # to release it, or a send that never landed goes on blocking the next
    # real game for the length of both cooldowns.
    try:
        channel = await resolve_bot_channel(bot, dest_id)
        if channel is None:
            log.warning("event echo: destination channel %s unreachable", dest_id)
            await asyncio.to_thread(_release)
            return False

        color = await safe_resolve_accent(db_path, guild, log_label="event echo")
        embed = build_echo_embed(
            name=name,
            channel_id=origin_channel_id,
            channel_name=origin_channel_name,
            url=url,
            host_name=host_name,
            source=source,
            deadline_epoch=deadline_epoch,
            detail=detail,
            color=color,
        )
        # Silent by design — see event_echo_logic's module docstring.
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("event echo: send failed for %s/%s", source, ref, exc_info=True)
        await asyncio.to_thread(_release)
        return False
    return True


# ── Source 2: Gamebot (Cards Against Humanity) ──────────────────────────────

async def echo_gamebot_lobby(bot, message: discord.Message, sub_game: str) -> bool:
    """Echo a Gamebot lobby we care about.

    Called from the collector's ``on_message`` — the listener that already
    banks these messages for economy payouts — so CAH costs a branch on a hot
    path we were running anyway, not a second watcher to keep in sync with
    Gamebot's wording.
    """
    if sub_game not in GAMEBOT_ECHO_GAMES or message.guild is None:
        return False
    name = parser.GAME_LABELS.get(sub_game, sub_game)
    return await echo_event(
        bot,
        guild=message.guild,
        source=SOURCE_GAMEBOT,
        echo_key=sub_game,
        ref=str(message.id),
        name=name,
        origin_channel_id=message.channel.id,
        origin_channel_name=getattr(message.channel, "name", None),
        url=message.jump_url,
    )


# ── Source 3: Discord's native scheduled events ─────────────────────────────

async def echo_discord_event(bot, event: discord.ScheduledEvent) -> bool:
    """Echo a native Discord event at the moment it goes live.

    Keyed on the event id, so the several ``on_scheduled_event_update`` calls
    Discord emits over an event's life produce at most one echo.
    """
    if event.guild is None:
        return False
    return await echo_event(
        bot,
        guild=event.guild,
        source=SOURCE_DISCORD_EVENT,
        echo_key=SOURCE_DISCORD_EVENT,
        ref=str(event.id),
        name=event.name,
        # None for an `external` event — those carry a location string and no
        # channel. Falling back to the guild id here would render as
        # `<#guild_id>`: a mention Discord can't resolve, shown as a dead link.
        origin_channel_id=event.channel.id if event.channel is not None else None,
        url=event.url,
        host_name=event.creator.display_name if event.creator else None,
    )


# ── Sources 8–9: boundaries the server crossed ──────────────────────────────
#
# Both are *push* sources, fired once by the economy loop at the moment it
# commits the thing being announced, and both link at the leaderboard panel —
# the one surface that renders a week's quests and a goal's progress bar, so
# it is where a reader who wants more than the one line actually goes.
#
# Neither passes `origin_channel_id`. "A fresh set of weeklies just landed in
# #economy" is wrong: the quests aren't *in* a channel, and naming the panel's
# channel alongside a link to the panel is the same fact twice.
#
# The copy for each `detail` line is built by the economy, not here — those
# numbers (pool size, spotlight, tier, contributors) are its vocabulary, and
# the warm phrasing for goal progress was written on purpose (see
# `quests.TRIGGER_FLAVOR` and `quests.tier_echo_line`). Event Echo owns the
# frame; the feature owns its own voice.

async def echo_quest_flip(
    bot,
    guild: discord.Guild,
    *,
    week: str,
    detail: str,
    channel_id: int,
    message_id: int,
    now: float | None = None,
) -> bool:
    """Echo a new quest period going live, at the ISO-week roll.

    Keyed on the week, so a loop tick replayed after a restart cannot announce
    the same week twice.
    """
    return await echo_event(
        bot,
        guild=guild,
        source=SOURCE_QUEST_FLIP,
        echo_key=SOURCE_QUEST_FLIP,
        ref=week,
        name="This week's quests",
        origin_channel_id=None,
        url=jump_url(guild.id, channel_id, message_id),
        detail=detail,
        now=now,
    )


async def echo_community_tier(
    bot,
    guild: discord.Guild,
    *,
    quest_id: int,
    tier: int,
    title: str,
    detail: str,
    channel_id: int,
    message_id: int,
    now: float | None = None,
) -> bool:
    """Echo a community goal crossing a milestone tier.

    ``ref`` is the goal and the tier together rather than the goal alone: a
    period's three tiers are three separate pieces of news, and a goal that
    crosses two of them inside one hourly pass must produce two echoes. The
    real once-only guarantee is upstream in ``notified_tier`` — this ref is
    the belt to its braces, and covers a replayed tick.
    """
    return await echo_event(
        bot,
        guild=guild,
        source=SOURCE_COMMUNITY_TIER,
        echo_key=SOURCE_COMMUNITY_TIER,
        ref=f"{quest_id}:{tier}",
        name=title,
        origin_channel_id=None,
        url=jump_url(guild.id, channel_id, message_id),
        detail=detail,
        now=now,
    )


# ── Source 1: party games + scheduled games (the poll loop) ─────────────────

def _opened_at(row) -> float | None:
    """``created_at`` as an epoch, or None if it's missing or unparseable.

    SQLite writes ``CURRENT_TIMESTAMP`` as naive UTC text; a row predating
    some format change, or one written by a test with its own value, must not
    take the loop down.
    """
    raw = row["created_at"] if "created_at" in row.keys() else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class EchoCandidate:
    """One thing that might be echoed, in the shape :func:`echo_event` wants.

    Every non-game source produces these, so the sweep is one loop rather
    than a branch per source, and adding a seventh means writing a function
    that returns candidates — not another dispatch path.
    """

    source: str
    guild_id: int
    ref: str
    name: str
    channel_id: int
    message_id: int
    deadline: float | None = None


def _pools_name(row) -> str:
    """The closing market, named by what it counts.

    Falls back to the bare line for a metric this build no longer defines:
    the round is about to be refunded anyway, and a last call that says
    less is better than one that crashes the sweep.
    """
    line = f"{float(row['line']):g}"
    spec = pools_metrics.spec_for(str(row["metric"]))
    return (
        f"today's over/under on {spec.label.lower()} ({line})"
        if spec else f"today's over/under ({line})"
    )


def _candidate(row, *, source: str, name: str) -> EchoCandidate:
    """A candidate from a row the owning service already aliased for us.

    The three row-backed sources name their columns differently in their own
    tables (``card_channel_id``, ``ends_at``, ``closes_at``); each service's
    query aliases them to this shared shape, so no column names are threaded
    through here as strings.
    """
    return EchoCandidate(
        source=source,
        guild_id=int(row["guild_id"]),
        ref=str(row["id"]),
        name=name,
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]),
        deadline=(
            float(row["deadline"]) if "deadline" in row.keys() else None
        ),
    )


def raffle_last_call(
    conn: sqlite3.Connection, guild_id: int, now: float
) -> EchoCandidate | None:
    """The weekly raffle, if ticket sales shut within the hour.

    The odd one out among the sources. There is no raffle *row* — tickets are
    week-scoped and the draw happens at the ISO-week roll — so both halves of
    an echo have to be derived rather than read:

    * **When** comes from ``econ_logic.next_week_roll_epoch``, which is where
      the economy's own week boundary lives, so this can't drift from it.
    * **What to link to** is the economy shop panel, because that is where the
      buy-tickets button lives. That makes it the best jump target of any
      source: the reader lands on the button rather than on a description of
      something happening elsewhere.

    Deliberately *not* gated on there being entrants already — zero tickets
    sold is exactly when the nudge is worth most.

    The time check runs before the settings load on purpose: the window is
    open for one hour in 168, and ``load_econ_settings`` builds an 80-field
    dataclass from a range scan, which is a lot to throw away 167 times over.
    """
    offset = get_tz_offset_hours(conn, guild_id)
    deadline = econ_logic.next_week_roll_epoch(now, offset)
    if not closing_due(deadline, now):
        return None

    settings = load_econ_settings(conn, guild_id)
    # The master switch as well as the raffle's own flag: ``roll_day`` returns
    # early when the economy is off, so no draw happens — but `raffle_enabled`
    # doesn't know that, and a previously-posted shop panel outlives the
    # switch. Without this the echo advertises a draw nobody will run, every
    # week, bypassing the cooldowns because it is a deadline source.
    if not settings.enabled or not raffle_enabled(settings):
        return None
    if not settings.shop_channel_id or not settings.shop_message_id:
        # No shop panel means no button to send anyone to, and "the raffle
        # closes soon" with nowhere to act is just an alarm.
        return None

    return EchoCandidate(
        source=SOURCE_RAFFLE_CLOSING,
        guild_id=guild_id,
        # The week is the identity — one last call per raffle week, however
        # many ticks fall inside the final hour.
        ref=quests.iso_week_for(econ_logic.local_day_for(now, offset)),
        name="this week's raffle",
        channel_id=settings.shop_channel_id,
        message_id=settings.shop_message_id,
        deadline=deadline,
    )


async def live_games(db, now: float):
    """Games worth considering for an echo this tick.

    Deliberately **not filtered by state**. The six lobby games sit in
    ``joining``, most others in ``open``, and wyr / nhie / price are created
    straight into ``playing`` — an enumerated state list silently dropped
    those three (all schedulable), and would drop the next game to invent a
    state. Presence in this table already means "live".

    What it *does* filter, in SQL rather than in Python: rows whose lobby
    hasn't been posted yet (nothing to link to), and rows too old to be worth
    announcing. Named columns rather than ``SELECT *`` because ``payload``
    holds lobby rosters and story text — kilobytes per row that this sweep
    reads four times a minute and never looks at.

    The freshness bound is applied twice, deliberately: this SQL cut is the
    cheap one and only understands SQLite's own ``CURRENT_TIMESTAMP`` text,
    while ``is_fresh`` re-checks in Python and decides what an unparseable
    timestamp means.
    """
    cutoff = datetime.fromtimestamp(now - FRESHNESS_SECONDS, timezone.utc)
    return await db.fetchall(
        "SELECT game_id, channel_id, message_id, game_type, host_id, created_at "
        "FROM games_active_games "
        "WHERE message_id IS NOT NULL AND (created_at IS NULL OR created_at >= ?)",
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    )


def _host_name(guild: discord.Guild, host_id) -> str | None:
    """The host's display name, or None if they've left or can't be resolved.

    Cache-only (``get_member``, no fetch): a footer is decoration, not worth
    an API round trip per game on a 15s sweep.
    """
    try:
        member = guild.get_member(int(host_id))
    except (TypeError, ValueError):
        return None
    return member.display_name if member is not None else None


async def _process_game(bot, row, now: float) -> None:
    message_id = row["message_id"]
    if not message_id:
        # The lobby hasn't been posted yet (create_game runs before the send).
        # Skipping leaves it for the next tick, by which point there's a
        # message to link to — the whole point of the echo.
        return
    if not is_fresh(_opened_at(row), now):
        return

    channel_id = int(row["channel_id"])
    # The guild comes from the game's own channel, never from ctx.guild_id:
    # games_active_games has no guild_id column, so a lobby opened in any
    # other guild the bot is in would otherwise be announced here, with a jump
    # link whose guild segment points at the wrong server — a dead link.
    # game_manager.end_game resolves it the same way for the same reason.
    channel = bot.get_channel(channel_id)
    guild = getattr(channel, "guild", None)
    if guild is None:
        return

    game_type = str(row["game_type"])
    await echo_event(
        bot,
        guild=guild,
        source=SOURCE_PARTY_GAME,
        echo_key=game_type,
        ref=str(row["game_id"]),
        # A game type missing from GAME_NAMES still gets a readable name rather
        # than being dropped — a new game should show up in main chat on the
        # day it ships, not once someone remembers the display-name table.
        name=GAME_NAMES.get(game_type) or game_type.replace("_", " ").title(),
        origin_channel_id=channel_id,
        origin_channel_name=getattr(channel, "name", None),
        url=jump_url(guild.id, channel_id, int(message_id)),
        host_name=_host_name(guild, row["host_id"]),
        now=now,
    )


# ── Sources 4–7: the economy (auctions, pools, bounties, raffle) ────────────
#
# Unlike games, all four know their own guild — three read it off the row, and
# the raffle is asked per guild — so none of them has to reverse-resolve it
# from a channel.

def econ_candidates(conn: sqlite3.Connection, guild_ids, now: float) -> list[EchoCandidate]:
    """Everything the economy has to say this tick, in one shape.

    The queries live in the services that own those tables — Event Echo
    consumes rows it doesn't shape, so a column rename or a state-machine
    change stays the owning feature's problem rather than silently breaking a
    module its authors have no reason to grep.
    """
    found = [
        _candidate(row, source=SOURCE_AUCTION_CLOSING, name=str(row["title"]))
        for row in auction_svc.closing_auctions(conn, now, CLOSING_LEAD_SECONDS)
    ]
    found += [
        # A round has no title — the metric and its line are the thing
        # you'd bet on. Naming the metric is not decoration: the market
        # rotates daily, so "today's over/under (1186.5)" on its own says
        # nothing about what is being counted.
        _candidate(
            row,
            source=SOURCE_POOLS_CLOSING,
            name=_pools_name(row),
        )
        for row in pools_svc.closing_rounds(conn, now, CLOSING_LEAD_SECONDS)
    ]
    found += [
        _candidate(row, source=SOURCE_BOUNTY, name=str(row["title"]))
        for row in bounty_svc.recent_bounties(conn, now - FRESHNESS_SECONDS)
    ]
    # The raffle has no row to discover, so it is asked about per guild —
    # its enable flag, timezone and shop panel are all guild-scoped config.
    found += [
        cand
        for gid in guild_ids
        if (cand := raffle_last_call(conn, gid, now)) is not None
    ]
    return found


async def _sweep_econ(bot, now: float) -> None:
    """One read connection for every economy source."""
    db_path: Path = bot.ctx.db_path
    guild_ids = [g.id for g in bot.guilds]

    def _read():
        with open_db(db_path) as conn:
            return econ_candidates(conn, guild_ids, now)

    for cand in await asyncio.to_thread(_read):
        guild = bot.get_guild(cand.guild_id)
        if guild is None:
            continue
        await echo_event(
            bot,
            guild=guild,
            source=cand.source,
            # One bucket per source: these fire a handful of times a year, so
            # there is nothing finer to bucket by (and the deadline ones skip
            # the windows anyway).
            echo_key=cand.source,
            ref=cand.ref,
            name=cand.name,
            origin_channel_id=cand.channel_id,
            url=jump_url(guild.id, cand.channel_id, cand.message_id),
            deadline_epoch=cand.deadline,
            now=now,
        )


async def event_echo_loop(bot) -> None:
    """Sweep every polled source and echo whatever clears the cooldowns.

    Registered as a bot startup task. Games drop out of
    ``games_active_games`` when they end, and the economy sweeps are bounded
    by their own deadline/freshness windows, so nothing can be echoed late.
    """
    await bot.wait_until_ready()
    db = bot.games_db
    db_path: Path = bot.ctx.db_path
    last_prune = 0.0

    while not bot.is_closed():
        try:
            now = time.time()
            rows = await live_games(db, now)
            for row in rows:
                try:
                    await _process_game(bot, row, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "event echo: game %s failed to process", row["game_id"]
                    )

            try:
                await _sweep_econ(bot, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event echo: economy sweep failed")

            # Hourly, not every tick — the table is tiny and the prune is pure
            # housekeeping.
            if now - last_prune > 3600:
                last_prune = now
                await asyncio.to_thread(_prune, db_path, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event_echo_loop iteration error")
        await asyncio.sleep(POLL_SECONDS)


def _prune(db_path: Path, now: float) -> None:
    with open_db(db_path) as conn:
        prune_echo_log(conn, now)
