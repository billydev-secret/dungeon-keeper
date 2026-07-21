"""Grant-audit service — durable prune ledger + bucketing for the dashboard panel.

The inactivity-prune loop removes a configured role from long-inactive
members with no hold row anywhere; ``role_prune_events`` is its durable "why"
record. The Grant Audit panel reads that ledger and splits members missing a
grant role into three buckets:

- **waiting for first grant** — leveled up, never granted, never pruned;
- **stripped but came back** — pruned, active again, never re-granted;
- **recent inactive stripped** — pruned and still inactive (newest first).

``restored_at`` is set the moment a mod re-grants (a discrete fact worth
storing); "is this member active again" stays a live computation against
``get_member_last_activity_map`` since it's inherently a moving target.

The same buckets also render as an auto-updating Discord embed — the
**grant-audit card**, posted by ``/grant_audit`` and refreshed hourly in
place by :func:`grant_audit_card_loop` (same channel-id/message-id config
pattern as the economy leaderboard panel; deleting the message retires it).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import discord

log = logging.getLogger("dungeonkeeper.grant_audit")


class _HasCreatedAt(Protocol):
    @property
    def created_at(self) -> float: ...


# ---------------------------------------------------------------------------
# Ledger writes / reads
# ---------------------------------------------------------------------------


def record_prune_events(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: Iterable[int],
    role_id: int,
    pruned_at: float,
    source: str = "inactivity_prune",
) -> int:
    """Record one open prune event per user; returns rows inserted."""
    rows = [(guild_id, uid, role_id, source, pruned_at) for uid in user_ids]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO role_prune_events (guild_id, user_id, role_id, source, pruned_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def mark_restored(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    role_id: int,
    restored_at: float,
) -> int:
    """Close any open prune events for (user, role); returns rows closed."""
    cursor = conn.execute(
        "UPDATE role_prune_events SET restored_at = ? "
        "WHERE guild_id = ? AND user_id = ? AND role_id = ? AND restored_at IS NULL",
        (restored_at, guild_id, user_id, role_id),
    )
    return cursor.rowcount


def get_open_prune_events(
    conn: sqlite3.Connection, guild_id: int, role_id: int
) -> list[sqlite3.Row]:
    """Open (unrestored) prune events, one row per user with the latest pruned_at."""
    return conn.execute(
        """
        SELECT user_id, MAX(pruned_at) AS pruned_at
        FROM role_prune_events
        WHERE guild_id = ? AND role_id = ? AND restored_at IS NULL
        GROUP BY user_id
        """,
        (guild_id, role_id),
    ).fetchall()


def get_ever_pruned_ids(
    conn: sqlite3.Connection, guild_id: int, role_id: int
) -> set[int]:
    """Every user with any prune event for this role, open or restored."""
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM role_prune_events WHERE guild_id = ? AND role_id = ?",
        (guild_id, role_id),
    ).fetchall()
    return {int(r["user_id"]) for r in rows}


def get_hold_excluded_ids(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[set[int], set[int]]:
    """DB-side hold exclusions + configured hold role ids for the live check.

    Members on an active inactive-channel hold or in jail had every role
    stripped on purpose — they must never appear in any audit bucket. The
    returned hold role ids let the caller also exclude members who hold the
    Inactive/Jailed role live in Discord without a matching DB row (a mod who
    stripped roles by hand).
    """
    from bot_modules.core.db_utils import get_config_value
    from bot_modules.inactive.store import active_inactive_user_ids
    from bot_modules.services.moderation import active_jailed_user_ids

    held_ids = active_inactive_user_ids(conn, guild_id) | active_jailed_user_ids(
        conn, guild_id
    )
    hold_role_ids = {
        rid
        for rid in (
            int(get_config_value(conn, "inactive_role_id", "0", guild_id) or "0"),
            int(get_config_value(conn, "jailed_role_id", "0", guild_id) or "0"),
        )
        if rid > 0
    }
    return held_ids, hold_role_ids


# ---------------------------------------------------------------------------
# Bucketing (pure)
# ---------------------------------------------------------------------------


def compute_waiting_for_first_grant(
    levels: dict[int, int],
    granted_ids: set[int],
    ever_pruned_ids: set[int],
) -> list[tuple[int, int]]:
    """``(user_id, level)`` pairs at/above the level bar with no grant and no
    prune history at all — the role was plain never given. Highest level first."""
    out = [
        (uid, lvl)
        for uid, lvl in levels.items()
        if uid not in granted_ids and uid not in ever_pruned_ids
    ]
    out.sort(key=lambda p: -p[1])
    return out


def _open_event_pairs(open_events: Iterable) -> list[tuple[int, float | None]]:
    # pruned_at None = an *implicit* strip (grant evidence exists but the
    # removal was never recorded — e.g. it happened while the bot was down).
    return [
        (
            int(ev["user_id"]),
            float(ev["pruned_at"]) if ev["pruned_at"] is not None else None,
        )
        for ev in open_events
    ]


def _newest_first(rows: list[dict]) -> list[dict]:
    """Sort by pruned_at desc, unknown-date (implicit) strips last."""
    rows.sort(
        key=lambda r: r["pruned_at"] if r["pruned_at"] is not None else float("-inf"),
        reverse=True,
    )
    return rows


def compute_stripped_returned(
    open_events: Iterable,
    granted_ids: set[int],
    activity_map: Mapping[int, _HasCreatedAt],
    cutoff_ts: float,
) -> list[dict]:
    """Open prune event, still not re-granted, but active again (at/after the
    cutoff) — pruned fairly, came back, and nobody closed the loop."""
    out = [
        {"user_id": uid, "pruned_at": pruned_at}
        for uid, pruned_at in _open_event_pairs(open_events)
        if uid not in granted_ids
        and (a := activity_map.get(uid)) is not None
        and a.created_at >= cutoff_ts
    ]
    return _newest_first(out)


def compute_recent_inactive(
    open_events: Iterable,
    granted_ids: set[int],
    activity_map: Mapping[int, _HasCreatedAt],
    cutoff_ts: float,
    limit: int = 10,
) -> list[dict]:
    """Open prune event and still inactive (no activity, or all before the
    cutoff) — the prune is working as intended. Newest prunes first, capped."""
    out = [
        {"user_id": uid, "pruned_at": pruned_at}
        for uid, pruned_at in _open_event_pairs(open_events)
        if uid not in granted_ids
        and ((a := activity_map.get(uid)) is None or a.created_at < cutoff_ts)
    ]
    return _newest_first(out)[:limit]


# ---------------------------------------------------------------------------
# One-off backfill from role_events history
# ---------------------------------------------------------------------------


def backfill_prune_events_from_role_events(
    conn: sqlite3.Connection,
    guild: discord.Guild,
    role: discord.Role,
    inactivity_days: int,
    *,
    now: float | None = None,
) -> int:
    """Seed ``role_prune_events`` from historical ``role_events`` removals.

    Idempotent: users who already have any prune event for this role are
    skipped, so running it twice inserts nothing new. A removal only counts
    as a prune if the member's last activity doesn't disprove it — activity
    inside the prune window at removal time, or no activity record at all,
    means the prune loop can't have done it (it never strips without an
    activity record older than the window). Members who hold the role again
    are inserted already-restored so they don't reopen the audit.

    Needs live Discord state (current role membership), so it's called once
    from a REPL/manage path, not a migration step.
    """
    from bot_modules.core.xp_system import get_member_last_activity_map

    now_ts = now if now is not None else time.time()
    guild_id = guild.id
    rows = conn.execute(
        "SELECT user_id, MAX(granted_at) AS removed_at FROM role_events "
        "WHERE guild_id = ? AND role_name = ? AND action = 'remove' GROUP BY user_id",
        (guild_id, role.name),
    ).fetchall()
    if not rows:
        return 0

    already_recorded = {
        int(r["user_id"])
        for r in conn.execute(
            "SELECT DISTINCT user_id FROM role_prune_events "
            "WHERE guild_id = ? AND role_id = ?",
            (guild_id, role.id),
        ).fetchall()
    }
    candidates = [
        (int(r["user_id"]), float(r["removed_at"]))
        for r in rows
        if int(r["user_id"]) not in already_recorded
    ]
    if not candidates:
        return 0

    current_holder_ids = {m.id for m in role.members}
    activity_map = get_member_last_activity_map(
        conn, guild_id, [uid for uid, _ in candidates]
    )
    window_secs = inactivity_days * 86400
    inserted = 0
    for uid, removed_at in candidates:
        activity = activity_map.get(uid)
        if activity is None:
            continue
        if (
            activity.created_at < removed_at
            and removed_at - activity.created_at < window_secs
        ):
            # Active inside the window when removed — a mod removal, not a prune.
            continue
        restored_at = now_ts if uid in current_holder_ids else None
        conn.execute(
            "INSERT INTO role_prune_events "
            "(guild_id, user_id, role_id, source, pruned_at, restored_at) "
            "VALUES (?, ?, ?, 'inactivity_prune', ?, ?)",
            (guild_id, uid, role.id, removed_at, restored_at),
        )
        inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Snapshot assembly (shared by the web route and the Discord card)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantAuditGather:
    """The DB half of a grant-audit read — no Discord state touched yet."""

    levels: dict[int, int]
    held_ids: set[int]
    hold_role_ids: set[int]
    open_events: list[tuple[int, float]]  # (user_id, pruned_at)
    ever_pruned_ids: set[int]
    # Everyone with a role_events grant row for this role name — evidence they
    # held the role once, even when the removal was never recorded.
    ever_granted_ids: set[int]
    inactivity_days: int
    activity_map: Mapping[int, _HasCreatedAt]


@dataclass(frozen=True)
class GrantAuditSnapshot:
    """Fully resolved buckets — rows are dicts with user_id/display_name/
    level (waiting) plus pruned_at (the two stripped buckets)."""

    min_level: int
    inactivity_days: int
    waiting: list[dict]
    returned: list[dict]
    inactive: list[dict]


def gather_grant_audit(
    conn: sqlite3.Connection,
    guild_id: int,
    role_id: int,
    min_level: int,
    role_name: str = "",
) -> GrantAuditGather:
    """One sync read of everything the buckets need from the database.

    ``role_name`` keys the grant-history lookup in ``role_events`` (which
    stores names, not ids); pass the role's current name. Note a role rename
    orphans its older history — an accepted limitation of role_events.
    """
    from bot_modules.core.xp_system import get_member_last_activity_map

    levels = {
        int(r["user_id"]): int(r["level"])
        for r in conn.execute(
            "SELECT user_id, level FROM member_xp WHERE guild_id=? AND level>=?",
            (guild_id, min_level),
        ).fetchall()
    }
    held_ids, hold_role_ids = get_hold_excluded_ids(conn, guild_id)
    open_events = [
        (int(ev["user_id"]), float(ev["pruned_at"]))
        for ev in get_open_prune_events(conn, guild_id, role_id)
    ]
    ever_pruned = get_ever_pruned_ids(conn, guild_id, role_id)
    ever_granted: set[int] = set()
    if role_name:
        ever_granted = {
            int(r["user_id"])
            for r in conn.execute(
                "SELECT DISTINCT user_id FROM role_events "
                "WHERE guild_id=? AND role_name=? AND action='grant'",
                (guild_id, role_name),
            ).fetchall()
        }
    rule = conn.execute(
        "SELECT role_id, inactivity_days FROM inactivity_prune_rules WHERE guild_id=?",
        (guild_id,),
    ).fetchone()
    days = (
        int(rule["inactivity_days"])
        if rule is not None and int(rule["role_id"]) == role_id
        else 30
    )
    lookup_ids = set(levels) | {uid for uid, _ in open_events} | ever_granted
    activity_map = get_member_last_activity_map(conn, guild_id, list(lookup_ids))
    return GrantAuditGather(
        levels=levels,
        held_ids=held_ids,
        hold_role_ids=hold_role_ids,
        open_events=open_events,
        ever_pruned_ids=ever_pruned,
        ever_granted_ids=ever_granted,
        inactivity_days=days,
        activity_map=activity_map,
    )


def resolve_grant_audit_buckets(
    guild: discord.Guild,
    role: discord.Role,
    gathered: GrantAuditGather,
    min_level: int,
    now_ts: float,
) -> GrantAuditSnapshot:
    """Cross the DB gather with live Discord state into the three buckets.

    Excludes bots, members who left, current role holders, and anyone on an
    inactive/jail hold — checked against both the DB hold rows and a hold
    role held live in Discord (a mod may have stripped roles by hand without
    a DB row).
    """
    granted_ids = {m.id for m in role.members}
    cutoff_ts = now_ts - gathered.inactivity_days * 86400

    def _resolve(uid: int) -> discord.Member | None:
        if uid in gathered.held_ids:
            return None
        member = guild.get_member(uid)
        if member is None or member.bot:
            return None
        if gathered.hold_role_ids and any(
            r.id in gathered.hold_role_ids for r in member.roles
        ):
            return None
        return member

    # role_events isn't gapless (removals during bot downtime are never
    # logged), so a member with grant evidence, no ledger row, and no role is
    # an *implicit* open strip with an unknown date — bucketed by activity
    # like any other open event, never shown as "waiting for first grant".
    stripped_history_ids = gathered.ever_pruned_ids | gathered.ever_granted_ids
    implicit_ids = (
        gathered.ever_granted_ids - gathered.ever_pruned_ids - granted_ids
    )
    open_events: list[dict] = [
        {"user_id": uid, "pruned_at": pruned_at}
        for uid, pruned_at in gathered.open_events
    ] + [{"user_id": uid, "pruned_at": None} for uid in sorted(implicit_ids)]

    waiting = []
    for uid, level in compute_waiting_for_first_grant(
        gathered.levels, granted_ids, stripped_history_ids
    ):
        member = _resolve(uid)
        if member is None:
            continue
        waiting.append(
            {"user_id": uid, "display_name": member.display_name, "level": level}
        )

    def _event_rows(bucket: list[dict]) -> list[dict]:
        rows = []
        for entry in bucket:
            member = _resolve(entry["user_id"])
            if member is None:
                continue
            rows.append(
                {
                    "user_id": entry["user_id"],
                    "display_name": member.display_name,
                    "level": gathered.levels.get(entry["user_id"]),
                    "pruned_at": entry["pruned_at"],
                }
            )
        return rows

    returned = _event_rows(
        compute_stripped_returned(
            open_events, granted_ids, gathered.activity_map, cutoff_ts
        )
    )
    inactive = _event_rows(
        compute_recent_inactive(
            open_events, granted_ids, gathered.activity_map, cutoff_ts
        )
    )
    return GrantAuditSnapshot(
        min_level=min_level,
        inactivity_days=gathered.inactivity_days,
        waiting=waiting,
        returned=returned,
        inactive=inactive,
    )


# ---------------------------------------------------------------------------
# Auto-updating Discord card
# ---------------------------------------------------------------------------

# Waiting can be arbitrarily long; the two stripped buckets are naturally
# small (recent_inactive is capped at 10 upstream).
_CARD_WAITING_CAP = 15

_CARD_KEYS = (
    "grant_audit_card_channel_id",
    "grant_audit_card_message_id",
    "grant_audit_card_grant_name",
    "grant_audit_card_min_level",
)


def _rel(ts: float) -> str:
    return f"<t:{int(ts)}:R>"


def build_grant_audit_embed(
    label: str,
    snap: GrantAuditSnapshot,
    *,
    now_ts: float,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The mod-facing card: same three buckets as the dashboard panel."""
    embed = discord.Embed(
        title=f"📋 Grant audit — {label}",
        description=(
            f"Members at level {snap.min_level}+ missing **{label}**, split by "
            "why. Excludes inactive/jail holds."
        ),
        color=color,
    )

    waiting_lines = [
        f"**{r['display_name']}** — level {r['level']}"
        for r in snap.waiting[:_CARD_WAITING_CAP]
    ]
    extra = len(snap.waiting) - _CARD_WAITING_CAP
    if extra > 0:
        waiting_lines.append(f"…and {extra} more on the dashboard.")
    embed.add_field(
        name=f"🕐 Waiting for first grant ({len(snap.waiting)})",
        value="\n".join(waiting_lines) or "Nobody — all clear.",
        inline=False,
    )

    def _stripped_when(pruned_at: float | None) -> str:
        # None = implicit strip: grant evidence exists but the removal was
        # never recorded (e.g. it happened during bot downtime).
        return (
            f"stripped {_rel(pruned_at)}"
            if pruned_at is not None
            else "stripped (date unrecorded)"
        )

    returned_lines = [
        f"**{r['display_name']}**"
        + (f" — level {r['level']}" if r["level"] is not None else "")
        + f" · {_stripped_when(r['pruned_at'])}"
        for r in snap.returned
    ]
    embed.add_field(
        name=f"↩️ Stripped but came back ({len(snap.returned)})",
        value="\n".join(returned_lines) or "Nobody — all clear.",
        inline=False,
    )

    inactive_lines = [
        f"{r['display_name']} · {_stripped_when(r['pruned_at'])}"
        for r in snap.inactive
    ]
    embed.add_field(
        name="💤 Recently stripped, still inactive",
        value="\n".join(inactive_lines)
        or f"Nobody stripped by the {snap.inactivity_days}d inactivity prune.",
        inline=False,
    )

    embed.set_footer(
        text=(
            "Auto-updates hourly · repost with /grant_audit · "
            "delete this message to retire the card"
        )
    )
    embed.timestamp = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    return embed


@dataclass(frozen=True)
class CardRef:
    channel_id: int
    message_id: int
    grant_name: str
    min_level: int


def load_card_ref(conn: sqlite3.Connection, guild_id: int) -> CardRef:
    from bot_modules.core.db_utils import get_config_value

    values = [
        get_config_value(conn, key, "0", guild_id, allow_legacy_fallback=False)
        for key in _CARD_KEYS
    ]
    return CardRef(
        channel_id=int(values[0] or "0"),
        message_id=int(values[1] or "0"),
        grant_name=values[2] if values[2] != "0" else "",
        min_level=max(1, int(values[3] or "0") or 5),
    )


def save_card_ref(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    grant_name: str,
    min_level: int,
) -> None:
    from bot_modules.core.db_utils import set_config_value

    for key, value in zip(
        _CARD_KEYS, (str(channel_id), str(message_id), grant_name, str(min_level))
    ):
        set_config_value(conn, key, value, guild_id)


def clear_card_ref(conn: sqlite3.Connection, guild_id: int) -> None:
    from bot_modules.core.db_utils import delete_config_value

    for key in _CARD_KEYS:
        delete_config_value(conn, key, guild_id)


def guilds_with_card(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT guild_id FROM config "
        "WHERE key = 'grant_audit_card_message_id' AND value != '0'",
    ).fetchall()
    return [int(r["guild_id"]) for r in rows]


async def refresh_grant_audit_card(
    bot: discord.Client, db_path: Path, guild_id: int, *, now_ts: float | None = None
) -> None:
    """In-place refresh of a guild's grant-audit card.

    A deleted card message (404) clears the stored ref so the loop stops
    retrying — deleting the message is how mods retire the card. A missing
    grant config or role also clears it (the card can't render sensibly).
    Any other Discord error leaves the ref for the next tick.
    """
    from bot_modules.core.branding import resolve_accent_color
    from bot_modules.core.db_utils import get_grant_roles, open_db

    now = now_ts if now_ts is not None else time.time()

    def _load_ref():
        with open_db(db_path) as conn:
            ref = load_card_ref(conn, guild_id)
            cfg = None
            if ref.message_id and ref.grant_name:
                cfg = get_grant_roles(conn, guild_id).get(ref.grant_name)
                if cfg is not None and int(cfg["role_id"]) <= 0:
                    cfg = None
            return ref, cfg

    ref, cfg = await asyncio.to_thread(_load_ref)
    if not ref.message_id:
        return

    def _clear() -> None:
        with open_db(db_path) as conn:
            clear_card_ref(conn, guild_id)

    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    role = guild.get_role(int(cfg["role_id"])) if cfg is not None else None
    if cfg is None or role is None:
        await asyncio.to_thread(_clear)
        log.info(
            "Grant audit card: config/role gone for guild %s — retired the card.",
            guild_id,
        )
        return
    channel = guild.get_channel(ref.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    role_id, role_name = role.id, role.name

    def _gather():
        with open_db(db_path) as conn:
            return gather_grant_audit(
                conn, guild_id, role_id, ref.min_level, role_name
            )

    gathered = await asyncio.to_thread(_gather)
    snap = resolve_grant_audit_buckets(guild, role, gathered, ref.min_level, now)
    accent = await resolve_accent_color(db_path, guild)
    embed = build_grant_audit_embed(str(cfg["label"]), snap, now_ts=now, color=accent)
    try:
        message = await channel.fetch_message(ref.message_id)
        await message.edit(embed=embed)
    except discord.NotFound:
        await asyncio.to_thread(_clear)
        log.info(
            "Grant audit card: message gone in guild %s — retired the card.",
            guild_id,
        )
    except discord.HTTPException:
        log.warning("Grant audit card: refresh failed for guild %s.", guild_id)


async def grant_audit_card_loop(bot: discord.Client, db_path: Path) -> None:
    """Hourly in-place refresh of every posted grant-audit card."""

    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild_ids = await asyncio.to_thread(
                lambda: guilds_with_card_from_path(db_path)
            )
            for guild_id in guild_ids:
                await refresh_grant_audit_card(bot, db_path, guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Grant audit card: unhandled error in refresh loop.")
        await asyncio.sleep(3600)


def guilds_with_card_from_path(db_path: Path) -> list[int]:
    from bot_modules.core.db_utils import open_db

    with open_db(db_path) as conn:
        return guilds_with_card(conn)
