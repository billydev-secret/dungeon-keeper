"""Ping Response — did anyone turn up when a role got pinged?

A role ping (``@Gamers``, ``@everyone``, the bot's own game-start announcement)
leaves no trace anywhere in the schema: ``message_mentions`` records *user*
mentions only, so a role ping was, until this module, entirely invisible.
``ping_events`` fixes that, and this module is everything that reads or writes
it.

**The one design decision worth knowing.** ``ping_events`` records only that a
ping happened. Turnout is *not* stored — it is computed at read time against
``messages`` and ``reaction_log``, both of which are retained anyway and both
of which are already indexed for exactly this shape of scan
(``idx_messages_guild_channel_ts``, ``reaction_log``'s primary key). Two
consequences, both deliberate:

  * the response window is a live control on the report rather than a constant
    baked in by whatever sweep filled the column, and
  * backfilled pings and pings captured live go through *the same* counting
    code, so history and the present are never measured two different ways.

The cost is a join per report rather than a lookup, which the report cache
absorbs.

**What counts as turning up.** Distinct people who, within the window, either
posted in the channel or reacted to the ping message — deduped across the two,
so someone who does both counts once and someone who posts ten times counts
once. The pinger is never their own responder, and bots are excluded by
default like every other dashboard metric. Raw message volume is kept
alongside as a secondary number, because "one person said forty things" and
"forty people said one thing" are different nights.

**Game pings get a better answer.** When the scheduled-game launcher stamps a
row with ``source='game_start'`` and its ``game_id`` in ``ref``, the report can
report the roster — how many people actually *played* — instead of only how
many talked. That is the honest measure of whether a game ping worked, and it
is why ``ref`` exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from bot_modules.core.bot_exclusion import bot_filter_clause

# ── Windows ───────────────────────────────────────────────────────────────
# 30 minutes is the default because it is the shape of the pings that prompted
# this: a game starting now, a nudge on a stalled round. An announcement's
# useful window is much longer, which is exactly why the panel exposes it
# rather than hard-coding one number for every kind of ping.
DEFAULT_WINDOW_MINUTES = 30
MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 24 * 60

# ── Sources ───────────────────────────────────────────────────────────────
# Who sent the ping. The three-way member/self/external split is not cosmetic:
# measured on 60 days of prod, 71% of all role pings came from two unrelated
# third-party bots. Without a way to separate them, the pings anyone actually
# wants to reason about — a mod's own, and this bot's game announcements —
# are a rounding error in their own report.
SOURCE_MEMBER = "member"
SOURCE_BOT = "bot"        # Dungeon Keeper itself
SOURCE_EXTERNAL = "external"  # some other bot in the server
# Stamped afterwards by a sender that knows what it was asking for.
SOURCE_GAME_START = "game_start"

ALL_SOURCES = (SOURCE_MEMBER, SOURCE_BOT, SOURCE_EXTERNAL, SOURCE_GAME_START)

# Named groupings the report offers, so the panel and the API agree on what
# "sent by Dungeon Keeper" means without the frontend hardcoding source strings.
SOURCE_FILTERS: dict[str, tuple[str, ...]] = {
    "all": ALL_SOURCES,
    "self": (SOURCE_BOT, SOURCE_GAME_START),
    "member": (SOURCE_MEMBER,),
    "external": (SOURCE_EXTERNAL,),
}

# The sources a caller is allowed to stamp. Anything else is a typo, and a
# typo'd source silently splits the report's breakdown in two.
STAMPABLE_SOURCES = frozenset({SOURCE_GAME_START})


def resolve_sources(name: str | None) -> tuple[str, ...]:
    """Turn a panel filter name into the source values it selects.

    An unknown name reads as "all" rather than raising: this arrives from a
    query string, and showing everything is a safer failure than a 500 or an
    empty report that looks like an answer.
    """
    return SOURCE_FILTERS.get(name or "all", ALL_SOURCES)


def ingest_source(*, is_bot: bool, is_self: bool) -> str:
    """What the ingest path can tell about a ping's sender on its own."""
    if not is_bot:
        return SOURCE_MEMBER
    return SOURCE_BOT if is_self else SOURCE_EXTERNAL

_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
# @everyone and @here both ping the room; the report treats them alike, since
# "the loudest possible ping" is the category that matters here.
_EVERYONE_RE = re.compile(r"@everyone|@here")


# ── Pure: what is a ping ──────────────────────────────────────────────────


def parse_role_mentions(content: str | None) -> tuple[list[int], bool]:
    """Pull role pings out of raw message text — the **backfill** path only.

    Returns ``(role_ids, mentions_everyone)`` with ids de-duplicated and in
    first-appearance order.

    Live capture must use :func:`role_pings_from_message` instead. Text is a
    strictly worse source: it is absent entirely in guilds that don't retain
    content, and it cannot tell a real ping from someone typing "@everyone"
    without the permission to actually ping anyone. This exists because the
    historical rows are all we have for the past, not because it is a good
    way to learn about the present.
    """
    if not content:
        return [], False
    seen: dict[int, None] = {}
    for raw in _ROLE_MENTION_RE.findall(content):
        try:
            seen.setdefault(int(raw), None)
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
    return list(seen), bool(_EVERYONE_RE.search(content))


def role_pings_from_message(message: Any) -> tuple[list[int], bool]:
    """Pull role pings off a live ``discord.Message`` — the **ingest** path.

    Reads Discord's own structured ``role_mentions`` / ``mention_everyone``
    rather than the text, which is what lets ping tracking work at storage
    level "none" where there is no text to read. It is also more truthful:
    Discord only populates these when the ping actually fired, so a member
    typing ``@everyone`` without the permission to use it is correctly not
    recorded as having pinged the server.
    """
    role_ids: list[int] = []
    seen: set[int] = set()
    for role in getattr(message, "role_mentions", None) or ():
        rid = getattr(role, "id", None)
        if isinstance(rid, int) and rid not in seen:
            seen.add(rid)
            role_ids.append(rid)
    return role_ids, bool(getattr(message, "mention_everyone", False))


def is_ping(role_ids: Sequence[int], everyone: bool) -> bool:
    """True when this message pinged a room, i.e. is worth a ``ping_events`` row."""
    return bool(role_ids) or bool(everyone)


def clamp_window_minutes(minutes: int | None) -> int:
    """Coerce a caller-supplied window into the supported range.

    Clamps rather than rejects: the window arrives from a panel control, and a
    report that 404s because someone dragged a slider too far is worse than one
    that quietly shows the widest window it can measure.
    """
    if minutes is None:
        return DEFAULT_WINDOW_MINUTES
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_MINUTES
    return max(MIN_WINDOW_MINUTES, min(MAX_WINDOW_MINUTES, value))


# ── Writes ────────────────────────────────────────────────────────────────


def record_ping_event(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    guild_id: int,
    channel_id: int,
    author_id: int,
    role_ids: Sequence[int],
    everyone: bool,
    source: str,
    ts: float,
    ref: str | None = None,
) -> bool:
    """Record that this message pinged a room. Returns True when a row landed.

    ``INSERT OR IGNORE`` keyed on ``message_id``: the ingest path and the
    backfill both write here, and a message that has been edited fires
    ``on_message`` again — none of which should produce a second ping. A
    no-ping message is a silent no-op so callers can pass everything through
    without pre-filtering.
    """
    if not is_ping(role_ids, everyone):
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO ping_events
            (message_id, guild_id, channel_id, author_id, role_ids, everyone,
             source, ref, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(guild_id),
            int(channel_id),
            int(author_id),
            json.dumps([int(r) for r in role_ids]),
            1 if everyone else 0,
            source,
            ref,
            float(ts),
        ),
    )
    return cur.rowcount > 0


def stamp_ping_source(
    conn: sqlite3.Connection,
    message_id: int,
    source: str,
    ref: str | None = None,
) -> bool:
    """Upgrade a recorded ping from the generic 'bot' to what it actually was.

    The ingest path sees a message and can only tell that a bot sent it; the
    launcher that sent it knows it was a game start and which game. This is how
    the second fact reaches the row, and it is why a game ping can be reported
    against its roster.

    Ordering is not guaranteed — the sender may call this before ``on_message``
    has stored anything — so a miss is normal and returns False rather than
    raising. The row keeps its generic source and the report still counts it,
    just without the roster column.
    """
    if source not in STAMPABLE_SOURCES:
        raise ValueError(f"refusing to stamp unknown ping source {source!r}")
    cur = conn.execute(
        "UPDATE ping_events SET source = ?, ref = COALESCE(?, ref) WHERE message_id = ?",
        (source, ref, int(message_id)),
    )
    return cur.rowcount > 0


def record_game_start_ping(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    guild_id: int,
    channel_id: int,
    author_id: int,
    role_ids: Sequence[int],
    game_id: str,
    ts: float,
) -> None:
    """Record the launcher's own "it's starting" ping, tied to its game.

    Write-then-stamp, because the launcher and the ``on_message`` ingest path
    both reach this row and either can arrive first. Whichever wins, the row
    ends up with ``game_start`` and the ``game_id``: if this call inserts, the
    later generic insert is ignored on the primary key; if ingest inserted
    first, the insert here no-ops and the stamp upgrades it in place.

    Without both halves the outcome depends on a thread race, and losing it
    costs the report the only column that says whether anyone actually
    *played* rather than merely talked.
    """
    inserted = record_ping_event(
        conn,
        message_id=message_id,
        guild_id=guild_id,
        channel_id=channel_id,
        author_id=author_id,
        role_ids=role_ids,
        everyone=False,
        source=SOURCE_GAME_START,
        ref=game_id,
        ts=ts,
    )
    if not inserted and is_ping(role_ids, False):
        stamp_ping_source(conn, message_id, SOURCE_GAME_START, game_id)


# ── Reads ─────────────────────────────────────────────────────────────────


def query_pings(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    since_ts: float = 0.0,
    sources: Sequence[str] = ALL_SOURCES,
) -> list[dict[str, Any]]:
    """The pings themselves, newest first. Turnout is added by the caller."""
    sources = tuple(sources) or ALL_SOURCES
    ph = ",".join("?" * len(sources))
    rows = conn.execute(
        f"""
        SELECT message_id, channel_id, author_id, role_ids, everyone, source, ref, ts
        FROM ping_events
        WHERE guild_id = ? AND ts >= ? AND source IN ({ph})
        ORDER BY ts DESC
        """,
        (guild_id, since_ts, *sources),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            role_ids = [int(x) for x in json.loads(r[3] or "[]")]
        except (json.JSONDecodeError, TypeError, ValueError):
            # A malformed role list costs this ping its by-role line, not the
            # whole report.
            role_ids = []
        out.append(
            {
                "message_id": int(r[0]),
                "channel_id": int(r[1]),
                "author_id": int(r[2]),
                "role_ids": role_ids,
                "everyone": bool(r[4]),
                "source": str(r[5]),
                "ref": r[6],
                "ts": float(r[7]),
            }
        )
    return out


def query_responders(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    since_ts: float,
    window_minutes: int,
    include_bots: bool = False,
    sources: Sequence[str] = ALL_SOURCES,
) -> tuple[dict[int, dict[int, int]], dict[int, set[int]]]:
    """Who answered each ping, as ``(posters, reactors)``.

    ``posters`` maps ping message_id → {responder_id: how many messages they
    posted}; ``reactors`` maps ping message_id → the set of people who reacted
    to the ping itself.

    Two joined queries rather than a correlated subquery per ping: SQLite has
    no LATERAL, so a per-ping ``COUNT(*) FROM (… UNION …)`` cannot reference
    the outer row at all. Joining and deduping in Python also keeps the union —
    "posted **or** reacted" — in one readable place instead of spread across
    three subqueries that must be kept in step.
    """
    window_seconds = clamp_window_minutes(window_minutes) * 60
    sources = tuple(sources) or ALL_SOURCES
    src_ph = ",".join("?" * len(sources))

    msg_clause, msg_params = bot_filter_clause(
        guild_id, column="m.author_id", include_bots=include_bots
    )
    post_rows = conn.execute(
        f"""
        SELECT p.message_id, m.author_id, COUNT(*)
        FROM ping_events p
        JOIN messages m
          ON m.guild_id = p.guild_id
         AND m.channel_id = p.channel_id
         AND m.ts > p.ts
         AND m.ts <= p.ts + ?
        WHERE p.guild_id = ?
          AND p.ts >= ?
          AND p.source IN ({src_ph})
          AND m.message_id <> p.message_id
          AND m.author_id <> p.author_id{msg_clause}
        GROUP BY p.message_id, m.author_id
        """,
        (window_seconds, guild_id, since_ts, *sources, *msg_params),
    ).fetchall()

    react_clause, react_params = bot_filter_clause(
        guild_id, column="r.reactor_id", include_bots=include_bots
    )
    react_rows = conn.execute(
        f"""
        SELECT p.message_id, r.reactor_id
        FROM ping_events p
        JOIN reaction_log r
          ON r.guild_id = p.guild_id
         AND r.message_id = p.message_id
         AND r.ts <= p.ts + ?
        WHERE p.guild_id = ?
          AND p.ts >= ?
          AND p.source IN ({src_ph})
          AND r.reactor_id <> p.author_id{react_clause}
        GROUP BY p.message_id, r.reactor_id
        """,
        (window_seconds, guild_id, since_ts, *sources, *react_params),
    ).fetchall()

    posters: dict[int, dict[int, int]] = defaultdict(dict)
    for ping_id, uid, n in post_rows:
        posters[int(ping_id)][int(uid)] = int(n)

    reactors: dict[int, set[int]] = defaultdict(set)
    for ping_id, uid in react_rows:
        reactors[int(ping_id)].add(int(uid))

    return dict(posters), dict(reactors)


def query_game_player_counts(
    conn: sqlite3.Connection,
    refs: Iterable[str],
) -> dict[str, int]:
    """Roster sizes for the games named by ``game_start`` pings.

    Finished games carry their final ``player_count`` in ``games_game_history``;
    a game still in progress has no history row yet, so its roster is counted
    off the live lobby's payload instead. A ping for a game that left no trace
    of either (an in-memory game like risky_roll) is simply absent from the
    result and reports no roster rather than a misleading zero.
    """
    wanted = [str(r) for r in refs if r]
    if not wanted:
        return {}
    out: dict[str, int] = {}
    # Chunked: a long-window report can name more games than SQLite's default
    # 999-variable limit allows in one IN list.
    for start in range(0, len(wanted), 400):
        chunk = wanted[start : start + 400]
        ph = ",".join("?" * len(chunk))
        for game_id, count in conn.execute(
            f"""
            SELECT game_id, MAX(player_count) FROM games_game_history
            WHERE game_id IN ({ph})
            GROUP BY game_id
            """,
            chunk,
        ).fetchall():
            out[str(game_id)] = int(count or 0)

        missing = [g for g in chunk if g not in out]
        if not missing:
            continue
        ph = ",".join("?" * len(missing))
        for game_id, payload in conn.execute(
            f"SELECT game_id, payload FROM games_active_games WHERE game_id IN ({ph})",
            missing,
        ).fetchall():
            roster = _roster_size(payload)
            if roster is not None:
                out[str(game_id)] = roster
    return out


def _roster_size(payload: str | None) -> int | None:
    """Distinct player count from a live lobby payload, or None if unreadable.

    The six lobby games keep their rosters under different keys, so every
    plausible one is tried. A payload with no recognisable roster returns None
    — "we don't know" — which the report renders as blank rather than zero.
    """
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("players", "participants", "joined", "entrants", "member_ids"):
        value = data.get(key)
        if isinstance(value, dict):
            return len(value)
        if isinstance(value, list):
            ids = {
                item.get("user_id") if isinstance(item, dict) else item
                for item in value
            }
            ids.discard(None)
            return len(ids)
    return None


# ── Pure: the report ──────────────────────────────────────────────────────


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def build_ping_report(
    pings: Sequence[Mapping[str, Any]],
    posters: Mapping[int, Mapping[int, int]],
    reactors: Mapping[int, Iterable[int]],
    *,
    window_minutes: int,
    window_label: str,
    role_names: Mapping[int, str] | None = None,
    channel_names: Mapping[int, str] | None = None,
    game_players: Mapping[str, int] | None = None,
    tz_offset_hours: float = 0.0,
    recent_limit: int = 100,
) -> dict[str, Any]:
    """Assemble the panel payload. Pure — no DB, no Discord, fully testable.

    Turnout per ping is ``|posters ∪ reactors|``: distinct people, deduped
    across the two ways of answering.

    A ping naming several roles is counted once in the totals but appears under
    **each** role in the by-role breakdown, because the question that breakdown
    answers is "does pinging this role bring anyone", and both roles were
    pinged. So the by-role ping counts can legitimately sum to more than the
    headline total.
    """
    role_names = role_names or {}
    channel_names = channel_names or {}
    game_players = game_players or {}

    entries: list[dict[str, Any]] = []
    per_role: dict[int, list[int]] = defaultdict(list)
    per_channel: dict[int, list[int]] = defaultdict(list)
    per_day: dict[str, list[int]] = defaultdict(list)

    for ping in pings:
        ping_id = int(ping["message_id"])
        posted = dict(posters.get(ping_id, {}))
        reacted = set(reactors.get(ping_id, ()) or ())
        turnout = len(set(posted) | reacted)
        msgs = sum(posted.values())

        ref = ping.get("ref")
        players = game_players.get(str(ref)) if ref else None

        role_ids = [int(r) for r in ping.get("role_ids", [])]
        labels = [role_names.get(rid, f"Role {rid}") for rid in role_ids]
        if ping.get("everyone"):
            labels.append("@everyone")

        entries.append(
            {
                "message_id": str(ping_id),
                "channel_id": str(ping["channel_id"]),
                "channel_name": channel_names.get(int(ping["channel_id"]), ""),
                "author_id": str(ping["author_id"]),
                "role_ids": [str(r) for r in role_ids],
                "role_labels": labels,
                "everyone": bool(ping.get("everyone")),
                "source": ping.get("source", SOURCE_MEMBER),
                "ref": ref,
                "ts": float(ping["ts"]),
                "turnout": turnout,
                "messages": msgs,
                "reactors": len(reacted),
                "players": players,
            }
        )

        for rid in role_ids:
            per_role[rid].append(turnout)
        if ping.get("everyone"):
            # 0 is not a real role id, so it can stand in for @everyone in the
            # breakdown without colliding with one.
            per_role[0].append(turnout)
        per_channel[int(ping["channel_id"])].append(turnout)
        per_day[_local_day(float(ping["ts"]), tz_offset_hours)].append(turnout)

    turnouts = [e["turnout"] for e in entries]
    silent = sum(1 for t in turnouts if t == 0)

    return {
        "window_label": window_label,
        "window_minutes": window_minutes,
        "total_pings": len(entries),
        "total_turnout": sum(turnouts),
        "median_turnout": round(statistics.median(turnouts), 1) if turnouts else 0.0,
        "mean_turnout": round(statistics.fmean(turnouts), 2) if turnouts else 0.0,
        "silent_pings": silent,
        "silent_pct": _pct(silent, len(entries)),
        "series": [
            {
                "day": day,
                "pings": len(values),
                "mean_turnout": round(statistics.fmean(values), 2),
            }
            for day, values in sorted(per_day.items())
        ],
        "by_role": _breakdown(
            per_role,
            lambda rid: "@everyone" if rid == 0 else role_names.get(rid, f"Role {rid}"),
        ),
        "by_channel": _breakdown(
            per_channel,
            lambda cid: channel_names.get(cid, f"#{cid}"),
        ),
        "entries": entries[:recent_limit],
    }


def _breakdown(buckets: Mapping[int, Sequence[int]], label) -> list[dict[str, Any]]:
    """One row per role/channel, worst turnout last — the interesting end first."""
    rows = [
        {
            "id": str(key),
            "label": label(key),
            "pings": len(values),
            "mean_turnout": round(statistics.fmean(values), 2),
            "median_turnout": round(statistics.median(values), 1),
            "silent_pings": sum(1 for v in values if v == 0),
            "silent_pct": _pct(sum(1 for v in values if v == 0), len(values)),
        }
        for key, values in buckets.items()
        if values
    ]
    rows.sort(key=lambda r: (-r["mean_turnout"], -r["pings"]))
    return rows


def _local_day(ts: float, tz_offset_hours: float) -> str:
    """Guild-local YYYY-MM-DD, matching the fixed-offset convention used
    everywhere else in the bot (no DST — see ``db_utils.get_tz_offset_hours``)."""
    return datetime.fromtimestamp(
        ts + tz_offset_hours * 3600, tz=timezone.utc
    ).strftime("%Y-%m-%d")


# ── Backfill ──────────────────────────────────────────────────────────────


def backfill_ping_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    since_ts: float = 0.0,
    bot_ids: Sequence[int] = (),
    self_id: int = 0,
) -> dict[str, int]:
    """Recover historical pings by parsing stored message text.

    Returns ``{"scanned": n, "recorded": n}``. Idempotent — re-running adds
    nothing, because ``record_ping_event`` is keyed on the message id.

    **This sees less than the live path does**, and the panel says so: it can
    only find pings in channels where message content was retained, and it
    cannot distinguish a real ``@everyone`` from someone typing the words
    without permission to ping. It exists so the report opens with history
    instead of an empty chart, not as a substitute for capture.
    """
    bots = {int(b) for b in bot_ids}
    rows = conn.execute(
        """
        SELECT message_id, channel_id, author_id, content, ts
        FROM messages
        WHERE guild_id = ?
          AND ts >= ?
          AND content IS NOT NULL
          AND (content LIKE '%<@&%' OR content LIKE '%@everyone%' OR content LIKE '%@here%')
        """,
        (guild_id, since_ts),
    ).fetchall()

    recorded = 0
    for message_id, channel_id, author_id, content, ts in rows:
        role_ids, everyone = parse_role_mentions(content)
        if not is_ping(role_ids, everyone):
            continue
        if record_ping_event(
            conn,
            message_id=int(message_id),
            guild_id=guild_id,
            channel_id=int(channel_id),
            author_id=int(author_id),
            role_ids=role_ids,
            everyone=everyone,
            source=ingest_source(
                is_bot=int(author_id) in bots,
                is_self=bool(self_id) and int(author_id) == int(self_id),
            ),
            ts=float(ts),
        ):
            recorded += 1
    return {"scanned": len(rows), "recorded": recorded}


def known_bot_ids(conn: sqlite3.Connection, guild_id: int) -> list[int]:
    """Bot authors, for labelling backfilled rows' ``source``."""
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT user_id FROM known_users WHERE guild_id = ? AND is_bot = 1",
            (guild_id,),
        ).fetchall()
    ]
