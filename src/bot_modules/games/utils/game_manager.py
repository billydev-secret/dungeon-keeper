import asyncio
import json
import uuid
import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import discord

from bot_modules.core.utils import disable_all_items

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


async def check_allowed_channel(db, channel_id: int | None) -> bool:
    if channel_id is None:
        return False
    row = await db.fetchone(
        "SELECT channel_id FROM games_allowed_channels WHERE channel_id = ?", (channel_id,)
    )
    return row is not None


async def check_game_enabled(db, game_type: str, guild_id: int) -> bool:
    row = await db.fetchone(
        "SELECT enabled FROM games_game_config WHERE guild_id = ? AND game_type = ?",
        (guild_id, game_type),
    )
    return row is None or bool(row[0])


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


async def create_game(
    db,
    channel_id: int,
    host_id: int,
    game_type: str,
    message_id: int | None = None,
    state: str = "open",
    payload: dict | None = None,
) -> str:
    game_id = str(uuid.uuid4())
    payload_json = json.dumps(payload or {})
    await db.execute(
        """
        INSERT INTO games_active_games (game_id, channel_id, message_id, game_type, host_id, state, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, channel_id, message_id, game_type, host_id, state, payload_json),
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


async def update_game_host(db, game_id: str, new_host_id: int):
    await db.execute(
        "UPDATE games_active_games SET host_id = ? WHERE game_id = ?",
        (new_host_id, game_id),
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
    """
    async with payload_lock(game_id):
        payload = await get_game_payload(db, game_id)
        result = fn(payload)
        if result is not None:
            payload = result
        await update_game_payload(db, game_id, payload)
    return payload


async def end_game(
    db,
    game_id: str,
    player_count: int = 0,
    round_count: int = 0,
    payload: dict | None = None,
    *,
    bot=None,
    player_ids: Sequence[int | str] | None = None,
):
    """Write game to history and remove from games_active_games.

    When *bot* and *player_ids* are supplied (only from a game's genuine
    completion site), the economy faucet pays each participant. ``bot=None``
    keeps abort/cleanup call sites payout-free and fully backward-compatible.
    """
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    if not row:
        return

    try:
        await db.execute(
            """
            INSERT INTO games_game_history
                (game_id, game_type, channel_id, host_id, player_count, round_count, payload, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["game_id"],
                row["game_type"],
                row["channel_id"],
                row["host_id"],
                player_count,
                round_count,
                json.dumps(payload or {}),
                row["created_at"],
            ),
        )
    except Exception as e:
        log.error("Failed to archive game %s to history: %s", game_id, e)
    await db.execute("DELETE FROM games_active_games WHERE game_id = ?", (game_id,))
    _payload_locks.pop(game_id, None)
    log.info("Game %s ended and removed.", game_id)

    if bot is not None and player_ids:
        await _pay_party_rewards(bot, row, payload, player_ids)
        await _fire_session_join(bot, db, row, player_ids)


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
        session_id = await update_session(db, row["channel_id"], row["game_id"], ids)
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


async def _pay_party_rewards(bot, row, payload: dict | None, player_ids: Sequence[int | str]) -> None:
    """Fire the economy faucet for a completed party game; never raises."""
    try:
        from bot_modules.economy.game_rewards import pay_game_rewards, resolve_winners

        channel = bot.get_channel(row["channel_id"])
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        game_type = row["game_type"]
        winners = resolve_winners(game_type, payload or {})
        await pay_game_rewards(
            bot, guild.id, list(player_ids), winners, game_type,
            occurrence=str(row["game_id"]),
        )
    except Exception:
        log.exception("party game payout failed for %s", row["game_id"])


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
    """
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
    await end_game(db, game_id)


async def get_all_active_games(db) -> list:
    return await db.fetchall("SELECT * FROM games_active_games")


async def is_game_expired(db, game_id: str, max_seconds: int = 86400) -> bool:
    """Return True if the game is older than max_seconds (default 24 h) or no longer exists."""
    row = await db.fetchone(
        "SELECT created_at FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    if not row:
        return True
    from datetime import timezone
    created_at = datetime.fromisoformat(str(row["created_at"]))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).total_seconds() > max_seconds


# ── Session tracking ──────────────────────────────────────────────────────────

async def update_session(
    db, channel_id: int, game_id: str, player_ids: list[int]
) -> str:
    """
    Find an active session within 30 minutes in the channel.
    Append game_id and merge player IDs. Create new session if none found.
    Returns the session_id the game landed in.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    row = await db.fetchone(
        """
        SELECT session_id, game_ids, player_ids FROM games_session_tracker
        WHERE channel_id = ? AND last_game_at >= ?
        ORDER BY last_game_at DESC LIMIT 1
        """,
        (channel_id, cutoff.isoformat()),
    )

    now = datetime.utcnow().isoformat()

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
