"""Usage telemetry — record and report slash-command and dashboard-panel usage.

Two event kinds share one table (see migration 139): ``command`` rows written by
``usage_telemetry_cog`` from the global app-command listeners, and ``panel_view``
rows written by ``POST /api/telemetry/panel`` when the dashboard mounts a panel.

The reporting functions here are the whole point of the feature. The headline
one is :func:`unused_names` — a pure set difference that answers "which of the
70 slash commands has nobody ever run", which is the only number in this feature
that tells you to *delete* something rather than just admire a chart.

Bot exclusion runs through ``core.bot_exclusion`` like every other dashboard
metric. In practice it should never remove a row (bots cannot invoke slash
commands, and panel views require an OAuth login) — it is applied so this table
cannot become the one place the convention silently doesn't hold.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import NamedTuple

from discord import app_commands

from bot_modules.core.bot_exclusion import bot_filter_clause

KIND_COMMAND = "command"
KIND_PANEL = "panel_view"

# Guards a malformed/hostile panel id before it reaches the DB. Panel ids are
# the dashboard's own kebab-case route ids ("economy-stats"); command names are
# Discord-validated and space-joined for subcommands ("quest board").
_MAX_NAME_LEN = 100


class NameUsage(NamedTuple):
    """One command or panel, aggregated over the report window."""

    name: str
    uses: int
    users: int  # distinct
    errors: int  # rows with ok=0; always 0 for panel views
    last_ts: float


class UserUsage(NamedTuple):
    """One person's activity over the report window."""

    user_id: int
    uses: int
    distinct_names: int
    last_ts: float


class DayPoint(NamedTuple):
    day: str  # 'YYYY-MM-DD' in the guild's local offset
    # Named `total`, not `count` — a NamedTuple field called `count` would
    # shadow tuple.count().
    total: int


# ── Write path ───────────────────────────────────────────────────────────


def record_event(
    conn: sqlite3.Connection,
    guild_id: int,
    kind: str,
    name: str,
    user_id: int,
    *,
    channel_id: int | None = None,
    ok: bool = True,
    extra: dict | None = None,
    ts: float | None = None,
) -> None:
    """Insert one usage row.

    Never raises on a bad ``name`` — telemetry must not be able to break the
    command it is measuring, so an empty name is dropped and an over-long one is
    truncated rather than propagating an error up into the interaction handler.
    """
    name = (name or "").strip()[:_MAX_NAME_LEN]
    if not name:
        return
    conn.execute(
        """
        INSERT INTO usage_events
            (guild_id, kind, name, user_id, channel_id, ok, extra, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            kind,
            name,
            user_id,
            channel_id,
            1 if ok else 0,
            json.dumps(extra or {}),
            ts if ts is not None else time.time(),
        ),
    )


# ── Read path ────────────────────────────────────────────────────────────


def _window(days: int) -> float:
    """Unix cutoff for a report window. ``days <= 0`` means all of history."""
    return 0.0 if days <= 0 else time.time() - days * 86400


def _scope(
    guild_id: int,
    kind: str | None,
    days: int,
    include_bots: bool = False,
) -> tuple[str, tuple]:
    """The ``WHERE`` clause and params every windowed report query shares.

    Exists because ``bot_filter_clause`` returns a fragment that lands *inside*
    the WHERE, so its params have to be spliced at the matching position — get
    the order wrong and the placeholders silently mis-bind rather than error.
    Building the clause and its params together in one place is the only way
    that stays correct; five hand-rolled copies were five chances to get it
    wrong.
    """
    bot_sql, bot_params = bot_filter_clause(
        guild_id, column="user_id", include_bots=include_bots
    )
    kind_sql, kind_params = (" AND kind = ?", (kind,)) if kind else ("", ())
    return (
        f"WHERE guild_id = ? AND ts >= ?{kind_sql}{bot_sql}",
        (guild_id, _window(days), *kind_params, *bot_params),
    )


def name_usage(
    conn: sqlite3.Connection,
    guild_id: int,
    kind: str,
    *,
    days: int = 30,
    include_bots: bool = False,
    limit: int = 200,
) -> list[NameUsage]:
    """Per-name totals for one event kind, busiest first."""
    where, params = _scope(guild_id, kind, days, include_bots)
    rows = conn.execute(
        f"""
        SELECT name,
               COUNT(*)                      AS uses,
               COUNT(DISTINCT user_id)       AS users,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors,
               MAX(ts)                       AS last_ts
        FROM usage_events
        {where}
        GROUP BY name
        ORDER BY uses DESC, name ASC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [
        NameUsage(
            name=str(r[0]),
            uses=int(r[1]),
            users=int(r[2]),
            errors=int(r[3] or 0),
            last_ts=float(r[4]),
        )
        for r in rows
    ]


def used_names(conn: sqlite3.Connection, guild_id: int, kind: str) -> set[str]:
    """Every name ever recorded for this kind.

    Deliberately unwindowed: this feeds the never-used lists, and a command
    last run 90 days ago is unpopular, not unused. Bots are not excluded
    either — a command a bot somehow ran still *was* run.
    """
    rows = conn.execute(
        "SELECT DISTINCT name FROM usage_events WHERE guild_id = ? AND kind = ?",
        (guild_id, kind),
    ).fetchall()
    return {str(r[0]) for r in rows}


def registered_command_names(tree) -> set[str]:
    """Every *invocable* slash command on a discord.py ``CommandTree``.

    Walks the tree so subcommands come back space-joined as ``"quest board"``,
    matching what the cog records.

    ``walk_commands()`` yields Groups as well as Commands, and a Group
    (``/quest`` on its own) can never be invoked — including them would park
    every command group permanently in the never-run list, which is exactly the
    list that has to stay trustworthy. So Groups are filtered out.

    Returns an empty set for ``tree is None`` (standalone dashboard, no bot
    attached), which makes the never-used list empty rather than claiming every
    command is unused.
    """
    if tree is None:
        return set()
    try:
        return {
            c.qualified_name
            for c in tree.walk_commands()
            if isinstance(c, app_commands.Command)
        }
    except Exception:
        return set()


def unused_names(registered: set[str], seen: set[str]) -> list[str]:
    """Registered names that never appear in the telemetry, sorted.

    Pure so it can be tested without a DB, and so the caller decides what
    "registered" means — the live command tree for commands, the dashboard's
    nav manifest for panels.

    NOTE the asymmetry: a name in ``seen`` but not ``registered`` (a command
    since renamed or deleted) is deliberately *not* reported here. This list
    answers "what can I delete", and stale history is not a deletion candidate.
    """
    return sorted(registered - seen)


def user_usage(
    conn: sqlite3.Connection,
    guild_id: int,
    kind: str | None = None,
    *,
    days: int = 30,
    include_bots: bool = False,
    limit: int = 100,
) -> list[UserUsage]:
    """Per-person totals, busiest first. ``kind=None`` counts both kinds."""
    where, params = _scope(guild_id, kind, days, include_bots)
    rows = conn.execute(
        f"""
        SELECT user_id,
               COUNT(*)                AS uses,
               COUNT(DISTINCT name)    AS distinct_names,
               MAX(ts)                 AS last_ts
        FROM usage_events
        {where}
        GROUP BY user_id
        ORDER BY uses DESC, user_id ASC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [
        UserUsage(
            user_id=int(r[0]),
            uses=int(r[1]),
            distinct_names=int(r[2]),
            last_ts=float(r[3]),
        )
        for r in rows
    ]


def daily_series(
    conn: sqlite3.Connection,
    guild_id: int,
    kind: str | None = None,
    *,
    days: int = 30,
    tz_offset_hours: float = 0.0,
    include_bots: bool = False,
) -> list[DayPoint]:
    """Per-day counts across the window, oldest first, gaps filled with zeros.

    Bucketing applies ``tz_offset_hours`` before splitting the date so "today"
    matches the server's local day, the same convention the other time-bucketed
    reports use.
    """
    where, params = _scope(guild_id, kind, days, include_bots)
    shift = tz_offset_hours * 3600
    rows = conn.execute(
        f"""
        SELECT date(ts + ?, 'unixepoch') AS day, COUNT(*)
        FROM usage_events
        {where}
        GROUP BY day
        ORDER BY day ASC
        """,
        (shift, *params),
    ).fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}

    if days <= 0:
        return [DayPoint(day=d, total=counts[d]) for d in sorted(counts)]

    # Fill the window so a quiet day reads as a zero on the chart rather than
    # vanishing and making the line lie about its own x-axis.
    now = time.time() + shift
    out: list[DayPoint] = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
        out.append(DayPoint(day=day, total=counts.get(day, 0)))
    return out


def hour_histogram(
    conn: sqlite3.Connection,
    guild_id: int,
    kind: str | None = None,
    *,
    days: int = 30,
    tz_offset_hours: float = 0.0,
    include_bots: bool = False,
) -> list[int]:
    """24 counts, index = local hour of day."""
    where, params = _scope(guild_id, kind, days, include_bots)
    rows = conn.execute(
        f"""
        SELECT CAST(strftime('%H', ts + ?, 'unixepoch') AS INTEGER) AS hr,
               COUNT(*)
        FROM usage_events
        {where}
        GROUP BY hr
        """,
        (tz_offset_hours * 3600, *params),
    ).fetchall()
    out = [0] * 24
    for r in rows:
        hr = int(r[0])
        if 0 <= hr < 24:
            out[hr] = int(r[1])
    return out


def totals(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    days: int = 30,
    include_bots: bool = False,
) -> dict[str, int]:
    """Headline counters for the report's stat tiles."""
    where, params = _scope(guild_id, None, days, include_bots)
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN kind = ? THEN 1 ELSE 0 END),
          SUM(CASE WHEN kind = ? THEN 1 ELSE 0 END),
          SUM(CASE WHEN kind = ? AND ok = 0 THEN 1 ELSE 0 END),
          COUNT(DISTINCT user_id)
        FROM usage_events
        {where}
        """,
        (KIND_COMMAND, KIND_PANEL, KIND_COMMAND, *params),
    ).fetchone()
    return {
        "commands": int(row[0] or 0),
        "panel_views": int(row[1] or 0),
        "command_errors": int(row[2] or 0),
        "distinct_users": int(row[3] or 0),
    }
