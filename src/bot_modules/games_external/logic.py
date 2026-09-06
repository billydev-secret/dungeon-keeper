"""Storage helpers for external game-bot tracking.

The collector banks every watched-bot message RAW (content + embed dicts) keyed
on the source ``message_id`` so restarts, edits, and backfills all de-duplicate
instead of inflating counts. Metrics are derived from this table later, never at
capture time — see ``parser.py`` (added once the raw format is confirmed).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import discord

from bot_modules.games_external import parser


# Parser kinds a watch can carry — each selects a parser + economy mapping in
# parser.py. Ordered for display; the first is the default a bare migration row
# takes. Labels are what /games track shows.
#
# 'gamebot' covers every Gamebot sub-game (CAH, Connect 4, Anagrams, Survey
# Says, Wisecracks) — they share one Discord bot account, and which game a
# finished run was is read off its lobby embed at parse time
# (parser.identify_game), not from the watch config. Renamed from
# 'gamebot_cah' (2026-07-25) since no rows carried that value yet — a pure
# rename, no data migration needed.
WATCH_KIND_LABELS: dict[str, str] = {
    "gamebot": "Gamebot (Cards Against Humanity, Connect 4, Anagrams, Survey Says, Wisecracks)",
    "catbot": "Cat Bot",
    "wordle": "Wordle (daily group results)",
    "coordle": "Co-ordle (co-op word puzzle)",
}
VALID_WATCH_KINDS: tuple[str, ...] = tuple(WATCH_KIND_LABELS)

# The ledger ``kind`` values each watch kind writes (``claim_payout``), for
# the dashboard's per-watch "last payout" (photo-external-100). Gamebot's
# abandoned-lobby claim is deliberately *not* here: it pays nobody, so it is
# no evidence the parser is still crediting anyone.
PAYOUT_KINDS_BY_WATCH: dict[str, tuple[str, ...]] = {
    "gamebot": (
        "gamebot_cah", "gamebot_connect4", "gamebot_anagrams",
        "gamebot_survey_says", "gamebot_wisecracks",
    ),
    "catbot": ("catbot",),
    "wordle": ("wordle",),
    "coordle": ("coordle",),
}


async def list_watches(db, guild_id: int) -> list[Mapping[str, Any]]:
    """Every watch config for a guild (enabled or paused), newest first."""
    rows = await db.fetchall(
        "SELECT id, guild_id, channel_id, bot_user_id, kind, enabled "
        "FROM games_external_watch WHERE guild_id = ? ORDER BY set_at DESC",
        (guild_id,),
    )
    return list(rows)


async def get_watch_for_bot(
    db, guild_id: int, bot_user_id: int
) -> Mapping[str, Any] | None:
    """The most recently configured watch row for one (guild, bot), or None.

    A bot can now be watched in several channels at once, so this is only for
    the "pick a representative channel" cases (``/games track sample`` with no
    explicit channel). Use ``watch_channels_for_bot`` when every channel
    matters.
    """
    return await db.fetchone(
        "SELECT id, guild_id, channel_id, bot_user_id, kind, enabled "
        "FROM games_external_watch WHERE guild_id = ? AND bot_user_id = ? "
        "ORDER BY enabled DESC, set_at DESC",
        (guild_id, bot_user_id),
    )


async def watch_channels_for_bot(
    db, guild_id: int, bot_user_id: int
) -> list[Mapping[str, Any]]:
    """Every channel one bot is watched in for a guild, newest first."""
    rows = await db.fetchall(
        "SELECT id, guild_id, channel_id, bot_user_id, kind, enabled "
        "FROM games_external_watch WHERE guild_id = ? AND bot_user_id = ? "
        "ORDER BY set_at DESC",
        (guild_id, bot_user_id),
    )
    return list(rows)


async def set_watch(
    db, guild_id: int, channel_id: int, bot_user_id: int, kind: str, set_by: int
) -> None:
    """Watch one (channel, external bot) pair for a guild (enabled).

    Idempotent per (guild, bot, **channel**): re-running for a pair already
    watched refreshes its kind rather than adding a duplicate. Pointing the
    same bot at a *second* channel adds a row instead of moving the first one,
    so a bot playing in several channels is tracked in all of them at once
    (migration 135). Different bots coexist as before.
    """
    await db.execute(
        """
        INSERT INTO games_external_watch
            (guild_id, channel_id, bot_user_id, kind, enabled, set_by)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(guild_id, bot_user_id, channel_id) DO UPDATE SET
            kind        = excluded.kind,
            enabled     = 1,
            set_by      = excluded.set_by,
            set_at      = CURRENT_TIMESTAMP
        """,
        (guild_id, channel_id, bot_user_id, kind, set_by),
    )


async def set_watch_enabled(
    db, guild_id: int, bot_user_id: int, enabled: bool, channel_id: int | None = None
) -> bool:
    """Toggle collection on/off for a bot. False if no such watch exists.

    Without ``channel_id`` this applies to *every* channel the bot is watched
    in — pausing a chatty bot shouldn't require naming its channels one by one.
    Pass ``channel_id`` to toggle a single pair.
    """
    if channel_id is None:
        cur = await db.execute(
            "UPDATE games_external_watch SET enabled = ? "
            "WHERE guild_id = ? AND bot_user_id = ?",
            (1 if enabled else 0, guild_id, bot_user_id),
        )
    else:
        cur = await db.execute(
            "UPDATE games_external_watch SET enabled = ? "
            "WHERE guild_id = ? AND bot_user_id = ? AND channel_id = ?",
            (1 if enabled else 0, guild_id, bot_user_id, channel_id),
        )
    return cur.rowcount > 0


async def load_all_watches(db) -> list[Mapping[str, Any]]:
    """All enabled watch configs, for warming the in-memory cache on startup."""
    rows = await db.fetchall(
        "SELECT guild_id, channel_id, bot_user_id, kind FROM games_external_watch "
        "WHERE enabled = 1"
    )
    return list(rows)


def message_to_row(message: discord.Message) -> tuple:
    """Flatten a discord Message into the games_external_messages column order."""
    embeds_json = json.dumps([e.to_dict() for e in message.embeds])
    edited = message.edited_at.isoformat() if message.edited_at else None
    return (
        message.id,
        message.guild.id if message.guild else 0,
        message.channel.id,
        message.author.id,
        message.created_at.isoformat(),
        edited,
        message.content or "",
        embeds_json,
    )


async def store_message(db, message: discord.Message) -> None:
    """Idempotently bank a raw message. Re-capturing (e.g. on edit) refreshes
    content/embeds and clears any prior parse so the parser revisits it."""
    row = message_to_row(message)
    await db.execute(
        """
        INSERT INTO games_external_messages
            (message_id, guild_id, channel_id, author_id, created_at,
             edited_at, content, embeds_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            edited_at    = excluded.edited_at,
            content      = excluded.content,
            embeds_json  = excluded.embeds_json,
            parse_status = NULL,
            parsed_at    = NULL
        """,
        row,
    )


async def claim_payout(db, message_id: int, guild_id: int, kind: str) -> bool:
    """Reserve the one-time payout for a terminal message. True on first claim.

    Backs the "pay each external game exactly once" guarantee independently of
    ``parse_status`` (which edits reset). A second caller for the same message
    gets False and must not pay.

    **The contract is one payout per message globally, not per kind**: the
    table's primary key is ``message_id`` alone (migration 099), so a message
    claimed under one kind can never be claimed under another — ``kind`` is a
    label on the claim, not part of the key. Today's kinds are structurally
    disjoint (each claims a different bot's messages; ``mention_award`` alone
    claims human-authored ones), so this only matters to a future kind that
    overlaps an existing one's messages.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO games_external_payouts (message_id, guild_id, kind) "
        "VALUES (?, ?, ?)",
        (message_id, guild_id, kind),
    )
    return cur.rowcount > 0


def claim_payout_sync(conn, message_id: int, guild_id: int, kind: str) -> bool:
    """Sync twin of ``claim_payout`` for scripts riding a sqlite3 connection.

    Same contract (see above); the backfill scripts were inlining this SQL.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO games_external_payouts (message_id, guild_id, kind) "
        "VALUES (?, ?, ?)",
        (message_id, guild_id, kind),
    )
    return cur.rowcount > 0


async def release_payout(db, message_id: int, kind: str) -> bool:
    """Reopen a claim whose payout failed to credit. True if a row was freed.

    The claim-first ordering means a payout that no-ops (economy off, member
    unresolvable) has already burned the message's once-ever claim; without a
    release, that member is silently unpaid forever. Scoped on ``kind`` so a
    caller can only ever reopen its *own* claim — releasing another kind's
    would let that kind double-pay.
    """
    cur = await db.execute(
        "DELETE FROM games_external_payouts WHERE message_id = ? AND kind = ?",
        (message_id, kind),
    )
    return cur.rowcount > 0


def release_payout_sync(conn, message_id: int, kind: str) -> bool:
    """Sync twin of ``release_payout``."""
    cur = conn.execute(
        "DELETE FROM games_external_payouts WHERE message_id = ? AND kind = ?",
        (message_id, kind),
    )
    return cur.rowcount > 0


async def recent_channel_messages(
    db, guild_id: int, channel_id: int, author_id: int, before_iso: str, limit: int = 300
) -> list[Mapping[str, Any]]:
    """Banked messages for one (guild, channel, bot) at//before a timestamp,
    oldest-first — the window a parser walks to reconstruct a finished game."""
    rows = await db.fetchall(
        "SELECT message_id, created_at, content, embeds_json FROM games_external_messages "
        "WHERE guild_id = ? AND channel_id = ? AND author_id = ? AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (guild_id, channel_id, author_id, before_iso, limit),
    )
    return list(reversed(list(rows)))


# How far back one game's window is read, in banked messages. One page was
# the whole lookback until 2026-09-04: a game longer than it lost its lobby —
# and with it the host bounty and the sub-game identification (the largest
# real game is 214 Gamebot messages, so it was latent; photo-external-110).
# ``game_window_rows`` now pages further back until the window is bounded,
# up to the cap.
WINDOW_PAGE = 300
WINDOW_MAX_ROWS = 1500


def _parse_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "message_id": int(r["message_id"]),
            "created_at": r["created_at"],
            "content": r["content"],
            "embeds": json.loads(r["embeds_json"] or "[]"),
        }
        for r in rows
    ]


async def game_window_rows(
    db,
    guild_id: int,
    channel_id: int,
    author_id: int,
    message_id: int,
    before_iso: str,
    *,
    page: int = WINDOW_PAGE,
    max_rows: int = WINDOW_MAX_ROWS,
) -> list[Mapping[str, Any]]:
    """The banked messages making up the game ``message_id`` ends, oldest
    first, each with its ``embeds`` already decoded.

    Reads one page at or before ``before_iso``, then keeps paging further
    back while ``parser.find_window_start`` reports the window unbounded (no
    lobby and no previous terminal met yet) and the channel still has older
    rows, up to ``max_rows``. A window that hits the cap is returned as is:
    the parser's lobby-less fallback still pays what those rows hold.
    """
    rows = _parse_rows(
        await recent_channel_messages(db, guild_id, channel_id, author_id, before_iso, page)
    )
    while True:
        idx = next(
            (i for i, r in enumerate(rows) if r["message_id"] == message_id),
            len(rows) - 1,
        )
        _start, bounded = parser.find_window_start(rows, idx) if rows else (0, True)
        if bounded or len(rows) >= max_rows or len(rows) < page:
            break
        older = _parse_rows(
            await older_channel_messages(
                db, guild_id, channel_id, author_id, rows[0]["created_at"],
                min(page, max_rows - len(rows)),
            )
        )
        if not older:
            break
        rows = older + rows
    if not rows:
        return []
    return parser.current_game_window(rows, idx)


async def older_channel_messages(
    db, guild_id: int, channel_id: int, author_id: int, before_iso: str, limit: int
) -> list[Mapping[str, Any]]:
    """The ``limit`` banked messages strictly before a timestamp, oldest-first
    — the next page back for ``game_window_rows``."""
    rows = await db.fetchall(
        "SELECT message_id, created_at, content, embeds_json FROM games_external_messages "
        "WHERE guild_id = ? AND channel_id = ? AND author_id = ? AND created_at < ? "
        "ORDER BY created_at DESC LIMIT ?",
        (guild_id, channel_id, author_id, before_iso, limit),
    )
    return list(reversed(list(rows)))


async def mark_parsed(db, message_id: int, status: str) -> None:
    """Stamp a banked message's parse outcome ('ok' | 'skip' | 'error')."""
    await db.execute(
        "UPDATE games_external_messages SET parse_status = ?, "
        "parsed_at = CURRENT_TIMESTAMP WHERE message_id = ?",
        (status, message_id),
    )


async def count_messages(
    db, guild_id: int, bot_user_id: int | None = None, channel_id: int | None = None
) -> int:
    """How many raw messages we've banked for a guild, optionally narrowed to
    one bot and/or one channel.

    ``/games track status`` narrows by both, since a bot watched in several
    channels has one row per channel and an unscoped count would report the
    bot's whole total against each of them.
    """
    sql = "SELECT COUNT(*) AS n FROM games_external_messages WHERE guild_id = ?"
    params: list[Any] = [guild_id]
    if bot_user_id is not None:
        sql += " AND author_id = ?"
        params.append(bot_user_id)
    if channel_id is not None:
        sql += " AND channel_id = ?"
        params.append(channel_id)
    r = await db.fetchone(sql, tuple(params))
    return int(r["n"]) if r else 0


PARSE_BUFFER_RETENTION_DAYS = 30

# ── health signal (photo-external-100) ───────────────────────────────────────
#
# The panel used to show only how many raw messages a watch had banked, which
# keeps climbing whether or not a coin is paid — the 08-15 CAH outage and the
# 08-22 Anagrams one were both found by someone noticing in Discord. Two
# derived numbers per watch make a silent parser break visible: when this
# watch last credited anyone, and how many finishes in the buffer would be
# paid but carry no claim.

UNPAID_LOOKBACK_DAYS = 30


async def last_payout_at(db, guild_id: int, channel_id: int, watch_kind: str) -> str | None:
    """When a watch last paid anyone — the newest ``paid_at`` among its ledger
    kinds, or None if it never has.

    Narrowed to the channel where the claimed message is still in the buffer;
    a claim whose message has aged out (or was never a message id — Co-ordle
    keys on the round's timestamp) counts for every channel of that kind,
    which is the honest answer once the buffer can't say which channel it was.
    """
    kinds = PAYOUT_KINDS_BY_WATCH.get(watch_kind, (watch_kind,))
    marks = ", ".join("?" for _ in kinds)
    row = await db.fetchone(
        "SELECT MAX(p.paid_at) AS at FROM games_external_payouts p "
        "LEFT JOIN games_external_messages m ON m.message_id = p.message_id "
        f"WHERE p.guild_id = ? AND p.kind IN ({marks}) "
        "AND (m.message_id IS NULL OR m.channel_id = ?)",
        (guild_id, *kinds, channel_id),
    )
    return str(row["at"]) if row and row["at"] else None


async def unpaid_finishes(
    db,
    guild_id: int,
    channel_id: int,
    bot_user_id: int,
    watch_kind: str,
    *,
    days: int = UNPAID_LOOKBACK_DAYS,
    now_iso: str | None = None,
) -> list[int]:
    """Message ids of finishes in the last ``days`` that the parser would pay
    but that carry no claim — the sign a format change has broken a parser.

    Runs the same pure parser the live path runs over the buffered rows for
    one (channel, bot): for Gamebot, each terminal's window through
    ``parser.payable_game``; for the one-message kinds, whether the message
    parses at all. Only what the cog would actually credit counts — an
    abandoned lobby, a game we have no parser for, a Wordle "is playing"
    line, a Cat Bot spawn are not unpaid, they are unpayable.
    """
    now = (
        datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    rows = _parse_rows(
        await db.fetchall(
            "SELECT message_id, created_at, content, embeds_json FROM games_external_messages "
            "WHERE guild_id = ? AND channel_id = ? AND author_id = ? AND created_at >= ? "
            "ORDER BY created_at ASC",
            (guild_id, channel_id, bot_user_id, since),
        )
    )
    claimed = {
        int(r["message_id"])
        for r in await db.fetchall(
            "SELECT message_id FROM games_external_payouts WHERE guild_id = ?", (guild_id,)
        )
    }
    out: list[int] = []
    for i, row in enumerate(rows):
        key: int | None = None
        if watch_kind == "gamebot":
            if parser.is_terminal(row["embeds"]) and parser.payable_game(
                parser.current_game_window(rows, i)
            ):
                key = row["message_id"]
        elif watch_kind == "catbot":
            if parser.parse_cat_catch(row["content"] or "") is not None:
                key = row["message_id"]
        elif watch_kind == "wordle":
            if parser.parse_wordle_results(row["content"] or "") is not None:
                key = row["message_id"]
        elif watch_kind == "coordle":
            # Keyed on the round, and every guess re-posts the board: one
            # final board per unclaimed round is enough.
            _scores, _winner, state = parser.extract_coordle_game(row["embeds"])
            if state in (parser.COORDLE_SOLVED, parser.COORDLE_EXHAUSTED):
                round_key = parser.coordle_game_key(row["embeds"])
                if round_key is not None and round_key not in claimed:
                    claimed.add(round_key)
                    out.append(row["message_id"])
            continue
        if key is not None and key not in claimed:
            out.append(key)
    return out


async def sweep_old_buffer_rows(
    db, *, older_than_days: int = PARSE_BUFFER_RETENTION_DAYS
) -> int:
    """Delete parse-buffer rows collected more than *older_than_days* ago.

    games_external_messages is a capture buffer for external game bots'
    output: rows exist to be parsed into ledger payouts, and once the parser
    has moved past them (whatever their parse_status) they have no read-back
    use — the payouts are already booked. Left alone the buffer never
    empties and retains channel content indefinitely (2026-08 review,
    games batch-bc A1). ``collected_at`` is a TEXT timestamp, so the cutoff
    is computed in SQL datetime terms."""
    cur = await db.execute(
        "DELETE FROM games_external_messages "
        "WHERE collected_at < datetime('now', ?)",
        (f"-{int(older_than_days)} days",),
    )
    return getattr(cur, "rowcount", 0) or 0
