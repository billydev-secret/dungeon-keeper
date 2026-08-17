"""Survivor season/config/flavor/roster service — stage 1 of docs/plans/survivor.md.

The season's rules all live in one JSON config column (spec §5), read through
:func:`get_config` which merges over :data:`DEFAULT_CONFIG` — so a season
created today keeps its rules even if the defaults move next year, and every
key is guaranteed to have a reader (the staged-config lesson: a config key
whose reader never ships is a silent no-op).

Pure DB logic over a caller-owned connection, no Discord imports — the cog,
routes, and tasks are glue on top of this. Game logic (locks, settle, the
gauntlet) arrives in later stages as ``survivor_logic.py``; this module is the
part the dashboard panel needs to exist.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# Every rule in spec §1 is a setting, not code (spec §3/§5). Values here are
# the season-one decisions of 2026-08-17: free entry, a 10,000 house seed with
# 20% carved off for the Ghost Streak side-pot.
DEFAULT_CONFIG: dict[str, object] = {
    "buyin_coins": 0,
    "pot_seed": 10000,
    "ghost_pot_pct": 20,
    "gauntlet_fee_per_week": 50,
    "strikes": 1,
    "tie_rule": "loss",             # loss | survive
    "late_entry": "gauntlet",       # gauntlet | closed | ghost_only
    "missed_pick": "auto_assign",   # auto_assign | eliminate
    "max_auto_assigns": 3,
    "double_pick_start_week": 14,   # 0 = never
    "double_pick_min_alive": 5,
    "wipeout_annul_through_week": 13,
    "accord_max_alive": 6,
    "ghost_streak": True,
    "slate_hour": 9,                # Wed, guild-local
    "lastcall_hour": 18,            # Sat
    "reckoning_hour": 9,            # Tue
    # Wiring, not rules: where the season lives and which roles the bot
    # manages. 0 = unset; role/channel steps degrade to skipped-and-logged
    # rather than failing the game (spec §3.3).
    "channel_id": 0,
    "role_survivor_id": 0,
    "role_ghost_id": 0,
    "role_sole_survivor_id": 0,
    # The pinned §2.2 announcement, so the entrant counter can be refreshed
    # and the Join button re-pointed after a repost. Set by the post route.
    "announcement_message_id": 0,
}

_ENUM_KEYS: dict[str, tuple[str, ...]] = {
    "tie_rule": ("loss", "survive"),
    "late_entry": ("gauntlet", "closed", "ghost_only"),
    "missed_pick": ("auto_assign", "eliminate"),
}

# Inclusive ranges for the integer dials. Snowflake keys are bounded only
# below — Discord ids don't have a meaningful ceiling worth encoding.
_INT_RANGES: dict[str, tuple[int, int]] = {
    "buyin_coins": (0, 1_000_000),
    "pot_seed": (0, 1_000_000),
    "ghost_pot_pct": (0, 100),
    "gauntlet_fee_per_week": (0, 10_000),
    "strikes": (0, 2),
    "max_auto_assigns": (0, 18),
    "double_pick_start_week": (0, 18),
    "double_pick_min_alive": (2, 1000),
    "wipeout_annul_through_week": (0, 18),
    "accord_max_alive": (2, 1000),
    "slate_hour": (0, 23),
    "lastcall_hour": (0, 23),
    "reckoning_hour": (0, 23),
    "channel_id": (0, 1 << 63),
    "role_survivor_id": (0, 1 << 63),
    "role_ghost_id": (0, 1 << 63),
    "role_sole_survivor_id": (0, 1 << 63),
    "announcement_message_id": (0, 1 << 63),
}

_BOOL_KEYS = frozenset({"ghost_streak"})

FLAVOR_CATEGORIES = ("eulogy", "toll", "no_death", "annul")

SEASON_STATUSES = ("enrolling", "active", "complete")


class SeasonError(ValueError):
    """A season lifecycle rule was violated (message is user-presentable)."""


# ── seasons ────────────────────────────────────────────────────────────


def create_season(
    conn: sqlite3.Connection,
    guild_id: int,
    name: str,
    season_year: int,
    overrides: dict | None = None,
) -> int:
    """Create a season in ``enrolling`` state and return its id.

    One live (non-complete) season per guild (spec §6.12) — a second create
    raises :class:`SeasonError`. ``overrides`` are validated config keys
    stored sparsely; everything else reads through the defaults.
    """
    name = name.strip()
    if not name:
        raise SeasonError("Season name can't be empty.")
    if not 2020 <= season_year <= 2100:
        raise SeasonError("Season year looks wrong.")
    if get_active_season(conn, guild_id) is not None:
        raise SeasonError("This guild already has a live season — end it first.")
    config = validate_config(overrides or {})
    try:
        cur = conn.execute(
            "INSERT INTO survivor_seasons (guild_id, name, season_year, status, config) "
            "VALUES (?, ?, ?, 'enrolling', ?)",
            (guild_id, name, season_year, json.dumps(config)),
        )
    except sqlite3.IntegrityError as exc:
        # The schema backstop (partial unique index on live seasons): two
        # concurrent creates both pass the check above; the loser lands here.
        raise SeasonError(
            "This guild already has a live season — end it first."
        ) from exc
    return int(cur.lastrowid or 0)


def get_season(conn: sqlite3.Connection, season_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, guild_id, name, season_year, status, config "
        "FROM survivor_seasons WHERE id = ?",
        (season_id,),
    ).fetchone()
    return _season_dict(row) if row else None


def get_active_season(conn: sqlite3.Connection, guild_id: int) -> dict | None:
    """The guild's live season — ``enrolling`` or ``active`` — or None."""
    row = conn.execute(
        "SELECT id, guild_id, name, season_year, status, config "
        "FROM survivor_seasons WHERE guild_id = ? AND status != 'complete' "
        "ORDER BY id DESC LIMIT 1",
        (guild_id,),
    ).fetchone()
    return _season_dict(row) if row else None


def list_seasons(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, guild_id, name, season_year, status, config "
        "FROM survivor_seasons WHERE guild_id = ? ORDER BY id DESC",
        (guild_id,),
    ).fetchall()
    return [_season_dict(r) for r in rows]


def set_season_status(conn: sqlite3.Connection, season_id: int, status: str) -> None:
    """Move a season along enrolling → active → complete (no resurrection)."""
    if status not in SEASON_STATUSES:
        raise SeasonError(f"Unknown season status {status!r}.")
    season = get_season(conn, season_id)
    if season is None:
        raise SeasonError("No such season.")
    order = {s: i for i, s in enumerate(SEASON_STATUSES)}
    if order[status] < order[season["status"]]:
        raise SeasonError(
            f"A {season['status']} season can't go back to {status}."
        )
    conn.execute(
        "UPDATE survivor_seasons SET status = ? WHERE id = ?", (status, season_id)
    )


def end_season(conn: sqlite3.Connection, season_id: int) -> None:
    """Archive the season. History stays queryable (spec §6.12)."""
    set_season_status(conn, season_id, "complete")


def _season_dict(row: sqlite3.Row) -> dict:
    try:
        stored = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    return {
        "id": int(row["id"]),
        "guild_id": int(row["guild_id"]),
        "name": row["name"],
        "season_year": int(row["season_year"]),
        "status": row["status"],
        "config": get_config(stored),
    }


# ── config ─────────────────────────────────────────────────────────────


def get_config(stored: dict) -> dict:
    """Merge a season's sparse stored config over the defaults."""
    merged = dict(DEFAULT_CONFIG)
    for key, value in stored.items():
        if key in merged:
            merged[key] = value
    return merged


def validate_config(updates: dict) -> dict:
    """Validate config ``updates``; returns the cleaned dict or raises.

    Unknown keys are rejected outright — every stored key must have a reader,
    or it becomes a silent no-op that outlives its branch.
    """
    cleaned: dict[str, object] = {}
    for key, value in updates.items():
        if key not in DEFAULT_CONFIG:
            raise SeasonError(f"Unknown setting {key!r}.")
        if key in _ENUM_KEYS:
            if value not in _ENUM_KEYS[key]:
                allowed = ", ".join(_ENUM_KEYS[key])
                raise SeasonError(f"{key} must be one of: {allowed}.")
            cleaned[key] = value
        elif key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise SeasonError(f"{key} must be true or false.")
            cleaned[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SeasonError(f"{key} must be a whole number.")
            lo, hi = _INT_RANGES[key]
            if not lo <= value <= hi:
                raise SeasonError(f"{key} must be between {lo} and {hi}.")
            cleaned[key] = value
    return cleaned


def update_config(conn: sqlite3.Connection, season_id: int, updates: dict) -> dict:
    """Apply validated ``updates`` to a season's stored config; returns the
    full merged config after the write."""
    season = get_season(conn, season_id)
    if season is None:
        raise SeasonError("No such season.")
    cleaned = validate_config(updates)
    row = conn.execute(
        "SELECT config FROM survivor_seasons WHERE id = ?", (season_id,)
    ).fetchone()
    try:
        stored = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    stored.update(cleaned)
    conn.execute(
        "UPDATE survivor_seasons SET config = ? WHERE id = ?",
        (json.dumps(stored), season_id),
    )
    return get_config(stored)


# ── roster ─────────────────────────────────────────────────────────────


def list_players(conn: sqlite3.Connection, season_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, status, strikes_used, eliminated_week, joined_at "
        "FROM survivor_players WHERE season_id = ? "
        "ORDER BY status, joined_at",
        (season_id,),
    ).fetchall()
    return [
        {
            "user_id": int(r["user_id"]),
            "status": r["status"],
            "strikes_used": int(r["strikes_used"]),
            "eliminated_week": r["eliminated_week"],
            "joined_at": r["joined_at"],
        }
        for r in rows
    ]


def add_player(
    conn: sqlite3.Connection, season_id: int, user_id: int, *, joined_at: float
) -> bool:
    """Enroll a member; False if they already have an entry (one per person,
    spec §1.1). The full join flow (buy-in, gauntlet) is later-stage logic
    that calls this last."""
    iso = datetime.fromtimestamp(joined_at, timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO survivor_players (season_id, user_id, joined_at) "
        "VALUES (?, ?, ?)",
        (season_id, user_id, iso),
    )
    return (cur.rowcount or 0) > 0


def eliminate_player(
    conn: sqlite3.Connection, season_id: int, user_id: int, week: int
) -> bool:
    """Admin elimination: alive → ghost at ``week``. False if not alive."""
    cur = conn.execute(
        "UPDATE survivor_players SET status = 'ghost', eliminated_week = ? "
        "WHERE season_id = ? AND user_id = ? AND status = 'alive'",
        (week, season_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def revive_player(conn: sqlite3.Connection, season_id: int, user_id: int) -> bool:
    """Admin revival: ghost → alive, elimination week cleared. Strike counts
    are left as they stand — a wrong *result* is corrected through re-settling
    (stage 4), which unwinds the strike too; this only restores life."""
    cur = conn.execute(
        "UPDATE survivor_players SET status = 'alive', eliminated_week = NULL "
        "WHERE season_id = ? AND user_id = ? AND status = 'ghost'",
        (season_id, user_id),
    )
    return (cur.rowcount or 0) > 0


# ── flavor corpus ──────────────────────────────────────────────────────
# The Reckoning's voice (spec §2.5). Lines carry {name}/{team}/{week} slots,
# filled by plain str.replace at render time (stage 5) — never str.format, so
# a brace in a member name can't crash a eulogy.


def list_flavor(
    conn: sqlite3.Connection,
    guild_id: int,
    category: str | None = None,
    *,
    include_inactive: bool = False,
) -> list[dict]:
    if category is not None and category not in FLAVOR_CATEGORIES:
        raise SeasonError(f"Unknown flavor category {category!r}.")
    sql = "SELECT id, category, line, active FROM survivor_flavor WHERE guild_id = ?"
    params: list[object] = [guild_id]
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY category, id"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": int(r["id"]),
            "category": r["category"],
            "line": r["line"],
            "active": bool(r["active"]),
        }
        for r in rows
    ]


def add_flavor(
    conn: sqlite3.Connection, guild_id: int, category: str, line: str
) -> int:
    if category not in FLAVOR_CATEGORIES:
        raise SeasonError(f"Unknown flavor category {category!r}.")
    line = line.strip()
    if not line:
        raise SeasonError("Flavor line can't be empty.")
    cur = conn.execute(
        "INSERT INTO survivor_flavor (guild_id, category, line) VALUES (?, ?, ?)",
        (guild_id, category, line),
    )
    return int(cur.lastrowid or 0)


def update_flavor(
    conn: sqlite3.Connection,
    guild_id: int,
    flavor_id: int,
    *,
    line: str | None = None,
    active: bool | None = None,
) -> bool:
    """Edit a line and/or flip its active flag. Guild-checked so one guild's
    panel can never touch another's corpus."""
    sets: list[str] = []
    params: list[object] = []
    if line is not None:
        line = line.strip()
        if not line:
            raise SeasonError("Flavor line can't be empty.")
        sets.append("line = ?")
        params.append(line)
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if not sets:
        # Distinct from the False no-such-row return: an empty update is a
        # caller mistake, not a missing line, and must not surface as a 404.
        raise SeasonError("Nothing to update.")
    params += [flavor_id, guild_id]
    cur = conn.execute(
        f"UPDATE survivor_flavor SET {', '.join(sets)} WHERE id = ? AND guild_id = ?",
        params,
    )
    return (cur.rowcount or 0) > 0


def delete_flavor(conn: sqlite3.Connection, guild_id: int, flavor_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM survivor_flavor WHERE id = ? AND guild_id = ?",
        (flavor_id, guild_id),
    )
    return (cur.rowcount or 0) > 0


def seed_default_flavor(conn: sqlite3.Connection, guild_id: int) -> int:
    """Install the starter corpus for a guild that has none. Idempotent — a
    guild with any rows (even all-retired ones) is left alone, so a curated
    corpus is never diluted by re-seeding. Returns rows inserted."""
    row = conn.execute(
        "SELECT COUNT(*) FROM survivor_flavor WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    if int(row[0]) > 0:
        return 0
    inserted = 0
    for category, lines in DEFAULT_FLAVOR.items():
        for line in lines:
            conn.execute(
                "INSERT INTO survivor_flavor (guild_id, category, line) "
                "VALUES (?, ?, ?)",
                (guild_id, category, line),
            )
            inserted += 1
    return inserted


# ── starter corpus ─────────────────────────────────────────────────────
# Written for the meadow: lowercase, wry, gently macabre. {name} is the
# member, {team} their fatal pick, {week} the week number. Spec asks for
# ~20 eulogies, ~10 toll, ~10 no-death; annulments are rare enough for 4.
# The auto-assign-cap death line ("the groundskeeper stopped covering for X")
# is NOT here — it's the fixed line for that one path, hardcoded at the site.

DEFAULT_FLAVOR: dict[str, tuple[str, ...]] = {
    "eulogy": (
        "pour one out for **{name}**, who believed in the {team} the way children believe in summer. 🥀",
        "**{name}** trusted the {team}. the {team} did not know this, and it would not have helped. 🪦",
        "the meadow opens to receive **{name}**, who died as they lived: confident on a thursday.",
        "**{name}** has left the land of the living. the {team} send their regards.",
        "somewhere a door closes softly. **{name}** walks with the ghosts now.",
        "**{name}** rode the {team} into the sunset. the sunset was a wall. 🧱",
        "light a candle for **{name}**. the {team} already blew it out. 🕯️",
        "**{name}** did the research, checked the injuries, and died anyway. the meadow respects the process.",
        "the {team} were due, said **{name}**, who is now also due. at the graveyard. 🥀",
        "**{name}** is survived by their remaining teams, who will never be picked.",
        "we gather to remember **{name}**, who made it to week {week} and not an inch further.",
        "**{name}** fell in week {week}, betrayed by the {team} and, arguably, by hope itself.",
        "no eulogy could do justice to what the {team} did to **{name}** in front of everyone.",
        "**{name}** believed. that was the whole problem. 🌙",
        "the grass grows a little taller where **{name}** used to stand. 🌾",
        "**{name}** joins the graveyard chorus. they'll know all the words by november.",
        "one strike, two strikes, and the long quiet. goodnight, **{name}**.",
        "**{name}** picked with their heart. the heart is a muscle, not an analyst.",
        "the {team} thank **{name}** for their faith and regret the outcome.",
        "**{name}** walked in like a lion and left like a rumor.",
    ),
    "toll": (
        "week {week} came for the overconfident. the meadow is quieter now.",
        "week {week} took its share and left without apologizing.",
        "the meadow counted heads after week {week} and came up short.",
        "week {week} is settled. the ground is freshly turned. 🍂",
        "another week, another toll. the gate creaks either way.",
        "week {week} has been paid in full. the currency was people.",
        "the ledger closes on week {week}. some names are written smaller now.",
        "week {week} passed through like weather. not everyone had a roof.",
        "the bell rang for week {week}. some of you heard it personally. 🔔",
        "week {week} kept receipts. they are itemized below.",
    ),
    "no_death": (
        "everyone lives. suspicious. the meadow remembers.",
        "no deaths in week {week}. the graveyard waits politely.",
        "the toll came up empty. even the groundskeeper looked surprised.",
        "everyone survived week {week}. somewhere, chalk is smiling.",
        "not one soul lost. the meadow narrows its eyes.",
        "week {week} passed and took nobody. it will not make a habit of this.",
        "all present and accounted for. the scythe stays in the shed. 🌾",
        "zero deaths. the ghosts boo from the fence. 👻",
        "the meadow set the table for grief and nobody came.",
        "everyone lives to sweat another sunday.",
    ),
    "annul": (
        "everyone died. therefore nobody did. the week is stricken — but the teams you burned stay burned.",
        "a total wipeout. the meadow, unable to bury everyone, buries the week instead.",
        "the record will show week {week} never happened. your satchels know better.",
        "all fall down; all get up. the week is annulled and the teams stay ash.",
    ),
}
