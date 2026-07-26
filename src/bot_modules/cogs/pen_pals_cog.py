"""Pen Pals — private 1-on-1 matched text channels with prompted questions."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games.utils.question_source import get_ai_config, _pick_least_recently_served

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.pen_pals")

_SESSION_SECS = 24 * 3600       # 24-hour session
_Q_INTERVAL = 24 * 3600         # auto-question every 24 h
_WARN_SECS = 3600                # post 1-h warning when this much time remains
_Q_SUPPRESS_SECS = 2 * 3600     # skip auto-question if fewer than 2 h remain
_MAX_SWAPS = 3
_TICK_SECS = 300                 # background loop tick every 5 min
_MATCH_COOLDOWN_SECS = 30 * 86400  # only re-match a member once they've had no pen pal for a month
_GAME_TYPE = "pen_pals"

# ── Room visibility ───────────────────────────────────────────────────────────
# How much staff oversight a pairing's otherwise-private room gets. Stored as a
# single TEXT enum on pen_pals_config.room_visibility (migration 126); the
# overwrites are built from the guild's admin_role_ids / mod_role_ids.
ROOM_VIS_ADMIN = "admin"        # configured admins (+ Discord admins) only
ROOM_VIS_MODS = "mods"          # admins + configured mod roles
ROOM_VIS_EVERYONE = "everyone"  # world-readable, watch-only (members/staff post)
ROOM_VISIBILITIES = (ROOM_VIS_ADMIN, ROOM_VIS_MODS, ROOM_VIS_EVERYONE)
DEFAULT_ROOM_VISIBILITY = ROOM_VIS_MODS

# ── Match mode ───────────────────────────────────────────────────────────────
# 'instant' (default): pair the moment someone joins if a partner is waiting,
# with the background sweep only mopping up stragglers. 'scheduled': never
# match on join — everyone queues, and the whole pool is drawn once a day at
# _SCHEDULED_MATCH_HOUR in _SCHEDULED_MATCH_TZ. Stored on
# pen_pals_config.match_mode (migration 128).
MATCH_MODE_INSTANT = "instant"
MATCH_MODE_SCHEDULED = "scheduled"
MATCH_MODES = (MATCH_MODE_INSTANT, MATCH_MODE_SCHEDULED)
DEFAULT_MATCH_MODE = MATCH_MODE_INSTANT
_SCHEDULED_MATCH_TZ = ZoneInfo("America/New_York")
_SCHEDULED_MATCH_HOUR = 8  # 8am Eastern, DST-aware via ZoneInfo

_QUEUED_MSG_INSTANT = (
    "✅ You're in the pool! The moment someone else joins, "
    "your private channel opens automatically."
)
_QUEUED_MSG_SCHEDULED = "✅ You're in the pool! Matches go out once a day at 8:00 AM Eastern."


def _normalize_room_visibility(value: object) -> str:
    """Coerce a stored/incoming value to a known state, defaulting to mods."""
    return value if value in ROOM_VISIBILITIES else DEFAULT_ROOM_VISIBILITY


def _normalize_match_mode(value: object) -> str:
    """Coerce a stored/incoming value to a known mode, defaulting to instant."""
    return value if value in MATCH_MODES else DEFAULT_MATCH_MODE


def _room_everyone_can_view(visibility: str) -> bool:
    """True when @everyone should see (read-only) the room."""
    return _normalize_room_visibility(visibility) == ROOM_VIS_EVERYONE


def _room_staff_role_ids(
    visibility: str,
    admin_role_ids: "frozenset[int] | set[int]",
    mod_role_ids: "frozenset[int] | set[int]",
) -> set[int]:
    """Role ids that get an explicit view overwrite for this visibility.

    Admin roles are always granted (so configured admins keep access even under
    the world-readable 'everyone' state, where @everyone is view-only and can't
    post). Mod roles are added only for the 'mods' state.
    """
    staff = set(admin_role_ids)
    if _normalize_room_visibility(visibility) == ROOM_VIS_MODS:
        staff |= set(mod_role_ids)
    return staff


def _room_footer_text(visibility: str) -> str:
    """The intro embed's footer line describing who can see the room."""
    return {
        ROOM_VIS_ADMIN: "Admins can see this channel.",
        ROOM_VIS_MODS: "Admins and mods can see this channel.",
        ROOM_VIS_EVERYONE: "This channel is visible to everyone (they can read, not post).",
    }[_normalize_room_visibility(visibility)]


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_config(conn, guild_id: int):
    return conn.execute(
        "SELECT * FROM pen_pals_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()


def _set_config(
    conn,
    guild_id: int,
    *,
    enabled: bool,
    category_id: int,
    opt_in_role_id: int,
    question_category: str,
    log_channel_id: int,
    panel_channel_id: int,
    room_visibility: str = DEFAULT_ROOM_VISIBILITY,
    intro_message: str = "",
    match_mode: str = DEFAULT_MATCH_MODE,
) -> None:
    conn.execute("INSERT OR IGNORE INTO pen_pals_config (guild_id) VALUES (?)", (guild_id,))
    conn.execute(
        """UPDATE pen_pals_config
           SET enabled=?, category_id=?, opt_in_role_id=?, question_category=?,
               log_channel_id=?, panel_channel_id=?, room_visibility=?, intro_message=?,
               match_mode=?
           WHERE guild_id=?""",
        (int(enabled), category_id, opt_in_role_id, question_category,
         log_channel_id, panel_channel_id,
         _normalize_room_visibility(room_visibility), intro_message,
         _normalize_match_mode(match_mode), guild_id),
    )


def _set_timers(
    conn,
    guild_id: int,
    *,
    session_seconds: int,
    match_cooldown_seconds: int,
    max_question_swaps: int,
    warn_seconds: int,
    question_suppress_seconds: int,
) -> None:
    conn.execute("INSERT OR IGNORE INTO pen_pals_config (guild_id) VALUES (?)", (guild_id,))
    conn.execute(
        """UPDATE pen_pals_config
           SET session_seconds=?, match_cooldown_seconds=?, max_question_swaps=?,
               warn_seconds=?, question_suppress_seconds=?
           WHERE guild_id=?""",
        (session_seconds, match_cooldown_seconds, max_question_swaps,
         warn_seconds, question_suppress_seconds, guild_id),
    )


def _set_panel_message_id(conn, guild_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE pen_pals_config SET panel_message_id=? WHERE guild_id=?",
        (message_id, guild_id),
    )


def _in_pool(conn, guild_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM pen_pals_pool WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone() is not None


def _add_to_pool(conn, guild_id: int, user_id: int, joined_at: float | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pen_pals_pool (guild_id, user_id, joined_at) VALUES (?, ?, ?)",
        (guild_id, user_id, joined_at if joined_at is not None else time.time()),
    )


def _remove_from_pool(conn, guild_id: int, user_id: int) -> None:
    conn.execute(
        "DELETE FROM pen_pals_pool WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def _get_pool(conn, guild_id: int) -> list:
    return conn.execute(
        "SELECT user_id, joined_at FROM pen_pals_pool WHERE guild_id = ? ORDER BY joined_at ASC",
        (guild_id,),
    ).fetchall()


# ── Block list / separations ──────────────────────────────────────────────────
#
# One table, two sources (see migration 096). Members manage their own
# directional blocks via /penpals block; admins manage symmetric separations on
# the dashboard. Matching treats every row as symmetric — a pairing is excluded
# when any row connects the two members in either direction.


def _is_blocked_pair(conn, guild_id: int, a: int, b: int) -> bool:
    """True if these two must never be matched (either side, either source)."""
    return conn.execute(
        """
        SELECT 1 FROM pen_pals_blocks
        WHERE guild_id = ?
          AND ((user_id = ? AND blocked_user_id = ?)
            OR (user_id = ? AND blocked_user_id = ?))
        LIMIT 1
        """,
        (guild_id, a, b, b, a),
    ).fetchone() is not None


def _add_block(conn, guild_id: int, user_id: int, blocked_user_id: int) -> None:
    """Add a member's self-service block (blocker → blockee), idempotent."""
    conn.execute(
        """INSERT OR IGNORE INTO pen_pals_blocks
               (guild_id, user_id, blocked_user_id, source, created_at)
           VALUES (?, ?, ?, 'member', ?)""",
        (guild_id, user_id, blocked_user_id, time.time()),
    )


def _remove_block(conn, guild_id: int, user_id: int, blocked_user_id: int) -> None:
    """Remove one of a member's own blocks (leaves admin separations alone)."""
    conn.execute(
        """DELETE FROM pen_pals_blocks
           WHERE guild_id = ? AND user_id = ? AND blocked_user_id = ? AND source = 'member'""",
        (guild_id, user_id, blocked_user_id),
    )


def _get_member_blocks(conn, guild_id: int, user_id: int) -> list[int]:
    """The members this member has blocked, most recent first."""
    rows = conn.execute(
        """SELECT blocked_user_id FROM pen_pals_blocks
           WHERE guild_id = ? AND user_id = ? AND source = 'member'
           ORDER BY created_at DESC""",
        (guild_id, user_id),
    ).fetchall()
    return [r["blocked_user_id"] for r in rows]


def _get_admin_separations(conn, guild_id: int) -> list[tuple[int, int]]:
    """All mod-enforced separations as (user_a, user_b) pairs (a < b)."""
    rows = conn.execute(
        """SELECT user_id, blocked_user_id FROM pen_pals_blocks
           WHERE guild_id = ? AND source = 'admin'
           ORDER BY created_at DESC""",
        (guild_id,),
    ).fetchall()
    return [(r["user_id"], r["blocked_user_id"]) for r in rows]


def _set_admin_separations(conn, guild_id: int, pairs: list[tuple[int, int]]) -> None:
    """Replace the guild's admin separations with *pairs* (member blocks untouched).

    Pairs are normalized to (min, max) so each separated couple is one row
    regardless of the order they were entered; self-pairs are dropped.
    """
    conn.execute(
        "DELETE FROM pen_pals_blocks WHERE guild_id = ? AND source = 'admin'",
        (guild_id,),
    )
    now = time.time()
    seen: set[tuple[int, int]] = set()
    for a, b in pairs:
        if a == b:
            continue
        lo, hi = (a, b) if a < b else (b, a)
        if (lo, hi) in seen:
            continue
        seen.add((lo, hi))
        conn.execute(
            """INSERT INTO pen_pals_blocks
                   (guild_id, user_id, blocked_user_id, source, created_at)
               VALUES (?, ?, ?, 'admin', ?)""",
            (guild_id, lo, hi, now),
        )


def _get_active_session(conn, guild_id: int, user_id: int):
    return conn.execute(
        """
        SELECT * FROM pen_pals_sessions
        WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?) AND state = 'active'
        """,
        (guild_id, user_id, user_id),
    ).fetchone()


def _get_session_by_channel(conn, channel_id: int):
    return conn.execute(
        "SELECT * FROM pen_pals_sessions WHERE channel_id = ? AND state = 'active'",
        (channel_id,),
    ).fetchone()


def _get_all_active_sessions(conn) -> list:
    return conn.execute(
        "SELECT * FROM pen_pals_sessions WHERE state = 'active'",
    ).fetchall()


def _create_session(
    conn, session_id: str, guild_id: int, channel_id: int,
    user1_id: int, user2_id: int, now: float,
    *, session_seconds: int = _SESSION_SECS,
) -> None:
    expiry = now + session_seconds
    conn.execute(
        """
        INSERT INTO pen_pals_sessions
            (session_id, guild_id, channel_id, user1_id, user2_id,
             started_at, expiry_at, next_question_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, guild_id, channel_id, user1_id, user2_id,
         now, expiry, now + _Q_INTERVAL),
    )


def _close_session(conn, session_id: str, reason: str) -> None:
    conn.execute(
        """
        UPDATE pen_pals_sessions
        SET state = 'closed', closed_at = ?, close_reason = ?
        WHERE session_id = ?
        """,
        (time.time(), reason, session_id),
    )


def _claim_close(conn, session_id: str, reason: str) -> bool:
    """Close a session only if it is still active, atomically.

    Returns True for the caller that actually closed it, False if it was
    already closed. This is the guard against double-handling: a ban deletes
    the channel, which fires ``on_guild_channel_delete`` for the same session —
    only one of the two events should notify and re-queue the survivors.
    """
    cur = conn.execute(
        """
        UPDATE pen_pals_sessions
        SET state = 'closed', closed_at = ?, close_reason = ?
        WHERE session_id = ? AND state = 'active'
        """,
        (time.time(), reason, session_id),
    )
    return cur.rowcount > 0


def _close_abnormal_and_requeue(
    conn, session_row, reason: str, departed_user_id: int | None
) -> list[int] | None:
    """Close a session that ended for a reason other than expiry or /end.

    Atomically claims the close (returns ``None`` if another handler already
    did — a lost race or a duplicate event), drops the departed member from
    the pool, and returns each surviving member to the pool so they get a
    fresh match. Never touches the expiry path, so ``pen_pal_complete`` does
    **not** fire — an abandoned session isn't "seen through".

    ``departed_user_id`` is the member who left/was banned (never re-queued),
    or ``None`` when both members remain (e.g. a mod deleted the channel).

    Returns the list of re-queued survivor ids.
    """
    if not _claim_close(conn, session_row["session_id"], reason):
        return None
    guild_id = session_row["guild_id"]
    if departed_user_id is not None:
        _remove_from_pool(conn, guild_id, departed_user_id)
    requeued: list[int] = []
    for uid in (session_row["user1_id"], session_row["user2_id"]):
        if uid == departed_user_id:
            continue
        if _get_active_session(conn, guild_id, uid) or _in_pool(conn, guild_id, uid):
            continue
        _add_to_pool(conn, guild_id, uid)
        requeued.append(uid)
    return requeued


def _set_close_warning_sent(conn, session_id: str) -> None:
    conn.execute(
        "UPDATE pen_pals_sessions SET close_warning_sent = 1 WHERE session_id = ?",
        (session_id,),
    )


def _advance_next_question(conn, session_id: str, next_at: float) -> None:
    conn.execute(
        "UPDATE pen_pals_sessions SET next_question_at = ? WHERE session_id = ?",
        (next_at, session_id),
    )


def _increment_swaps(conn, session_id: str) -> int:
    conn.execute(
        """
        UPDATE pen_pals_sessions
        SET question_swaps_used = question_swaps_used + 1
        WHERE session_id = ?
        """,
        (session_id,),
    )
    row = conn.execute(
        "SELECT question_swaps_used FROM pen_pals_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0] if row else 0


def _record_question(conn, session_id: str, question_text: str) -> None:
    conn.execute(
        "INSERT INTO pen_pals_questions (session_id, question_text, shown_at) VALUES (?, ?, ?)",
        (session_id, question_text, time.time()),
    )


def _get_shown_questions(conn, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT question_text FROM pen_pals_questions WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _past_partners(conn, guild_id: int, user_id: int) -> set[int]:
    """Everyone *user_id* has ever been a pen pal with in this guild.

    All-time, deliberately: a pairing is one-per-lifetime, so this is the
    exclusion list `_pick_partner` enforces rather than a recency window.
    """
    rows = conn.execute(
        """
        SELECT CASE WHEN user1_id = ? THEN user2_id ELSE user1_id END AS partner
        FROM pen_pals_sessions
        WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?)
        """,
        (user_id, guild_id, user_id, user_id),
    ).fetchall()
    return {r[0] for r in rows}


def _last_pen_pal_ended_at(conn, guild_id: int, user_id: int) -> float | None:
    """When *user_id*'s most recent pen pal chat ended, or None if never.

    The cooldown is a rest period *between* chats, so it is anchored to
    ``closed_at`` rather than ``started_at``. Anchoring to the start made the
    cooldown tick while the chat was still running: real rest was
    ``cooldown - session_length``, and any cooldown shorter than the session
    was no rest at all. It also made the knob mean something no admin would
    guess from "wait before matching again".

    An unfinished session has no ``closed_at``, so it falls back to its start:
    NULL must not read as "never had a pen pal". Callers exclude members with
    an active session before they get here, so that fallback is a guard, not a
    path — it matters only for a row left active by a crash mid-teardown.
    """
    row = conn.execute(
        """
        SELECT MAX(COALESCE(closed_at, started_at)) FROM pen_pals_sessions
        WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?)
        """,
        (guild_id, user_id, user_id),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _eligible_pool(conn, guild_id: int, now: float, cooldown_seconds: int) -> list[int]:
    """Pool members who can be matched right now, oldest signup first.

    Two rules, both hard: nobody already in an active session (one pen pal at a
    time — a stale pool row must never hand someone a second channel), and
    nobody inside the re-match cooldown. Ineligible members stay in the pool
    untouched and become eligible on their own.
    """
    return [
        r["user_id"]
        for r in _get_pool(conn, guild_id)
        if not _get_active_session(conn, guild_id, r["user_id"])
        and (
            (last := _last_pen_pal_ended_at(conn, guild_id, r["user_id"])) is None
            or now - last >= cooldown_seconds
        )
    ]


def _force_pair_status(conn, guild_id: int, user1_id: int, user2_id: int) -> str:
    """Why `/penpals pair` would refuse these two, or ``"ok"``.

    A mod force-pair skips the cooldown and the queue order, but never
    consent: a pen pal channel is a private two-person conversation, so both
    members must have opted in themselves (be in the pool, exactly the
    population `/penpals round` draws from). An active session is reported
    ahead of the opt-in gate because pairing removes people from the pool —
    "already has a pen pal" is the accurate reason there.
    """
    cfg = _get_config(conn, guild_id)
    if cfg is None or not cfg["enabled"]:
        return "disabled"
    if _is_blocked_pair(conn, guild_id, user1_id, user2_id):
        return "blocked"
    if _get_active_session(conn, guild_id, user1_id):
        return "active_1"
    if _get_active_session(conn, guild_id, user2_id):
        return "active_2"
    if not _in_pool(conn, guild_id, user1_id):
        return "not_opted_in_1"
    if not _in_pool(conn, guild_id, user2_id):
        return "not_opted_in_2"
    return "ok"


def _pick_partner(candidates: list[int], past: set[int]) -> int | None:
    """Oldest waiter in *candidates* (FIFO) who isn't already a past partner.

    Never repeating a pairing is a hard gate, not a preference: two members who
    have been pen pals once are never matched again, and when everyone waiting
    is a past partner the answer is None — both stay pooled for a later round
    rather than being handed the same person back. The cooldown alone can't do
    this, because it runs from the *shared* session's end and so expires for
    both halves of a pair at the same moment, floating them back into an
    otherwise-empty pool together.
    """
    return next((u for u in candidates if u not in past), None)


def _find_instant_match(conn, guild_id: int, user_id: int) -> int | None:
    """Partner for *user_id* to be paired with on the spot, or None to wait."""
    cfg = _get_config(conn, guild_id)
    cooldown = cfg["match_cooldown_seconds"] if cfg else _MATCH_COOLDOWN_SECS
    now = time.time()
    if _get_active_session(conn, guild_id, user_id):
        return None
    last = _last_pen_pal_ended_at(conn, guild_id, user_id)
    if last is not None and now - last < cooldown:
        return None
    candidates = [
        u
        for u in _eligible_pool(conn, guild_id, now, cooldown)
        if u != user_id and not _is_blocked_pair(conn, guild_id, user_id, u)
    ]
    return _pick_partner(candidates, _past_partners(conn, guild_id, user_id))


def _cfg_allows_nsfw(cfg) -> bool:
    """True when the guild's configured question pool includes NSFW prompts."""
    return cfg is not None and (cfg["question_category"] or "sfw") == "all"


def _parse_tags(tags_json) -> set[str]:
    """Parse a bank row's JSON tags column into a set, tolerating bad data."""
    try:
        return set(json.loads(tags_json or "[]"))
    except (json.JSONDecodeError, TypeError):
        return set()


def _draw_from_bank(conn, allow_nsfw: bool, exclude: list[str]) -> str | None:
    """Round-robin unshown bank question; rows tagged 'nsfw' need *allow_nsfw*.

    Prefers the least-recently-served matching row (see
    ``question_source._pick_least_recently_served``) and marks it served, so
    the small pen_pals pool doesn't repeat a question across separate
    sessions until every row has been served once.
    """
    rows = conn.execute(
        "SELECT question_id, question_text, tags, last_served_at FROM games_question_bank WHERE game_type = ?",
        (_GAME_TYPE,),
    ).fetchall()
    seen = set(exclude)
    candidates = [
        (r["question_id"], r["question_text"], r["last_served_at"])
        for r in rows
        if r["question_text"] not in seen
        and (allow_nsfw or "nsfw" not in _parse_tags(r["tags"]))
    ]
    picked = _pick_least_recently_served(candidates)
    if picked is None:
        return None
    qid, text = picked
    conn.execute(
        "UPDATE games_question_bank SET last_served_at = CURRENT_TIMESTAMP WHERE question_id = ?",
        (qid,),
    )
    return text


def _update_last_sweep(conn, guild_id: int) -> None:
    """Stamp when the pool was last drained.

    Every ``_do_round`` call stamps this, instant-mode sweeps and scheduled
    rounds alike; scheduled mode also reads it back via
    ``_scheduled_round_due`` to know whether today's round has already run.
    """
    conn.execute(
        "UPDATE pen_pals_config SET last_auto_round_at = ? WHERE guild_id = ?",
        (time.time(), guild_id),
    )


def _scheduled_round_due(cfg, now: float) -> bool:
    """True once per day: it's past _SCHEDULED_MATCH_HOUR local time and
    today's round hasn't run yet.

    Compares local dates (not a 24h interval) so a bot restart after 8am
    catches up immediately instead of waiting a full day, and so the round
    never fires twice in the same local day even if the tick interval drifts.
    """
    local_now = datetime.fromtimestamp(now, tz=_SCHEDULED_MATCH_TZ)
    if local_now.hour < _SCHEDULED_MATCH_HOUR:
        return False
    last_at = cfg["last_auto_round_at"] or 0
    if not last_at:
        return True
    last_local_date = datetime.fromtimestamp(last_at, tz=_SCHEDULED_MATCH_TZ).date()
    return last_local_date != local_now.date()


# ── Question draw ─────────────────────────────────────────────────────────────


_FALLBACK_QUESTION = "What's something about you that most people in this server don't know?"


async def _draw_question(db_path: Path, session_id: str, allow_nsfw: bool) -> str:
    def _from_bank():
        with open_db(db_path) as conn:
            shown = _get_shown_questions(conn, session_id)
            return _draw_from_bank(conn, allow_nsfw, shown)

    question = await asyncio.to_thread(_from_bank)
    if question:
        return question

    # Bank exhausted (or empty): AI fallback via the shared per-game prompt
    # config. Always SFW — NSFW prompts come only from the curated bank.
    ai_cfg = get_ai_config(_GAME_TYPE, "sfw")
    if ai_cfg:
        system, user, max_tokens = ai_cfg
        ai_text = await generate_text(system, user, max_tokens=max_tokens)
        if ai_text:
            line = ai_text.strip().splitlines()[0].strip()
            if line:
                return line

    return _FALLBACK_QUESTION


# ── Channel helpers ───────────────────────────────────────────────────────────


def _channel_name(name1: str, name2: str) -> str:
    def _slug(s: str) -> str:
        out = "".join(c if c.isalnum() else "-" for c in s.lower())
        return out[:20].strip("-")

    return f"penpals-{_slug(name1)}-{_slug(name2)}"[:100]


def _room_overwrites(
    guild: discord.Guild,
    user1: discord.Member,
    user2: discord.Member,
    *,
    visibility: str,
    staff_role_ids: "set[int] | frozenset[int]",
) -> dict:
    """Channel overwrites for a pairing's room under the given visibility.

    The two members always get full read/write and the bot gets management.
    Under 'everyone', @everyone reads (watch-only, no posting); otherwise the
    room is hidden from @everyone and each resolvable ``staff_role_ids`` role
    gets read/write so staff can oversee it. Unresolvable role ids are skipped.
    """
    visibility = _normalize_room_visibility(visibility)
    member_ow = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True
    )
    overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
        guild.default_role: (
            discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True
            )
            if _room_everyone_can_view(visibility)
            else discord.PermissionOverwrite(view_channel=False)
        ),
        user1: member_ow,
        user2: member_ow,
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_messages=True, manage_channels=True,
        ),
    }
    for rid in staff_role_ids:
        role = guild.get_role(rid)
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, manage_messages=True,
            )
    return overwrites


async def _create_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    user1: discord.Member,
    user2: discord.Member,
    *,
    nsfw: bool = False,
    visibility: str = DEFAULT_ROOM_VISIBILITY,
    staff_role_ids: "set[int] | frozenset[int]" = frozenset(),
) -> discord.TextChannel:
    overwrites = _room_overwrites(
        guild, user1, user2, visibility=visibility, staff_role_ids=staff_role_ids
    )
    # NSFW-flagged when the guild's question pool includes NSFW prompts, so the
    # channel age-gate matches the content that can appear in it.
    return await guild.create_text_channel(
        _channel_name(user1.display_name, user2.display_name),
        category=category,
        overwrites=overwrites,
        nsfw=nsfw,
        reason="Pen Pals session",
    )


async def _post_intro(
    channel: discord.TextChannel,
    user1: discord.Member,
    user2: discord.Member,
    expiry_at: float,
    question: str,
    color: "discord.Color | None" = None,
    visibility: str = DEFAULT_ROOM_VISIBILITY,
    intro_message: str = "",
) -> None:
    if color is None:
        color = discord.Color.blurple()
    embed = discord.Embed(title="🖊️ Pen Pals", color=color)
    if intro_message:
        embed.description = intro_message[:4096]
    embed.add_field(
        name="Matched With",
        value=f"{user1.mention} × {user2.mention}",
        inline=False,
    )
    embed.add_field(
        name="Session Ends",
        value=f"<t:{int(expiry_at)}:F> (<t:{int(expiry_at)}:R>)",
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/penpals new-question` — swap the prompt (3 max)\n"
            "`/penpals end` — leave this chat early"
        ),
        inline=False,
    )
    embed.set_footer(text=_room_footer_text(visibility))
    intro_msg = await channel.send(embed=embed)
    await intro_msg.pin()

    await channel.send(
        f"{user1.mention} {user2.mention}\n"
        f"💬 Here's your first question:\n> {question}"
    )


# ── Pair logic ────────────────────────────────────────────────────────────────


async def _do_pair(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    user1_id: int,
    user2_id: int,
) -> bool:
    """Create a session and channel for two users. Returns True on success."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False

    user1 = guild.get_member(user1_id)
    user2 = guild.get_member(user2_id)
    if user1 is None or user2 is None:
        log.warning("pen_pals: member(s) missing in guild %d (%d, %d)", guild_id, user1_id, user2_id)
        return False

    def _load_cfg():
        with open_db(db_path) as conn:
            return _get_config(conn, guild_id), _is_blocked_pair(conn, guild_id, user1_id, user2_id)

    cfg, blocked = await asyncio.to_thread(_load_cfg)
    if cfg is None or not cfg["enabled"] or not cfg["category_id"]:
        return False
    # Final safety net: no path — instant match, sweep, admin force-pair, or a
    # race where a block landed mid-pairing — ever opens a channel for a pair
    # that must stay apart.
    if blocked:
        log.info("pen_pals: refused blocked pairing %d ↔ %d in guild %d", user1_id, user2_id, guild_id)
        return False
    session_seconds = cfg["session_seconds"]

    category = guild.get_channel(cfg["category_id"])
    if not isinstance(category, discord.CategoryChannel):
        return False

    allow_nsfw = _cfg_allows_nsfw(cfg)
    session_id = str(uuid.uuid4())
    now = time.time()

    # Room visibility: resolve the staff roles that get to see this pairing's
    # otherwise-private room from the guild's configured admin/mod roles.
    visibility = _normalize_room_visibility(
        cfg["room_visibility"] if "room_visibility" in cfg.keys() else None
    )
    gcfg = await asyncio.to_thread(cast("Bot", bot).ctx.guild_config, guild_id)
    staff_role_ids = _room_staff_role_ids(
        visibility, gcfg.admin_role_ids, gcfg.mod_role_ids
    )

    # Draw question before the channel exists so we can post it immediately
    # (the session has no shown-question history yet).
    question = await _draw_question(db_path, session_id, allow_nsfw)

    try:
        channel = await _create_channel(
            guild, category, user1, user2, nsfw=allow_nsfw,
            visibility=visibility, staff_role_ids=staff_role_ids,
        )
    except discord.Forbidden:
        log.warning("pen_pals: missing permission to create channel in guild %d", guild_id)
        return False
    except discord.HTTPException as exc:
        log.error("pen_pals: channel creation failed in guild %d: %s", guild_id, exc)
        return False

    def _save() -> bool:
        with open_db(db_path) as conn:
            # Guard against a concurrent pairing that won the race while the
            # channel was being created: never give a user two sessions.
            if (
                _get_active_session(conn, guild_id, user1_id)
                or _get_active_session(conn, guild_id, user2_id)
            ):
                return False
            _create_session(
                conn, session_id, guild_id, channel.id, user1_id, user2_id, now,
                session_seconds=session_seconds,
            )
            _record_question(conn, session_id, question)
            _remove_from_pool(conn, guild_id, user1_id)
            _remove_from_pool(conn, guild_id, user2_id)
            # Pen-pal quest trigger for both matched members, keyed to the
            # session so one pairing pays each side once.
            from bot_modules.services.economy_quests_service import fire_trigger_inline

            for m in (user1, user2):
                fire_trigger_inline(
                    conn,
                    guild_id,
                    "pen_pal",
                    m.id,
                    occurrence=session_id,
                    booster=m.premium_since is not None,
                )
            return True

    if not await asyncio.to_thread(_save):
        log.warning(
            "pen_pals: aborted duplicate pairing %d ↔ %d in guild %d",
            user1_id, user2_id, guild_id,
        )
        try:
            await channel.delete(reason="Pen Pals: duplicate pairing aborted")
        except discord.HTTPException:
            pass
        return False

    expiry_at = now + session_seconds
    accent = await resolve_accent_color(db_path, guild)
    try:
        await _post_intro(
            channel, user1, user2, expiry_at, question,
            color=accent, visibility=visibility,
            intro_message=cfg["intro_message"] if "intro_message" in cfg.keys() else "",
        )
    except discord.HTTPException as exc:
        log.error("pen_pals: failed to post intro in channel %d: %s", channel.id, exc)

    # Post to log channel if configured
    if cfg["log_channel_id"]:
        log_ch = guild.get_channel(cfg["log_channel_id"])
        if isinstance(log_ch, discord.TextChannel):
            try:
                await log_ch.send(
                    f"🖊️ Pen Pals: {user1.mention} × {user2.mention} paired → {channel.mention}"
                )
            except discord.HTTPException:
                pass

    log.info("pen_pals: paired %d ↔ %d in guild %d (session %s)", user1_id, user2_id, guild_id, session_id)
    return True


async def _do_round(bot: discord.Client, db_path: Path, guild_id: int) -> tuple[int, int]:
    """Drain the pool for a guild. Returns (pairs_made, still_waiting).

    Pairing normally happens the moment someone joins (see ``_handle_join``);
    this is the sweeper for whoever is left over — the odd one out, members who
    were on cooldown when they joined, and anyone a failed pairing put back.
    Ineligible members are left untouched in the pool. ``still_waiting`` is the
    pool size once the round settles, so it counts cooled-down members, the odd
    one out, and any failed pairs alike.
    """
    def _load_eligible_and_stamp():
        with open_db(db_path) as conn:
            cfg = _get_config(conn, guild_id)
            match_cooldown_seconds = cfg["match_cooldown_seconds"] if cfg else _MATCH_COOLDOWN_SECS
            eligible = _eligible_pool(conn, guild_id, time.time(), match_cooldown_seconds)
            _update_last_sweep(conn, guild_id)
            return eligible

    remaining = await asyncio.to_thread(_load_eligible_and_stamp)
    pairs_made = 0

    while len(remaining) >= 2:
        u1 = remaining.pop(0)

        def _past_and_allowed(uid: int = u1, pool: list[int] = remaining):
            with open_db(db_path) as conn:
                past = _past_partners(conn, guild_id, uid)
                allowed = [u for u in pool if not _is_blocked_pair(conn, guild_id, uid, u)]
                return past, allowed

        past, allowed = await asyncio.to_thread(_past_and_allowed)
        partner = _pick_partner(allowed, past)
        if partner is None:
            # Everyone still waiting is either blocked with u1 or already been
            # their pen pal — leave u1 pooled for a future round rather than
            # forcing a bad pairing or a repeat.
            continue
        remaining.remove(partner)

        if await _do_pair(bot, db_path, guild_id, u1, partner):
            pairs_made += 1

    def _pool_count():
        with open_db(db_path) as conn:
            return len(_get_pool(conn, guild_id))

    return pairs_made, await asyncio.to_thread(_pool_count)


# ── Abnormal session teardown ─────────────────────────────────────────────────
#
# A session normally ends by running its 24h course (expiry) or via /penpals
# end. Everything else — a member banned/kicked/leaving mid-session, or a mod
# deleting the channel out from under the pair — routes here so the survivor(s)
# are told what happened and dropped back into the pool instead of silently
# falling out of Pen Pals.


async def _end_session_abnormally(
    bot: discord.Client,
    db_path: Path,
    session_row,
    *,
    reason: str,
    departed_user_id: int | None,
    delete_channel: bool,
) -> None:
    guild_id = session_row["guild_id"]
    channel_id = session_row["channel_id"]

    def _claim():
        with open_db(db_path) as conn:
            return _close_abnormal_and_requeue(conn, session_row, reason, departed_user_id)

    requeued = await asyncio.to_thread(_claim)
    if requeued is None:
        return  # another handler already closed this session — nothing to do

    # Tear the channel down (skip when it's already gone, e.g. it was deleted).
    if delete_channel:
        raw = bot.get_channel(channel_id)
        if raw is None:
            try:
                raw = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                raw = None
        if isinstance(raw, discord.TextChannel):
            try:
                await raw.delete(reason=f"Pen Pals: {reason}")
            except discord.HTTPException as exc:
                log.warning("pen_pals: failed to delete channel %d (%s): %s", channel_id, reason, exc)

    # Notify each surviving member and refresh the signup panel's pool count.
    guild = bot.get_guild(guild_id)
    if guild is not None:
        for uid in requeued:
            member = guild.get_member(uid)
            if member is None:
                continue
            try:
                await member.send(
                    f"Your pen pal session in **{guild.name}** ended early — your partner "
                    "is no longer available. You've been put back in the Pen Pals pool for "
                    "a new match."
                )
            except discord.HTTPException:
                pass

    try:
        await _refresh_panel(bot, guild_id)
    except discord.HTTPException as exc:
        log.warning("pen_pals: panel refresh after abnormal close failed in %d: %s", guild_id, exc)


# ── Background loop ───────────────────────────────────────────────────────────


async def _pen_pals_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _tick(bot, db_path)
        except Exception:
            log.exception("pen_pals_loop tick failed")
        await asyncio.sleep(_TICK_SECS)


async def _tick(bot: discord.Client, db_path: Path) -> None:
    def _load_all():
        with open_db(db_path) as conn:
            sessions = list(_get_all_active_sessions(conn))
            guild_ids = {s["guild_id"] for s in sessions}
            configs = {
                gid: _get_config(conn, gid)
                for gid in guild_ids
            }
            auto_cfgs = conn.execute(
                "SELECT * FROM pen_pals_config WHERE enabled = 1"
            ).fetchall()
            return sessions, configs, list(auto_cfgs)

    sessions, configs, auto_cfgs = await asyncio.to_thread(_load_all)
    now = time.time()

    for row in sessions:
        session_id = row["session_id"]
        guild_id = row["guild_id"]
        channel_id = row["channel_id"]
        expiry_at = row["expiry_at"]
        next_q_at = row["next_question_at"]
        warned = row["close_warning_sent"]
        user1_id = row["user1_id"]
        user2_id = row["user2_id"]
        cfg = configs.get(guild_id)
        warn_seconds = cfg["warn_seconds"] if cfg else _WARN_SECS
        q_suppress_seconds = cfg["question_suppress_seconds"] if cfg else _Q_SUPPRESS_SECS

        raw = bot.get_channel(channel_id)
        if raw is None:
            try:
                raw = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                # Channel vanished while the session was live (e.g. deleted
                # while the bot was offline, so on_guild_channel_delete never
                # fired). Same teardown as a manual delete: re-queue both.
                await _end_session_abnormally(
                    bot, db_path, row,
                    reason="channel_missing", departed_user_id=None, delete_channel=False,
                )
                continue
            except discord.HTTPException:
                continue

        if not isinstance(raw, discord.TextChannel):
            continue
        channel: discord.TextChannel = raw

        # Expiry
        if now >= expiry_at:
            try:
                await channel.delete(reason="Pen Pals session expired")
            except (discord.NotFound, discord.HTTPException):
                pass
            def _close_exp(sid: str = session_id):
                with open_db(db_path) as conn:
                    _close_session(conn, sid, "expired")
            await asyncio.to_thread(_close_exp)
            log.info("pen_pals: session %s expired", session_id)

            # Quest hook: running the full 24h is "seeing it through" — both
            # members fire; early-ended sessions never reach this path.
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            for uid in (user1_id, user2_id):
                await fire_member_trigger(
                    cast("Bot", bot), guild_id, uid, "pen_pal_complete",
                    occurrence=str(session_id),
                )
            continue

        # 1-hour close warning
        if not warned and (expiry_at - now) <= warn_seconds:
            try:
                await channel.send("⏰ This pen pal channel closes in 1 hour.")
            except discord.HTTPException:
                pass
            def _mark_warned(sid: str = session_id):
                with open_db(db_path) as conn:
                    _set_close_warning_sent(conn, sid)
            await asyncio.to_thread(_mark_warned)

        # Auto question (skip if too little session time remains)
        if next_q_at <= now and (expiry_at - now) >= q_suppress_seconds:
            question = await _draw_question(db_path, session_id, _cfg_allows_nsfw(cfg))
            try:
                await channel.send(
                    f"<@{user1_id}> <@{user2_id}>\n"
                    f"💬 A new question to keep things going:\n> {question}"
                )
            except discord.HTTPException as exc:
                log.warning("pen_pals: failed to post auto question in %d: %s", channel_id, exc)

            def _save_q(sid: str = session_id, q: str = question, nq: float = next_q_at):
                with open_db(db_path) as conn:
                    _record_question(conn, sid, q)
                    _advance_next_question(conn, sid, nq + _Q_INTERVAL)
            await asyncio.to_thread(_save_q)

    # Pool sweep. In instant mode, joining pairs on the spot, so a backlog only
    # forms when someone was ineligible at join time (on cooldown, mid-session)
    # and became eligible later — sweeping every tick means those pairs go out
    # within minutes instead of waiting for a scheduled round. In scheduled
    # mode nothing pairs on join at all, so the whole pool waits here for the
    # once-daily draw.
    for cfg in auto_cfgs:
        guild_id = cfg["guild_id"]
        mode = _normalize_match_mode(cfg["match_mode"] if "match_mode" in cfg.keys() else None)

        if mode == MATCH_MODE_SCHEDULED:
            if not _scheduled_round_due(cfg, now):
                continue
            pairs, left = await _do_round(bot, db_path, guild_id)
            log.info(
                "pen_pals: scheduled 8am ET round for guild %d — %d pairs, %d left over",
                guild_id, pairs, left,
            )
            continue

        def _pending(gid: int = guild_id, c=cfg) -> int:
            with open_db(db_path) as conn:
                cooldown = c["match_cooldown_seconds"]
                return len(_eligible_pool(conn, gid, time.time(), cooldown))

        if await asyncio.to_thread(_pending) < 2:
            continue
        pairs, left = await _do_round(bot, db_path, guild_id)
        log.info("pen_pals: swept guild %d — %d pairs, %d left over", guild_id, pairs, left)


# ── Signup panel ──────────────────────────────────────────────────────────────


def _build_panel_embed(
    pool_size: int, color: "discord.Color | None" = None, mode: str = DEFAULT_MATCH_MODE
) -> discord.Embed:
    if color is None:
        color = discord.Color.from_str("#5865F2")
    match_line = (
        "Matches go out once a day at 8:00 AM Eastern — "
        "join any time before then to be in the next round."
        if _normalize_match_mode(mode) == MATCH_MODE_SCHEDULED else
        "If someone's already waiting you're matched on the spot — "
        "a private channel opens for just the two of you, "
        "with a conversation starter already in it."
    )
    embed = discord.Embed(
        title="🖊️ Pen Pals",
        description=f"Get matched 1-on-1 with another server member for 24 hours.\n{match_line}",
        color=color,
    )
    label = f"{pool_size} member{'s' if pool_size != 1 else ''} waiting" if pool_size else "No one waiting yet"
    embed.add_field(name="Pool", value=label, inline=True)
    embed.set_footer(text="Matches open a private channel visible to just the two of you (and admins).")
    return embed


def _build_panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_PenPalsPanelJoinButton())
    view.add_item(_PenPalsPanelLeaveButton())
    return view


async def _refresh_panel(
    bot: discord.Client,
    guild_id: int,
    *,
    repost: bool = False,
) -> None:
    """Edit the panel embed in place, or delete+repost it at the channel bottom.

    Both paths run through the cog's ``StickyPanel`` (``core.sticky``), which
    owns the per-guild lock, the id bookkeeping and — importantly — posts the
    replacement *before* deleting the old one. The previous implementation
    deleted first and left ``channel.send`` unguarded, so a failed send
    permanently orphaned the panel row.
    """
    cog = cast("PenPalsCog | None", cast("Bot", bot).get_cog("PenPalsCog"))
    if cog is None:  # cog unloaded mid-flight
        return
    if repost:
        await cog.repost_panel(guild_id)
    else:
        await cog.panel.refresh(guild_id)


# ── Join / leave flows (shared by the panel buttons and slash commands) ──────


async def _handle_join(interaction: discord.Interaction, db_path: Path) -> None:
    if not interaction.guild:
        await interaction.response.send_message("❌ This only works in a server.", ephemeral=True)
        return

    guild = interaction.guild
    guild_id = guild.id
    user_id = interaction.user.id

    def _load_cfg():
        with open_db(db_path) as conn:
            return _get_config(conn, guild_id)

    cfg = await asyncio.to_thread(_load_cfg)
    if cfg is None or not cfg["enabled"]:
        await interaction.response.send_message(
            "❌ Pen Pals isn't set up yet — ask an admin.", ephemeral=True
        )
        return

    if cfg["opt_in_role_id"]:
        role = guild.get_role(int(cfg["opt_in_role_id"]))
        member = guild.get_member(user_id)
        if role is not None and (member is None or role not in member.roles):
            await interaction.response.send_message(
                f"❌ You need the **{role.name}** role to join Pen Pals.", ephemeral=True
            )
            return

    scheduled = _normalize_match_mode(
        cfg["match_mode"] if "match_mode" in cfg.keys() else None
    ) == MATCH_MODE_SCHEDULED

    def _check() -> tuple[str, int | None]:
        with open_db(db_path) as conn:
            if _get_active_session(conn, guild_id, user_id):
                return "active", None
            if _in_pool(conn, guild_id, user_id):
                return "in_pool", None
            _add_to_pool(conn, guild_id, user_id)
            if scheduled:
                # Scheduled mode never matches on join — everyone waits for
                # the once-a-day round.
                return "queued", None
            # Joining pairs immediately when someone eligible is already
            # waiting; the pool is only for when nobody is.
            return "queued", _find_instant_match(conn, guild_id, user_id)

    status, partner_id = await asyncio.to_thread(_check)

    if status == "active":
        await interaction.response.send_message(
            "❌ You already have an active pen pal. Use `/penpals status` to see it.", ephemeral=True
        )
        return
    if status == "in_pool":
        await interaction.response.send_message(
            "❌ You're already in the pool. Use `/penpals status` to check your position.", ephemeral=True
        )
        return

    if partner_id is None:
        await interaction.response.send_message(_QUEUED_MSG_SCHEDULED if scheduled else _QUEUED_MSG_INSTANT, ephemeral=True)
        await _refresh_panel(interaction.client, guild_id)
        return

    # Channel creation is several round-trips — defer so the token survives.
    await interaction.response.defer(ephemeral=True)
    paired = await _do_pair(interaction.client, db_path, guild_id, user_id, partner_id)
    if paired:
        def _channel_of():
            with open_db(db_path) as conn:
                row = _get_active_session(conn, guild_id, user_id)
                return row["channel_id"] if row else None

        channel_id = await asyncio.to_thread(_channel_of)
        where = f"<#{channel_id}>" if channel_id else "your new pen pal channel"
        await interaction.followup.send(
            f"🖊️ Matched! Say hi to <@{partner_id}> in {where}.", ephemeral=True
        )
    else:
        # Pairing fell through (permissions, a lost race, member left) — the
        # joiner stays pooled for the sweeper rather than losing their spot.
        # Only reachable in instant mode: scheduled mode never returns a
        # partner_id from _check above, so _do_pair is never called there.
        await interaction.followup.send(_QUEUED_MSG_INSTANT, ephemeral=True)
    await _refresh_panel(interaction.client, guild_id)


async def _handle_leave(interaction: discord.Interaction, db_path: Path) -> None:
    if not interaction.guild:
        await interaction.response.send_message("❌ This only works in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    user_id = interaction.user.id

    def _remove():
        with open_db(db_path) as conn:
            if not _in_pool(conn, guild_id, user_id):
                return False
            _remove_from_pool(conn, guild_id, user_id)
            return True

    removed = await asyncio.to_thread(_remove)
    if removed:
        await interaction.response.send_message("You've left the Pen Pals pool.", ephemeral=True)
        await _refresh_panel(interaction.client, guild_id)
    else:
        await interaction.response.send_message(
            "❌ You're not in the pool. Use `/penpals status` to check your status.", ephemeral=True
        )


class _PenPalsPanelJoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pen_pals:join",
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Join Pool",
                emoji="✉️",
                style=discord.ButtonStyle.success,
                custom_id="pen_pals:join",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = cast("Bot", interaction.client).ctx
        await _handle_join(interaction, ctx.db_path)


class _PenPalsPanelLeaveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pen_pals:leave",
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Leave Pool",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                custom_id="pen_pals:leave",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = cast("Bot", interaction.client).ctx
        await _handle_leave(interaction, ctx.db_path)


# ── Self-service blocklist (/penpals block) ───────────────────────────────────


def _block_panel_content(guild: discord.Guild, blocked_ids: list[int]) -> str:
    """Ephemeral message body listing a member's current Pen Pals blocks."""
    if not blocked_ids:
        return (
            "🚫 **Pen Pals — your blocklist**\n"
            "You have no blocks. Pick members below and Pen Pals will never "
            "match you with them. They're never told they were blocked."
        )

    def _label(uid: int) -> str:
        member = guild.get_member(uid)
        return member.display_name if member else f"User {uid}"

    lines = "\n".join(f"• {_label(u)}" for u in blocked_ids)
    return (
        "🚫 **Pen Pals — your blocklist**\n"
        "You'll never be matched with:\n"
        f"{lines}\n\n"
        "Add more below, or pick someone to unblock. Blocking someone doesn't "
        "end a chat you're already in — use `/penpals end` for that."
    )


class _PenPalsBlockView(discord.ui.View):
    """Ephemeral, single-viewer manager for a member's own Pen Pals blocklist.

    Only the invoker can see or use this (it rides on an ephemeral message), so
    it needs no author check. A user-select adds blocks; a string-select of the
    current blocks removes them. Both rebuild the view in place.
    """

    def __init__(
        self,
        db_path: Path,
        guild: discord.Guild,
        user_id: int,
        blocked_ids: list[int],
    ) -> None:
        super().__init__(timeout=180)
        self.db_path = db_path
        self.guild = guild
        self.user_id = user_id
        self.blocked_ids = blocked_ids
        self._build()

    def _label(self, uid: int) -> str:
        member = self.guild.get_member(uid)
        return member.display_name if member else f"User {uid}"

    def content(self) -> str:
        return _block_panel_content(self.guild, self.blocked_ids)

    def _build(self) -> None:
        self.clear_items()

        add = discord.ui.UserSelect(placeholder="Block someone…", min_values=1, max_values=25)
        add.callback = self._on_add  # type: ignore[method-assign]
        self.add_item(add)

        if self.blocked_ids:
            options = [
                discord.SelectOption(label=self._label(u)[:100], value=str(u))
                for u in self.blocked_ids[:25]
            ]
            remove = discord.ui.Select(
                placeholder="Unblock someone…",
                min_values=1,
                max_values=len(options),
                options=options,
            )
            remove.callback = self._on_remove  # type: ignore[method-assign]
            self.add_item(remove)

    async def _reload_and_edit(self, interaction: discord.Interaction) -> None:
        def _load() -> list[int]:
            with open_db(self.db_path) as conn:
                return _get_member_blocks(conn, self.guild.id, self.user_id)

        self.blocked_ids = await asyncio.to_thread(_load)
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        select = cast(discord.ui.UserSelect, self.children[0])
        # Never block yourself (you can't be matched with yourself) or bots.
        ids = [u.id for u in select.values if u.id != self.user_id and not getattr(u, "bot", False)]

        def _save() -> None:
            with open_db(self.db_path) as conn:
                for uid in ids:
                    _add_block(conn, self.guild.id, self.user_id, uid)

        await asyncio.to_thread(_save)
        await self._reload_and_edit(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        select = cast(discord.ui.Select, self.children[1])
        ids = [int(v) for v in select.values]

        def _save() -> None:
            with open_db(self.db_path) as conn:
                for uid in ids:
                    _remove_block(conn, self.guild.id, self.user_id, uid)

        await asyncio.to_thread(_save)
        await self._reload_and_edit(interaction)


async def _handle_block(interaction: discord.Interaction, db_path: Path) -> None:
    if not interaction.guild:
        await interaction.response.send_message("❌ This only works in a server.", ephemeral=True)
        return

    guild = interaction.guild
    user_id = interaction.user.id

    def _load() -> list[int]:
        with open_db(db_path) as conn:
            return _get_member_blocks(conn, guild.id, user_id)

    blocked = await asyncio.to_thread(_load)
    view = _PenPalsBlockView(db_path, guild, user_id, blocked)
    await interaction.response.send_message(view.content(), view=view, ephemeral=True)


# ── Confirm view for /penpals end ─────────────────────────────────────────────


class _EndConfirmView(discord.ui.View):
    def __init__(
        self,
        db_path: Path,
        session_id: str,
        channel: discord.TextChannel,
        other_user_id: int,
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=15)
        self.db_path = db_path
        self.session_id = session_id
        self.channel = channel
        self.other_user_id = other_user_id
        self.invoker_id = invoker_id
        self._msg: discord.Message | None = None
        self._done = False

    @discord.ui.button(label="End Session", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Only the person who initiated can confirm.", ephemeral=True)
            return
        self._done = True
        self.stop()
        await interaction.response.edit_message(content="Closing pen pal session…", view=None)

        # DM the other member before the channel (and this interaction) go away
        guild = self.channel.guild
        other = guild.get_member(self.other_user_id) if guild else None
        if other:
            try:
                await other.send(
                    f"Your pen pal session in **{guild.name}** was ended early by your partner."
                )
            except discord.HTTPException:
                pass

        # Delete the channel first, then close the session row. If the close
        # doesn't happen (crash), the loop finds the channel missing and marks
        # the session closed itself — the reverse order could orphan a live
        # channel behind an already-closed session.
        try:
            await self.channel.delete(reason="Pen Pals ended early")
        except discord.HTTPException as exc:
            log.warning("pen_pals: failed to delete channel %d on early end: %s", self.channel.id, exc)

        def _close(sid: str = self.session_id):
            with open_db(self.db_path) as conn:
                _close_session(conn, sid, "early")
        await asyncio.to_thread(_close)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._done = True
        self.stop()
        await interaction.response.edit_message(content="Close cancelled.", view=None)

    async def on_timeout(self) -> None:
        if self._done:
            return
        if self._msg:
            try:
                await self._msg.edit(content="Close cancelled.", view=None)
            except discord.HTTPException:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────


class PenPalsCog(commands.Cog):
    penpals = app_commands.Group(
        name="penpals",
        description="Pen Pals — get matched with someone for a private chat.",
    )

    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        self._panel_channels: dict[int, int] = {}  # panel_channel_id → guild_id
        self.panel = StickyPanel(
            "pen pals",
            bot,
            load_ids=self._panel_ids,
            save_ids=self._save_panel_ids,
            build=self._build_panel,
        )
        super().__init__()

    async def cog_unload(self) -> None:
        self.panel.cancel_all()

    # ── panel plumbing (core.sticky) ─────────────────────────────────────

    def _panel_ids(self, guild_id: int) -> tuple[int, int]:
        with open_db(self.ctx.db_path) as conn:
            cfg = _get_config(conn, guild_id)
        if cfg is None:
            return 0, 0
        return int(cfg["panel_channel_id"] or 0), int(cfg["panel_message_id"] or 0)

    def _save_panel_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with open_db(self.ctx.db_path) as conn:
            _set_panel_message_id(conn, guild_id, message_id)

    async def _build_panel(self, guild: discord.Guild) -> PanelContent:
        def _load():
            with open_db(self.ctx.db_path) as conn:
                cfg = _get_config(conn, guild.id)
                return cfg, len(_get_pool(conn, guild.id))

        cfg, pool_size = await asyncio.to_thread(_load)
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        mode = _normalize_match_mode(
            cfg["match_mode"] if cfg is not None and "match_mode" in cfg.keys() else None
        )
        return PanelContent(
            embed=_build_panel_embed(pool_size, color=accent, mode=mode),
            view=_build_panel_view(),
        )

    async def repost_panel(self, guild_id: int) -> None:
        """Move the panel to the bottom of its configured channel."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel_id, _ = await self.panel.ids(guild_id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            await self.panel.place(guild, channel)

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.ctx.db_path

        bot.add_dynamic_items(_PenPalsPanelJoinButton)
        bot.add_dynamic_items(_PenPalsPanelLeaveButton)

        def _load_panels():
            with open_db(db_path) as conn:
                rows = conn.execute(
                    "SELECT guild_id, panel_channel_id FROM pen_pals_config WHERE panel_channel_id != 0"
                ).fetchall()
                return {int(r["panel_channel_id"]): int(r["guild_id"]) for r in rows}

        self._panel_channels = await asyncio.to_thread(_load_panels)
        # Publish the panel-guild set: the listener then rejects everything else
        # with a set lookup, as the old channel-map gate did (no DB read).
        self.panel.set_known_guilds(set(self._panel_channels.values()))
        self.bot.startup_task_factories.append(lambda: _pen_pals_loop(bot, db_path))

    @commands.Cog.listener("on_message")
    async def _on_message_panel(self, message: discord.Message) -> None:
        await self.panel.on_message(message)

    @commands.Cog.listener("on_member_remove")
    async def _on_member_remove(self, member: discord.Member) -> None:
        """A member left / was kicked or banned. If they were mid-session,
        tear it down and re-queue their partner; if only pooled, drop them."""
        db_path = self.ctx.db_path
        guild_id = member.guild.id

        def _lookup():
            with open_db(db_path) as conn:
                was_pooled = _in_pool(conn, guild_id, member.id)
                if was_pooled:
                    _remove_from_pool(conn, guild_id, member.id)
                return _get_active_session(conn, guild_id, member.id), was_pooled

        session, was_pooled = await asyncio.to_thread(_lookup)
        if session is not None:
            await _end_session_abnormally(
                self.bot, db_path, session,
                reason="member_left", departed_user_id=member.id, delete_channel=True,
            )
        elif was_pooled:
            try:
                await self.panel.refresh(guild_id)
            except discord.HTTPException as exc:
                log.warning("pen_pals: panel refresh after member leave failed in %d: %s", guild_id, exc)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _on_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """A pen pal channel was deleted (usually by a mod). Close the session
        and return both members to the pool."""
        if not isinstance(channel, discord.TextChannel):
            return
        db_path = self.ctx.db_path

        def _lookup():
            with open_db(db_path) as conn:
                return _get_session_by_channel(conn, channel.id)

        session = await asyncio.to_thread(_lookup)
        if session is None:
            return
        await _end_session_abnormally(
            self.bot, db_path, session,
            reason="channel_deleted", departed_user_id=None, delete_channel=False,
        )

    @commands.Cog.listener("on_pen_pals_panel_refresh")
    async def _on_panel_refresh(
        self, guild_id: int, new_channel_id: int, old_channel_id: int, old_message_id: int
    ) -> None:
        # Delete old panel from the previous channel if the channel changed
        if old_channel_id and old_channel_id != new_channel_id and old_message_id:
            old_ch = self.bot.get_channel(old_channel_id)
            if isinstance(old_ch, discord.TextChannel):
                try:
                    old = await old_ch.fetch_message(old_message_id)
                    await old.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            def _clear():
                with open_db(self.ctx.db_path) as conn:
                    _set_panel_message_id(conn, guild_id, 0)

            await asyncio.to_thread(_clear)

        # Update in-memory channel map
        self._panel_channels = {ch: g for ch, g in self._panel_channels.items() if g != guild_id}
        if new_channel_id:
            self._panel_channels[new_channel_id] = guild_id
            await self.repost_panel(guild_id)
        self.panel.set_known_guilds(set(self._panel_channels.values()))

    # ── /penpals join ─────────────────────────────────────────────────

    @penpals.command(name="join", description="Get matched with a pen pal now, or wait for the next person to join.")
    async def penpals_join(self, interaction: discord.Interaction) -> None:
        await _handle_join(interaction, self.ctx.db_path)

    # ── /penpals leave ────────────────────────────────────────────────

    @penpals.command(name="leave", description="Leave the Pen Pals pool before being matched.")
    async def penpals_leave(self, interaction: discord.Interaction) -> None:
        await _handle_leave(interaction, self.ctx.db_path)

    # ── /penpals block ────────────────────────────────────────────────

    @penpals.command(
        name="block",
        description="Manage who Pen Pals should never match you with.",
    )
    async def penpals_block(self, interaction: discord.Interaction) -> None:
        await _handle_block(interaction, self.ctx.db_path)

    # ── /penpals status ───────────────────────────────────────────────

    @penpals.command(name="status", description="Check your current Pen Pals status.")
    async def penpals_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        db_path = self.ctx.db_path

        def _check():
            with open_db(db_path) as conn:
                session = _get_active_session(conn, guild_id, user_id)
                if session:
                    cfg = _get_config(conn, guild_id)
                    max_swaps = cfg["max_question_swaps"] if cfg else _MAX_SWAPS
                    return "active", (dict(session), max_swaps)
                pool = [r["user_id"] for r in _get_pool(conn, guild_id)]
                if user_id in pool:
                    cfg = _get_config(conn, guild_id)
                    mode = _normalize_match_mode(
                        cfg["match_mode"] if cfg and "match_mode" in cfg.keys() else None
                    )
                    return "pool", (pool.index(user_id) + 1, mode)
                return "none", None

        status, data = await asyncio.to_thread(_check)

        if status == "active":
            session_data, max_swaps = cast("tuple[dict, int]", data)
            ch = interaction.guild.get_channel(session_data["channel_id"])
            other_id = session_data["user2_id"] if session_data["user1_id"] == user_id else session_data["user1_id"]
            other = interaction.guild.get_member(other_id)
            expiry_at = int(session_data["expiry_at"])
            swaps_left = max_swaps - session_data["question_swaps_used"]
            lines = [
                f"You have an active pen pal: {other.mention if other else f'<@{other_id}>'}",
                f"Channel: {ch.mention if ch else '(channel missing)'}",
                f"Expires: <t:{expiry_at}:R>",
                f"Question swaps remaining: **{swaps_left}**",
            ]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        elif status == "pool":
            pos, mode = cast("tuple[int, str]", data)
            when = (
                "in the next daily round, at 8:00 AM Eastern"
                if mode == MATCH_MODE_SCHEDULED
                else "as soon as someone eligible joins"
            )
            await interaction.response.send_message(
                f"You're in the pool at position **#{pos}** — you'll be matched {when}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You're not in the pool and have no active pen pal. Use `/penpals join` to sign up.",
                ephemeral=True,
            )

    # ── /penpals new-question ─────────────────────────────────────────

    @penpals.command(name="new-question", description="Swap the current question for a fresh one (limited swaps per session).")
    async def penpals_new_question(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return

        db_path = self.ctx.db_path
        if interaction.channel_id is None:
            await interaction.response.send_message("❌ This command only works in an active pen pal channel.", ephemeral=True)
            return
        channel_id: int = interaction.channel_id

        def _load():
            with open_db(db_path) as conn:
                session = _get_session_by_channel(conn, channel_id)
                cfg = _get_config(conn, session["guild_id"]) if session else None
                return session, cfg

        session, cfg = await asyncio.to_thread(_load)
        if session is None:
            await interaction.response.send_message(
                "❌ This command only works in an active pen pal channel.", ephemeral=True
            )
            return

        if interaction.user.id not in (session["user1_id"], session["user2_id"]):
            await interaction.response.send_message(
                "❌ Only the two pen pals can swap the question.", ephemeral=True
            )
            return

        max_swaps = cfg["max_question_swaps"] if cfg else _MAX_SWAPS
        if session["question_swaps_used"] >= max_swaps:
            await interaction.response.send_message(
                f"❌ You've used all {max_swaps} question swaps for this session.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        question = await _draw_question(db_path, session["session_id"], _cfg_allows_nsfw(cfg))

        def _save():
            with open_db(db_path) as conn:
                swaps_used = _increment_swaps(conn, session["session_id"])
                _record_question(conn, session["session_id"], question)
                return swaps_used

        swaps_used = await asyncio.to_thread(_save)
        swaps_left = max_swaps - swaps_used

        user1_id = session["user1_id"]
        user2_id = session["user2_id"]
        chan = interaction.channel
        if isinstance(chan, discord.TextChannel):
            try:
                await chan.send(
                    f"<@{user1_id}> <@{user2_id}>\n"
                    f"🔄 New question ({swaps_left} swap{'s' if swaps_left != 1 else ''} remaining):\n"
                    f"> {question}"
                )
            except discord.HTTPException as exc:
                log.warning("pen_pals: failed to post swap question in %d: %s", channel_id, exc)

        await interaction.followup.send("Question swapped!", ephemeral=True)

    # ── /penpals end ──────────────────────────────────────────────────

    @penpals.command(name="end", description="End your current pen pal session early.")
    async def penpals_end(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return

        db_path = self.ctx.db_path
        user_id = interaction.user.id
        if interaction.channel_id is None:
            await interaction.response.send_message("❌ This command only works in your active pen pal channel.", ephemeral=True)
            return
        channel_id: int = interaction.channel_id

        def _load():
            with open_db(db_path) as conn:
                return _get_session_by_channel(conn, channel_id)

        session = await asyncio.to_thread(_load)
        if session is None or user_id not in (session["user1_id"], session["user2_id"]):
            await interaction.response.send_message(
                "❌ This command only works in your active pen pal channel.", ephemeral=True
            )
            return

        chan = interaction.channel
        if not isinstance(chan, discord.TextChannel):
            await interaction.response.send_message("❌ This command only works in your active pen pal channel.", ephemeral=True)
            return

        other_id = session["user2_id"] if session["user1_id"] == user_id else session["user1_id"]
        view = _EndConfirmView(
            db_path=db_path,
            session_id=session["session_id"],
            channel=chan,
            other_user_id=other_id,
            invoker_id=user_id,
        )
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this pen pal session early?", view=view, ephemeral=True
        )
        msg = await interaction.original_response()
        view._msg = msg

    # ── /penpals pair (admin) ─────────────────────────────────────────

    @penpals.command(name="pair", description="Force-pair two specific members.")
    @app_commands.describe(user1="First member", user2="Second member")
    @app_commands.default_permissions(manage_guild=True)
    async def penpals_pair(
        self,
        interaction: discord.Interaction,
        user1: discord.Member,
        user2: discord.Member,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return
        if user1 == user2:
            await interaction.response.send_message("❌ You can't pair someone with themselves.", ephemeral=True)
            return

        db_path = self.ctx.db_path
        guild_id = interaction.guild.id

        def _check() -> str:
            with open_db(db_path) as conn:
                return _force_pair_status(conn, guild_id, user1.id, user2.id)

        status = await asyncio.to_thread(_check)
        if status == "disabled":
            await interaction.response.send_message("❌ Pen Pals isn't enabled on this server.", ephemeral=True)
            return
        if status == "blocked":
            await interaction.response.send_message(
                "❌ These two can't be paired — one has blocked the other, or they're "
                "on the Pen Pals separations list. Clear the block first if this is intended.",
                ephemeral=True,
            )
            return
        if status in ("active_1", "active_2"):
            who = user1 if status == "active_1" else user2
            await interaction.response.send_message(f"❌ {who.mention} already has an active pen pal.", ephemeral=True)
            return
        if status in ("not_opted_in_1", "not_opted_in_2"):
            # Consent isn't a mod override: naming the member (no ping) tells
            # the mod who still has to run /penpals join.
            who = user1 if status == "not_opted_in_1" else user2
            await interaction.response.send_message(
                f"❌ **{who.display_name}** hasn't opted in to Pen Pals — they need to run "
                "`/penpals join` first. Force-pairing skips the queue, not consent.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        success = await _do_pair(self.bot, db_path, guild_id, user1.id, user2.id)
        if success:
            await interaction.followup.send(
                f"✅ Paired {user1.mention} × {user2.mention}.", ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Failed to create the channel — check bot permissions.", ephemeral=True)

    # ── /penpals round (admin) ────────────────────────────────────────

    @penpals.command(name="round", description="Pair everyone currently in the pool.")
    @app_commands.default_permissions(manage_guild=True)
    async def penpals_round(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        pairs, left = await _do_round(self.bot, self.ctx.db_path, interaction.guild.id)
        msg = f"✅ Paired **{pairs}** {'pair' if pairs == 1 else 'pairs'}."
        if left:
            msg += f" **{left}** member{'s' if left != 1 else ''} still in the pool (waiting or on cooldown)."
        else:
            msg += " Pool is now empty."
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(PenPalsCog(bot, bot.ctx))
