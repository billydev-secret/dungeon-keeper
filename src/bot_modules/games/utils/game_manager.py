import asyncio
import json
import time
import uuid
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from bot_modules.core.utils import disable_all_items
from bot_modules.games.utils.game_roster import NO_ROSTER_TYPES, roster_from_payload

log = logging.getLogger(__name__)


def channel_name(channel: Any) -> str:
    """Channel name for log lines; DMs and unresolved channels render as 'DM'."""
    return getattr(channel, "name", "DM") if channel else "unknown"


# ── Fake/test user name resolution ──────────────────────────────────
_FAKE_BASE = 900_000_001
_FAKE_NAMES = [
    "TestAlice", "TestBob", "TestCharlie", "TestDiana",
    "TestEve", "TestFrank", "TestGrace", "TestHank",
    "TestIvy", "TestJack", "TestKara", "TestLeo",
]


def resolve_name(guild, uid) -> str:
    """Return a display name for *uid* — handles fake test IDs."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return str(uid)
    if _FAKE_BASE <= uid < _FAKE_BASE + len(_FAKE_NAMES):
        return _FAKE_NAMES[uid - _FAKE_BASE]
    if guild:
        member = guild.get_member(uid)
        if member:
            return member.display_name
    return str(uid)


def resolve_names(guild, uids: list[int]) -> list[str]:
    return [resolve_name(guild, uid) for uid in uids]


# Per-game lock to serialise get→modify→write payload cycles.
_payload_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def payload_lock(game_id: str) -> asyncio.Lock:
    """Return an asyncio.Lock scoped to *game_id*."""
    return _payload_locks[game_id]


class ConfirmCloseView(discord.ui.View):
    """Ephemeral confirmation prompt before closing a game."""

    def __init__(self, callback):
        super().__init__(timeout=30)
        self._callback = callback

    @discord.ui.button(label="Yes, End Game", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(content="🛑 Closing game…", view=self)
        await self._callback(interaction)

    @discord.ui.button(label="Nevermind", style=discord.ButtonStyle.secondary)
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Close cancelled.", view=None)


DEFAULT_LAUNCH_PERMS_HINT = (
    "I don't have access to send messages in that channel. "
    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**."
)


async def sign_off_game_chore(bot, guild_id: int | None, user_id: int | None) -> None:
    """Tick off a "run a game" chore on the todo board. Never raises.

    **Moderators only.** The chore board is a mod worklist, and "run a game" is
    a thing a mod is supposed to do for the server — so two ordinary members
    accepting a duel is a multiplayer game being run, but it is not the mod
    having done their chore. Without this gate the chore went green on any
    active day with no moderator involved, which makes it a report on how busy
    the server was rather than a checklist.

    "Moderator" is ``AppContext.member_is_mod`` — the one definition the rest of
    the bot uses (Discord's manage_guild/administrator, then the guild's
    configured mod/admin roles). An uncached member is treated as not a mod: a
    chore left open is recoverable with the board's own Complete button, where
    a tick that should not have happened is not.

    **This is the seam that makes the chore mean what it says.** Every party
    game reaches its board through two doors: a member's ``/games play``, and
    the scheduler calling the same ``launch()`` on a timer. Only the
    interactive door passes through here, so "a scheduled game doesn't count as
    you running one" is a property of *where* this is called from rather than a
    flag someone has to remember to set — there is nothing for the scheduler to
    opt out of.

    It fires when the game **starts**, not when it ends, which is a deliberate
    reading of a chore whose text is "run a game": the mod's part is done the
    moment the room has a game in it. Waiting for the archive would also mean a
    game that was played but never formally ended — or one that flopped with
    nobody joining — left the board claiming the chore was skipped.

    All DB work goes through ``asyncio.to_thread``, including the guild-config
    load the mod check needs — that hits the database on a cold cache (the
    first hand-started game after a restart), and it sits in front of
    ``interaction.response.edit_message`` on the duel paths, where a connection
    waiting out its 30s busy timeout would stall the heartbeat and drop the
    gateway. The config is **warmed** in the thread and the mod check itself
    then runs on the loop against the warm cache, so the ``Member`` never
    crosses into the worker: reading its roles walks the guild's live role
    cache, which another gateway event can mutate underneath a thread.

    The tick opens with ``BEGIN IMMEDIATE``: it reads the wired definitions and
    then writes the completion, and on a plain deferred transaction that
    sequence can fail with ``SQLITE_BUSY_SNAPSHOT`` when another writer commits
    in between — which ``busy_timeout`` does not retry. The per-definition
    guard inside ``auto_complete_chores`` would swallow it and the chore would
    quietly stay open, which is the failure this whole feature exists to stop.

    That write lock is only taken when there is something to write. A guild
    with no game-triggered chore — which is every guild until someone picks the
    trigger, since the migration backfills nothing — answers on a plain read
    instead, so the launch path of a busy evening never queues behind another
    writer for a transaction whose only statement returns no rows. The
    existence check being stale is harmless: the immediate transaction re-reads
    under the lock, so it is a fast negative, never a decision.
    """
    try:
        if not guild_id or not user_id:
            return
        ctx = getattr(bot, "ctx", None)
        if ctx is None:
            return
        from bot_modules.services.todo_recurring_service import (  # noqa: PLC0415
            auto_complete_chores,
        )

        guild = bot.get_guild(int(guild_id))
        member = guild.get_member(int(user_id)) if guild is not None else None
        if member is None:
            return

        # Warm the per-guild config off-loop, then ask the one shared question
        # ("is this member staff?") on-loop against the now-cached answer.
        await asyncio.to_thread(ctx.guild_config, int(guild_id))
        if not ctx.member_is_mod(member):
            return

        from bot_modules.core.db_utils import open_db_immediate  # noqa: PLC0415

        def _work() -> list[int]:
            with ctx.open_db() as conn:
                wired = conn.execute(
                    "SELECT 1 FROM todo_recurring"
                    " WHERE guild_id = ? AND status = 'active'"
                    "   AND auto_complete = 'game' LIMIT 1",
                    (int(guild_id),),
                ).fetchone()
            if wired is None:
                return []
            with open_db_immediate(ctx.db_path) as conn:
                return auto_complete_chores(
                    conn, int(guild_id), "game",
                    completed_by=int(user_id), now_ts=time.time(),
                )

        if await asyncio.to_thread(_work):
            # Only when something was actually ticked: a game started in a
            # guild with no game chore must not cost a board edit.
            from bot_modules.cogs.todo_cog import repaint_board  # noqa: PLC0415

            await repaint_board(bot, int(guild_id))
    except Exception:
        log.exception("game chore auto sign-off failed")


async def finish_launch_response(
    interaction: discord.Interaction,
    game_id: str | None,
    *,
    perms_hint: str = DEFAULT_LAUNCH_PERMS_HINT,
) -> None:
    """Resolve a deferred /games play interaction after launch().

    The game posts its own lobby/prompt message, so the deferred placeholder
    is deleted rather than filled in — left dangling, Discord renders it as
    "The application did not respond" once the interaction token expires.
    On a failed launch (no send permissions) *perms_hint* is sent ephemerally.
    """
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
    if game_id is None:
        try:
            await interaction.followup.send(perms_hint, ephemeral=True)
        except discord.HTTPException:
            pass
        return
    await sign_off_game_chore(
        interaction.client, interaction.guild_id, getattr(interaction.user, "id", None)
    )


async def check_allowed_channel(
    db, channel_id: int | None, guild_id: int | None = None
) -> bool:
    """True if games may run in *channel_id*.

    ``channel_id`` is a globally-unique Discord snowflake, so matching on it
    alone is not a cross-guild leak. When *guild_id* is supplied the match is
    additionally scoped to that guild for defence-in-depth, treating a stored
    ``guild_id = 0`` as a wildcard so legacy rows (added before migration 122
    stamped a guild on new rows) keep working until a reconcile assigns them.
    """
    if channel_id is None:
        return False
    if guild_id is not None:
        row = await db.fetchone(
            "SELECT channel_id FROM games_allowed_channels"
            " WHERE channel_id = ? AND (guild_id = ? OR guild_id = 0)",
            (channel_id, guild_id),
        )
    else:
        row = await db.fetchone(
            "SELECT channel_id FROM games_allowed_channels WHERE channel_id = ?",
            (channel_id,),
        )
    return row is not None


async def check_game_enabled(db, game_type: str, guild_id: int) -> bool:
    row = await db.fetchone(
        "SELECT enabled FROM games_game_config WHERE guild_id = ? AND game_type = ?",
        (guild_id, game_type),
    )
    return row is None or bool(row[0])


async def relaunch_refusal(
    db, game_type: str, channel_id: int | None, guild_id: int, *,
    label: str | None = None,
) -> str | None:
    """Why a recap's Play Again / Run Again must not relaunch, or None if it may.

    The relaunch button is a second front door to ``cog.launch``. It used to be
    the only door with no guard on it, so a host could keep a game an admin
    had just switched off on the dashboard alive from the recap card
    indefinitely. It is now a thin name for ``launch_guard.launch_refusal`` —
    the one guard (allowed channel, enabled dial, busy channel with a jump
    link, empty bank) every door shares and the single owner of the refusal
    copy. Kept so the three recap cards need no rewiring; new callers import
    ``launch_guard`` directly. ``label`` overrides the display name.
    """
    # Local import: launch_guard imports this module's check helpers.
    from bot_modules.games.utils.launch_guard import launch_refusal  # noqa: PLC0415

    return await launch_refusal(db, game_type, channel_id, guild_id, label=label)


async def get_game_options(db, game_type: str, guild_id: int) -> dict:
    row = await db.fetchone(
        "SELECT options FROM games_game_config WHERE guild_id = ? AND game_type = ?",
        (guild_id, game_type),
    )
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


async def get_active_game(db, channel_id: int | None):
    if channel_id is None:
        return None
    return await db.fetchone(
        "SELECT * FROM games_active_games WHERE channel_id = ?", (channel_id,)
    )


async def get_active_game_by_id(db, game_id: str):
    return await db.fetchone(
        "SELECT * FROM games_active_games WHERE game_id = ?", (game_id,)
    )


async def guild_for_channel(db, channel_id: int | None) -> int:
    """The guild the games allowlist records for *channel_id*, or 0.

    The allowlist is the one games table keyed by channel that knows its guild
    (migration 122), so it is the fallback for a launcher that did not pass one.
    """
    if channel_id is None:
        return 0
    row = await db.fetchone(
        "SELECT guild_id FROM games_allowed_channels WHERE channel_id = ? AND guild_id != 0",
        (channel_id,),
    )
    return int(row["guild_id"]) if row else 0


async def create_game(
    db,
    channel_id: int,
    host_id: int,
    game_type: str,
    message_id: int | None = None,
    state: str = "open",
    payload: dict | None = None,
    guild_id: int | None = None,
) -> str:
    """Insert the live-game row. The guild is stamped **here**, at creation,
    so every end path — including the bare ``end_game`` calls that have no bot
    to look it up with — archives the right guild instead of 0 (platform-19).
    Every launcher knows its guild; a caller that omits it falls back to the
    channel allowlist's record of the channel.
    """
    game_id = str(uuid.uuid4())
    payload_json = json.dumps(payload or {})
    if not guild_id:
        guild_id = await guild_for_channel(db, channel_id)
    await db.execute(
        """
        INSERT INTO games_active_games
            (game_id, channel_id, message_id, game_type, host_id, state, payload, guild_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, channel_id, message_id, game_type, host_id, state, payload_json, int(guild_id)),
    )
    return game_id


async def update_game_message(db, game_id: str, message_id: int):
    await db.execute(
        "UPDATE games_active_games SET message_id = ? WHERE game_id = ?",
        (message_id, game_id),
    )


async def update_game_state(db, game_id: str, state: str):
    await db.execute(
        "UPDATE games_active_games SET state = ? WHERE game_id = ?",
        (state, game_id),
    )


async def update_game_payload(db, game_id: str, payload: dict):
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?",
        (json.dumps(payload), game_id),
    )


async def get_game_payload(db, game_id: str) -> dict:
    row = await db.fetchone(
        "SELECT payload FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    if row:
        return json.loads(row[0])
    return {}


async def modify_payload(db, game_id: str, fn):
    """Atomically read, modify, and write the game payload.

    *fn* receives the current payload dict and should mutate it in-place
    (or return a new dict).  The lock for *game_id* is held for the
    entire read-modify-write cycle.

    Every vote, pose, join and advance comes through here, so it is also where
    a live game keeps its game-night session open (``touch_session``): a
    member typing ``/recap`` forty minutes into an AMA used to be told there
    was no session, because the 30-minute window only ever moved at start and
    end (platform-22). The touch is outside the payload lock and never raises.
    """
    async with payload_lock(game_id):
        payload = await get_game_payload(db, game_id)
        result = fn(payload)
        if result is not None:
            payload = result
        await update_game_payload(db, game_id, payload)
    try:
        await touch_session(db, game_id, payload)
    except Exception:
        log.exception("session touch failed for %s", game_id)
    return payload


@dataclass(frozen=True)
class GameEnd:
    """What ``end_game`` archived and paid — for callers that report on it
    (the 24h sweep's in-channel line). ``coins_paid`` is 0 whenever the
    faucet did not fire: no bot, no explicit roster, economy off."""

    game_id: str
    game_type: str
    guild_id: int
    player_count: int
    round_count: int
    coins_paid: int = 0


async def _resolve_guild_id(db, row, bot) -> int:
    """The guild a history row belongs to.

    Stamped at creation since migration 204, so normally it is just copied.
    A row still at 0 (created before the column existed, or by a launcher
    that could not name its guild) is re-derived: from the bot's channel
    cache when a bot is at hand, else from the channel allowlist.
    """
    try:
        stored = int(row["guild_id"] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        stored = 0
    if stored:
        return stored
    if bot is not None:
        try:
            channel = bot.get_channel(row["channel_id"])
            guild = getattr(channel, "guild", None)
            if guild is not None:
                return int(guild.id)
        except Exception:
            pass
    return await guild_for_channel(db, row["channel_id"])


async def end_game(
    db,
    game_id: str,
    player_count: int = 0,
    round_count: int = 0,
    payload: dict | None = None,
    *,
    bot=None,
    player_ids: Sequence[int | str] | None = None,
    reason: str | None = None,
) -> GameEnd | None:
    """Write game to history and remove from games_active_games.

    When *bot* and *player_ids* are supplied (only from a game's genuine
    completion site), the economy faucet pays each participant. ``bot=None``
    keeps abort/cleanup call sites payout-free and fully backward-compatible.

    **Recording is not gated on the caller knowing the roster.** A bare call
    (lobby timeout, empty-bank unwind, crash cleanup) archives the game's
    *stored* payload when none is passed, and when it names no players and no
    count, the roster is rebuilt from that payload (``game_roster``) so
    ``player_count`` / ``round_count`` say what happened instead of 0/0
    (platform-20). That is recording only — payment still rides on an explicit
    ``player_ids``, so the anti-farm gate is untouched.

    ``reason`` — ``'lobby_timeout'``, ``'crash'``, ``'expired'`` — lands in the
    archived payload so the dashboard can tell an abandoned lobby from a game
    that was played (clapback-9). The caller's dict is never mutated.

    Ending a game also merges its roster into the channel's game-night session
    (a paying end fires the session_join quest as well), so ``/recap`` sees
    everyone who played rather than only the host the start-time call knew.
    Games with no joined roster never open or extend a session.

    Returns what was archived, or None when another call already ended it.
    """
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    if not row:
        return None

    # Claim the game by deleting its active row FIRST — the DELETE is the
    # exactly-once gate. Each GamesDb.execute is its own transaction, so two
    # concurrent end_game calls (a natural completion racing /games end)
    # serialize here and only the one that actually removes the row goes on to
    # archive and pay. Without this claim both pass the `if not row` guard and
    # both pay, double-crediting participation, the host bounty, and the
    # game_host community bump.
    claimed = await db.execute(
        "DELETE FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    if (claimed.rowcount or 0) == 0:
        return None  # another call already ended this game

    game_type = row["game_type"]
    if payload is None:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            log.warning("Unreadable stored payload on ending game %s", game_id)
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Recording only: a caller that named neither a roster nor a count gets
    # both read back out of the payload. Payment stays on explicit player_ids.
    roster: list[int] = [int(p) for p in player_ids] if player_ids else []
    if player_count == 0 and player_ids is None:
        roster, derived_rounds = roster_from_payload(game_type, payload)
        player_count = len(roster)
        if round_count == 0:
            round_count = derived_rounds
    elif player_count == 0 and roster:
        player_count = len(roster)  # a roster was named; count what was named

    guild_id = await _resolve_guild_id(db, row, bot)

    archived = dict(payload)
    if reason:
        archived["reason"] = reason
    try:
        await db.execute(
            """
            INSERT INTO games_game_history
                (game_id, game_type, channel_id, host_id, player_count, round_count, payload, started_at, guild_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["game_id"],
                game_type,
                row["channel_id"],
                row["host_id"],
                player_count,
                round_count,
                json.dumps(archived),
                row["created_at"],
                guild_id,
            ),
        )
    except Exception as e:
        log.error("Failed to archive game %s to history: %s", game_id, e)
    _payload_locks.pop(game_id, None)
    _session_touched.pop(game_id, None)
    log.info("Game %s ended and removed.", game_id)

    coins_paid = 0
    if bot is not None and player_ids:
        coins_paid = await _pay_party_rewards(bot, row, payload, player_ids)
        await _fire_session_join(bot, db, row, player_ids)
    elif roster and game_type not in NO_ROSTER_TYPES:
        # Not a paying end, but the room still played: give the recap the
        # roster and keep the session window open past this game.
        try:
            await update_session(
                db, row["channel_id"], game_id, roster, game_type=game_type,
            )
        except Exception:
            log.exception("session merge failed for %s", game_id)

    return GameEnd(
        game_id=game_id, game_type=game_type, guild_id=guild_id,
        player_count=player_count, round_count=round_count, coins_paid=coins_paid,
    )


async def _fire_session_join(bot, db, row, player_ids: Sequence[int | str]) -> None:
    """Merge the real roster into the channel's game-night session and fire
    the session_join quest for every player, keyed on the session id — the
    per-occurrence claim collision makes later games in the same session
    no-ops, so "attend a game night" pays once per night. Never raises.

    Start-time update_session calls only carry the host, so this end-of-game
    merge is also what gives the recap a complete roster.
    """
    try:
        ids = [int(p) for p in player_ids]
        session_id = await update_session(
            db, row["channel_id"], row["game_id"], ids, game_type=row["game_type"],
        )
        channel = bot.get_channel(row["channel_id"])
        guild = getattr(channel, "guild", None)
        if session_id is None or guild is None:
            return
        from bot_modules.economy.game_rewards import fire_member_trigger

        for pid in ids:
            await fire_member_trigger(
                bot, guild.id, pid, "session_join", occurrence=str(session_id)
            )
    except Exception:
        log.exception("session_join trigger failed for %s", row["game_id"])


async def _pay_party_rewards(bot, row, payload: dict | None, player_ids: Sequence[int | str]) -> int:
    """Fire the economy faucet for a completed party game; never raises.
    Returns the coins credited (participation, wins and host bounty)."""
    try:
        from bot_modules.economy.game_rewards import pay_game_rewards, resolve_winners

        channel = bot.get_channel(row["channel_id"])
        guild = getattr(channel, "guild", None)
        if guild is None:
            return 0
        game_type = row["game_type"]
        winners = resolve_winners(game_type, payload or {})
        return await pay_game_rewards(
            bot, guild.id, list(player_ids), winners, game_type,
            occurrence=str(row["game_id"]),
            host_id=int(row["host_id"]) if row["host_id"] else None,
        )
    except Exception:
        log.exception("party game payout failed for %s", row["game_id"])
        return 0


async def force_end_active_game(bot, db, game_id: str) -> None:
    """Tear down a running game from outside its own views (e.g. /games end).

    Games signal cancellation through whatever handle their current phase is
    blocked on, and every game stashes that handle on the view it registers in
    ``bot.active_views`` — a ``GameTimer`` (``_timer`` / ``_timer_obj``), an
    ``asyncio.Event`` (``_advanced_event`` / ``_pick_event`` / ``_done_event`` /
    ``_submitted_event``), or a nested sub-view it awaits. This pokes every
    known handle so the game loop wakes, sees the game is gone (``_closed`` set
    and the view popped from ``active_views``), and returns at its guard.

    Reactive games have no loop — popping the view and archiving the row is
    enough. ``end_game`` is idempotent, so callers may also await it themselves.

    **This path pays.** ``/games end`` is the normal way several games are
    closed, not only an abort, so ending one here credits the same room the
    game's own completion site would have — the roster is rebuilt from the
    stored payload by ``game_roster``. A game type with no joined roster (or
    none reconstructable) yields an empty roster and pays nobody, so an abort
    of a game that never got going still costs nothing.
    """
    from bot_modules.games.utils.game_roster import roster_from_payload

    # Read the payload before end_game deletes the row — that DELETE is the
    # exactly-once claim, so afterwards there is nothing left to reconstruct.
    players: list[int] = []
    rounds = 0
    payload: dict = {}
    row = await db.fetchone(
        "SELECT game_type, payload FROM games_active_games WHERE game_id = ?",
        (game_id,),
    )
    if row is not None:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            log.warning("Unreadable payload on force-ended game %s", game_id)
            payload = {}
        players, rounds = roster_from_payload(row["game_type"], payload)

    for key in (game_id, f"{game_id}_bottom"):
        view = bot.active_views.pop(key, None)
        if view is None:
            continue
        # Trips the `if view._closed and game_id not in active_views` guards.
        if hasattr(view, "_closed"):
            view._closed = True
        # Wake a GameTimer the phase is awaiting. skip() fires the callback
        # (which sets the loop's local event); cancel() would suppress it.
        for tattr in ("_timer", "_timer_obj"):
            timer = getattr(view, tattr, None)
            if timer is not None and hasattr(timer, "skip"):
                try:
                    timer.skip()
                except Exception:
                    log.exception("force_end: timer.skip failed")
        # Wake any phase event the loop stashed on the view.
        for eattr in ("_advanced_event", "_pick_event", "_done_event", "_submitted_event"):
            ev = getattr(view, eattr, None)
            if ev is not None and hasattr(ev, "set"):
                ev.set()
        # Wake a nested sub-view the loop is blocked on via View.wait().
        sub = getattr(view, "_active_submit_view", None)
        if sub is not None and hasattr(sub, "stop"):
            try:
                sub.stop()
            except Exception:
                log.exception("force_end: sub-view stop failed")
        try:
            view.stop()
        except Exception:
            log.exception("force_end: view.stop failed")
    await end_game(
        db, game_id,
        player_count=len(players), round_count=rounds, payload=payload,
        bot=bot, player_ids=players,
    )


async def is_game_expired(db, game_id: str, max_seconds: int = 86400) -> bool:
    """Return True if the game is older than max_seconds (default 24 h) or no longer exists."""
    row = await db.fetchone(
        "SELECT created_at FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    if not row:
        return True
    created_at = datetime.fromisoformat(str(row["created_at"]))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).total_seconds() > max_seconds


# ── Session tracking ──────────────────────────────────────────────────────────

SESSION_WINDOW = timedelta(minutes=30)

# game_id -> monotonic time of its last session touch. modify_payload runs on
# every vote, so the touch is rate-limited per game rather than written each
# time; end_game pops the entry.
_session_touched: dict[str, float] = {}
SESSION_TOUCH_INTERVAL = 30.0


async def touch_session(
    db, game_id: str, payload: dict | None, *, min_interval: float = SESSION_TOUCH_INTERVAL,
) -> str | None:
    """Keep a live game's game-night session open and its roster current.

    Called from ``modify_payload`` on every payload write. Reads the game's
    channel and type, rebuilds the roster from *payload* (so a joiner is in
    the recap from the moment they join, not only at the end) and merges it
    through ``update_session`` — which also moves ``last_game_at`` forward so
    the 30-minute ``/recap`` window covers a game that is still being played.
    At most one write per game per *min_interval* seconds. Returns the session
    id it touched, or None when it skipped (rate-limited, no live row, or a
    game with no joined roster).
    """
    now = time.monotonic()
    last = _session_touched.get(game_id)
    if last is not None and now - last < min_interval:
        return None
    row = await db.fetchone(
        "SELECT channel_id, game_type FROM games_active_games WHERE game_id = ?",
        (game_id,),
    )
    if row is None or row["game_type"] in NO_ROSTER_TYPES:
        return None
    _session_touched[game_id] = now
    players, _rounds = roster_from_payload(row["game_type"], payload or {})
    return await update_session(
        db, int(row["channel_id"]), game_id, players, game_type=row["game_type"],
    )


async def update_session(
    db, channel_id: int, game_id: str, player_ids: list[int], *,
    game_type: str | None = None,
) -> str | None:
    """
    Find an active session within 30 minutes in the channel.
    Append game_id and merge player IDs. Create new session if none found.
    Returns the session_id the game landed in — or None for a game with no
    joined roster (``NO_ROSTER_TYPES``: the daily photo post, ffa cards),
    which must never open a session on its own: a bot post is not a game
    night. ``game_type`` is looked up from the live row when not given; an
    already-archived id (end_game merges after its DELETE claim) has no row,
    so a caller ending a game passes it explicitly.
    """
    if game_type is None:
        try:
            row = await db.fetchone(
                "SELECT game_type FROM games_active_games WHERE game_id = ?", (game_id,),
            )
            game_type = row["game_type"] if row else None
        except Exception:
            game_type = None
    if game_type in NO_ROSTER_TYPES:
        return None
    # Deliberately naive UTC, both here and for ``now`` below.
    # ``games_session_tracker.last_game_at`` / ``started_at`` are naive ISO
    # strings — SQLite's ``CURRENT_TIMESTAMP`` default writes naive ones, the
    # cutoff below is a *string* comparison in SQL, and
    # ``games_session.logic.format_duration`` subtracts ``started_at`` from
    # ``last_game_at`` directly. An aware value here would serialise with a
    # "+00:00" suffix, break the string comparison, and make that subtraction
    # raise TypeError against pre-existing naive rows.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - SESSION_WINDOW
    row = await db.fetchone(
        """
        SELECT session_id, game_ids, player_ids FROM games_session_tracker
        WHERE channel_id = ? AND last_game_at >= ?
        ORDER BY last_game_at DESC LIMIT 1
        """,
        (channel_id, cutoff.isoformat()),
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    if row:
        existing_games = json.loads(row["game_ids"])
        existing_players = json.loads(row["player_ids"])
        if game_id not in existing_games:
            existing_games.append(game_id)
        merged_players = list(set(existing_players + player_ids))
        await db.execute(
            """
            UPDATE games_session_tracker
            SET last_game_at = ?, game_ids = ?, player_ids = ?
            WHERE session_id = ?
            """,
            (now, json.dumps(existing_games), json.dumps(merged_players), row["session_id"]),
        )
        return str(row["session_id"])
    else:
        session_id = str(uuid.uuid4())
        await db.execute(
            """
            INSERT INTO games_session_tracker (session_id, channel_id, last_game_at, game_ids, player_ids)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, channel_id, now, json.dumps([game_id]), json.dumps(player_ids)),
        )
        return session_id
