"""Shared duel database helpers — duel_nicks, duel_cooldowns, duel_config.

Also the home of every timing constant the six games' sweeps and cards have
to agree on (challenge window, lobby window, naming window, abandonment
window) and the table map that lets ``BaseGame`` run one generic query
across all six ``*_games`` tables.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bot_modules.core.db_utils import sql_identifier

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb

#: How long a challenged player has to press Accept or Decline.
#:
#: One number for four places that must agree — the ChallengeView's own
#: timeout, the countdown on the card, the copy a late presser gets, and the
#: `state = 'PENDING'` cutoff in each game's ``fetch_sweepable_games``. It was
#: 60 seconds, hard-coded in all four, which is barely long enough to notice
#: the ping (game night 2026-08-21: "LMAO that did not let me accept").
CHALLENGE_RESPONSE_SECONDS: int = 300

#: How long a lobby stays open after its last join, leave or open — the
#: countdown on the lobby card, the host's warning ping and the LOBBY cutoff
#: in the three group games' ``fetch_sweepable_games`` all read this. It was
#: 90 seconds, hard-coded in each sweep: a full ten-player Musical Chairs
#: lobby expired under its host while people were still reading the rules
#: (game night 2026-08-17) and everyone re-joined a second one.
LOBBY_IDLE_SECONDS: int = 300
#: How long before a lobby closes the host is pinged once. A join resets the
#: lobby clock and re-arms the warning.
LOBBY_WARNING_SECONDS: int = 60

#: How long the winner has to press Name the Loser before the result is
#: swept to NO_NICK_SET. Was 5 minutes — half of all Hot Potato winners let
#: it lapse. A new game between the same pair ends it early (``superseded``).
NAMING_WINDOW_SECONDS: int = 1800
#: When, inside that window, the winner gets their one in-channel reminder.
NAMING_REMINDER_SECONDS: int = 120

#: How long the Run It Back button on a result card keeps working.
REMATCH_WINDOW_SECONDS: int = 300

#: How long an ACTIVE game may sit with no move before the sweep abandons it.
#: Pressure Cooker is turn-based with a five-minute turn clock; the timer
#: games get ten minutes so a fuse or a climb is never cut short.
_ACTIVE_IDLE_SECONDS: dict[str, int] = {"pressure": 300}
_DEFAULT_ACTIVE_IDLE_SECONDS: int = 600


def active_idle_seconds(game_type: str) -> int:
    """The abandonment window for one game — the sweep's cutoff **and** the
    number the "Game Abandoned" card states, so the two can't disagree."""
    return _ACTIVE_IDLE_SECONDS.get(game_type, _DEFAULT_ACTIVE_IDLE_SECONDS)


#: GAME_KEY → the table its rows live in. Three of the six are not
#: ``<key>_games``, which is why the map exists.
GAME_TABLES: dict[str, str] = {
    "pressure": "pressure_games",
    "quickdraw": "quickdraw_games",
    "hot_potato": "hot_potato_games",
    "hot_potato_group": "hp_group_games",
    "chicken": "chicken_games",
    "musical_chairs": "mc_games",
}


#: The tables that have a LOBBY state (and so a ``lobby_warned_at`` column).
#: The three duel tables have neither, and one of them (``pressure_games``)
#: has no ``last_action_at`` either — a lobby query there is a SQL error.
LOBBY_TABLES: frozenset[str] = frozenset({"hp_group_games", "chicken_games", "mc_games"})


def games_table(game_type: str) -> str:
    """The validated table name for ``game_type``; raises for an unknown key
    so a generic query can never be pointed at an arbitrary table."""
    try:
        return sql_identifier(GAME_TABLES[game_type])
    except KeyError:
        raise ValueError(f"no games table for {game_type!r}") from None


#: Why a game concluded at NO_NICK_SET (``nick_reason``, migration 207).
NICK_REASON_WINNER_TIMEOUT = "winner_timeout"
NICK_REASON_LOSER_OUTRANKS = "loser_outranks"
NICK_REASON_LOSER_LEFT = "loser_left"
NICK_REASON_WINNER_LEFT = "winner_left"
NICK_REASON_ALREADY_SERVING = "already_serving"
NICK_REASON_SUPERSEDED = "superseded"
NICK_REASONS: frozenset[str] = frozenset({
    NICK_REASON_WINNER_TIMEOUT,
    NICK_REASON_LOSER_OUTRANKS,
    NICK_REASON_LOSER_LEFT,
    NICK_REASON_WINNER_LEFT,
    NICK_REASON_ALREADY_SERVING,
    NICK_REASON_SUPERSEDED,
})

_CONFIG_DEFAULTS: dict = {
    # 0 = play again straight away. Group games enforced this dial at 48 while
    # the three duel panels offered it and read it nowhere (duels-party-116);
    # both now enforce it, and 0 is what every duel actually behaved like.
    # Kept in step with _DUEL_SHARED_DEFAULTS in web_server/routes/config.py.
    "cooldown_hours": 0,
    "sentence_hours": 24,
    # NOTE: no `allow_early_revert`. Early nickname revert was never built
    # and nothing ever read the flag, so migration 194 dropped the column it
    # sat in. (`pressure_config`, the pre-032 shape of this table, still
    # carries its own copy — that whole table is an orphan.)
    "channel_allowlist": "[]",
    "nick_denylist": "[]",
    "max_nick_length": 32,
    "max_stakes_length": 200,
    # Challenges one person may open per hour; 0 = no limit. Kept in step with
    # the dashboard default in web_server/routes/config.py.
    "challenge_limit_per_hour": 30,
}


# ── Config ────────────────────────────────────────────────────────────────────

async def get_config(db: GamesDb, guild_id: int, game_type: str) -> dict:
    row = await db.fetchone(
        "SELECT * FROM duel_config WHERE guild_id = ? AND game_type = ?",
        (guild_id, game_type),
    )
    if row:
        return dict(row)
    return {"guild_id": guild_id, "game_type": game_type, **_CONFIG_DEFAULTS}


def default_sentence_hours() -> int:
    """The nickname-sentence length a guild with no config row plays under."""
    return int(_CONFIG_DEFAULTS["sentence_hours"])


async def upsert_config(db: GamesDb, guild_id: int, game_type: str, **fields) -> None:
    # The table's own DEFAULT for cooldown_hours is still the historical 48
    # (SQLite can't change a column default in place); the code default is 0,
    # so a fresh row is seeded explicitly rather than inheriting it.
    await db.execute(
        "INSERT OR IGNORE INTO duel_config (guild_id, game_type, cooldown_hours)"
        " VALUES (?, ?, ?)",
        (guild_id, game_type, _CONFIG_DEFAULTS["cooldown_hours"]),
    )
    for key, value in fields.items():
        await db.execute(
            f"UPDATE duel_config SET {key} = ? WHERE guild_id = ? AND game_type = ?",
            (value, guild_id, game_type),
        )


# ── Nicks ─────────────────────────────────────────────────────────────────────

async def apply_nick(
    db: GamesDb,
    game_id: int,
    game_type: str,
    guild_id: int,
    loser_id: int,
    winner_id: int,
    original_nick: str | None,
    imposed_nick: str,
    sentence_hours: int,
) -> int:
    now = time.time()
    expires_at = now + sentence_hours * 3600
    return await db.lastrowid(
        """
        INSERT INTO duel_nicks
            (game_id, game_type, guild_id, loser_id, winner_id, original_nick,
             imposed_nick, applied_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, game_type, guild_id, loser_id, winner_id, original_nick, imposed_nick, now, expires_at),
    )


async def fetch_expired_nicks(db: GamesDb, now: float, game_type: str) -> list[dict]:
    """Expired, not-yet-reverted nick sentences for one game.

    Filtered by game_type so each game's expire loop only reverts its own
    rows — otherwise every game cog's loop grabs the same expired row and
    DMs the loser once each, with whichever game name happens to be running.
    """
    rows = await db.fetchall(
        "SELECT * FROM duel_nicks "
        "WHERE reverted_at IS NULL AND expires_at <= ? AND game_type = ?",
        (now, game_type),
    )
    return [dict(r) for r in rows]


async def get_active_nick_for_user(db: GamesDb, guild_id: int, user_id: int) -> dict | None:
    """Return any active nick sentence for this user, regardless of game type."""
    row = await db.fetchone(
        """
        SELECT * FROM duel_nicks
        WHERE guild_id = ? AND loser_id = ? AND reverted_at IS NULL
        ORDER BY applied_at DESC LIMIT 1
        """,
        (guild_id, user_id),
    )
    return dict(row) if row else None


async def mark_nick_reverted(db: GamesDb, nick_id: int, reason: str) -> None:
    await db.execute(
        "UPDATE duel_nicks SET reverted_at = ?, revert_reason = ? WHERE id = ?",
        (time.time(), reason, nick_id),
    )


# ── Cooldowns ─────────────────────────────────────────────────────────────────

async def check_cooldown(
    db: GamesDb,
    guild_id: int,
    game_type: str,
    user_a: int,
    user_b: int,
    cooldown_hours: int,
) -> float | None:
    lo, hi = min(user_a, user_b), max(user_a, user_b)
    row = await db.fetchone(
        """
        SELECT last_game_at FROM duel_cooldowns
        WHERE guild_id = ? AND game_type = ? AND player_a = ? AND player_b = ?
        """,
        (guild_id, game_type, lo, hi),
    )
    if not row:
        return None
    elapsed = time.time() - row["last_game_at"]
    remaining = cooldown_hours * 3600 - elapsed
    return remaining if remaining > 0 else None


async def set_cooldown(
    db: GamesDb, guild_id: int, game_type: str, user_a: int, user_b: int
) -> None:
    lo, hi = min(user_a, user_b), max(user_a, user_b)
    await db.execute(
        """
        INSERT INTO duel_cooldowns (guild_id, game_type, player_a, player_b, last_game_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, game_type, player_a, player_b)
        DO UPDATE SET last_game_at = excluded.last_game_at
        """,
        (guild_id, game_type, lo, hi, time.time()),
    )


# ── Group cooldowns (per-player, for N-player BaseGame games) ──────────────────

async def check_group_cooldown(
    db: GamesDb,
    guild_id: int,
    game_type: str,
    player_id: int,
    cooldown_hours: int,
) -> float | None:
    """Return seconds remaining on this player's group cooldown, or None if clear."""
    if cooldown_hours <= 0:
        return None
    row = await db.fetchone(
        """
        SELECT last_game_at FROM duel_group_cooldowns
        WHERE guild_id = ? AND game_type = ? AND player_id = ?
        """,
        (guild_id, game_type, player_id),
    )
    if not row:
        return None
    elapsed = time.time() - row["last_game_at"]
    remaining = cooldown_hours * 3600 - elapsed
    return remaining if remaining > 0 else None


async def set_group_cooldown(
    db: GamesDb, guild_id: int, game_type: str, player_id: int
) -> None:
    await db.execute(
        """
        INSERT INTO duel_group_cooldowns (guild_id, game_type, player_id, last_game_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, game_type, player_id)
        DO UPDATE SET last_game_at = excluded.last_game_at
        """,
        (guild_id, game_type, player_id, time.time()),
    )


# ── One-shot pings and reasons (migration 207) ────────────────────────────────

async def fetch_lobby_warning_ids(db: GamesDb, game_type: str, now: float) -> list[int]:
    """Lobbies within ``LOBBY_WARNING_SECONDS`` of closing whose host hasn't
    been warned since the last join or leave. A join moves ``last_action_at``
    past ``lobby_warned_at``, which is what re-arms the warning. Empty for a
    duel, which has no lobby."""
    table = games_table(game_type)
    if table not in LOBBY_TABLES:
        return []
    rows = await db.fetchall(
        f"""
        SELECT id FROM {table}
        WHERE state = 'LOBBY'
          AND last_action_at <= ?
          AND (lobby_warned_at IS NULL OR lobby_warned_at < last_action_at)
        """,
        (now - (LOBBY_IDLE_SECONDS - LOBBY_WARNING_SECONDS),),
    )
    return [int(r["id"]) for r in rows]


async def mark_lobby_warned(db: GamesDb, game_type: str, game_id: int) -> None:
    table = games_table(game_type)
    await db.execute(
        f"UPDATE {table} SET lobby_warned_at = ? WHERE id = ?", (time.time(), game_id)
    )


async def fetch_naming_reminder_ids(db: GamesDb, game_type: str, now: float) -> list[int]:
    """Resolved games whose winner has been sitting on Name the Loser for
    ``NAMING_REMINDER_SECONDS`` and hasn't been reminded."""
    table = games_table(game_type)
    rows = await db.fetchall(
        f"""
        SELECT id FROM {table}
        WHERE state = 'RESOLVED'
          AND resolved_at IS NOT NULL
          AND resolved_at <= ?
          AND nick_reminded_at IS NULL
        """,
        (now - NAMING_REMINDER_SECONDS,),
    )
    return [int(r["id"]) for r in rows]


async def mark_naming_reminded(db: GamesDb, game_type: str, game_id: int) -> None:
    table = games_table(game_type)
    await db.execute(
        f"UPDATE {table} SET nick_reminded_at = ? WHERE id = ?", (time.time(), game_id)
    )


async def get_nick_reason(db: GamesDb, game_type: str, game_id: int) -> str | None:
    """Why ``game_id`` ended with no nickname, or None (renamed, or pre-207)."""
    table = games_table(game_type)
    row = await db.fetchone(f"SELECT nick_reason FROM {table} WHERE id = ?", (game_id,))
    return row["nick_reason"] if row else None
