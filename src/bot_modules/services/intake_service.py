"""Intake cards — per-newcomer welcome-procedure tracker.

When a member joins, a card posts to greeter chat (see
``docs/plans/intake-cards.md``) with the intake procedure as a checklist. The
card is a passive tracker: welcomers keep greeting, asking the question lists,
and running ``/grant`` exactly as before, and the card watches:

* **auto steps** tick from event hooks — ``greeted`` (a greeter-role member
  mentions the newcomer in the intake channel), ``verified`` (the unverified
  role is removed), ``role_gained`` (the member gains the step's configured
  role, whether via ``/grant`` or a manual add);
* **manual steps** (the SFW/NSFW question phases) are buttons on the card;
* **completion** is the configured code appearing in a greeter/mod message
  that mentions the newcomer — any channel. Unticked steps are stamped
  *skipped*, never blocking.

Cards close only on completion, Dismiss, or the member leaving / being
banned — never by timeout. A background loop nudges a stale card once.

This module owns the durable ledger (``intake_cards`` +
``intake_card_steps``, migration 115), config parsing, and the pure gating
logic; the Discord embed + persistent buttons live in ``intake_views``. The
message hot path uses :func:`is_watched` for an O(1) filter before touching
the DB (same pattern as ``promotion_review_service``).

Ships dark: nothing happens until ``intake_enabled`` is set and a channel
resolves (``intake_channel_id``, falling back to ``greeter_chat_channel_id``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass

from bot_modules.core.db_utils import get_config_value, open_db, parse_bool

ENABLED_KEY = "intake_enabled"
CHANNEL_KEY = "intake_channel_id"
FALLBACK_CHANNEL_KEY = "greeter_chat_channel_id"
GREETER_ROLE_KEY = "greeter_role_id"
STEPS_KEY = "intake_steps"
CODE_KEY = "intake_completion_code"
STALE_HOURS_KEY = "intake_stale_hours"

AUTO_GREETED = "greeted"
AUTO_VERIFIED = "verified"
AUTO_ROLE_GAINED = "role_gained"
AUTO_KINDS = ("", AUTO_GREETED, AUTO_VERIFIED, AUTO_ROLE_GAINED)

RESOLUTION_COMPLETED = "completed"
RESOLUTION_DISMISSED = "dismissed"
RESOLUTION_LEFT = "left"
RESOLUTION_BANNED = "banned"

DEFAULT_STALE_HOURS = 24.0

#: ``done_by`` for steps ticked by event hooks rather than a person.
AUTO_ACTOR = 0


# ---------------------------------------------------------------------------
# Step configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepDef:
    """One configured checklist step (``auto_kind`` '' = manual button)."""

    key: str
    label: str
    auto_kind: str = ""
    auto_role_id: int = 0


# The two role steps default to MANUAL: a role_gained step with no
# configured role id can never auto-tick and gets no button either (auto
# steps are filtered from the view) — an untickable dead step. They become
# role_gained the moment the admin saves the step editor with real roles
# (the dashboard requires a role for role_gained steps).
DEFAULT_STEPS: tuple[StepDef, ...] = (
    StepDef("greeted", "Greeted", AUTO_GREETED),
    StepDef("verified", "Verified", AUTO_VERIFIED),
    StepDef("member_role", "Member role granted"),
    StepDef("sfw_questions", "SFW questions asked"),
    StepDef("nsfw_role", "NSFW access granted"),
    StepDef("nsfw_questions", "NSFW questions asked"),
)


def parse_steps(raw: str) -> list[StepDef]:
    """Parse the ``intake_steps`` JSON config into step definitions.

    Expected shape: ``[{"key": ..., "label": ..., "auto": ..., "role_id": ...}]``
    with ``auto``/``role_id`` optional. Malformed JSON, a non-list, or a list
    that yields no valid entries falls back to :data:`DEFAULT_STEPS`; invalid
    or duplicate-key entries are dropped individually so one bad row doesn't
    nuke the rest of the procedure.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return list(DEFAULT_STEPS)
    if not isinstance(data, list):
        return list(DEFAULT_STEPS)
    steps: list[StepDef] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        label = str(entry.get("label") or "").strip()
        auto = str(entry.get("auto") or "").strip()
        if not key or not label or key in seen or auto not in AUTO_KINDS:
            continue
        try:
            role_id = int(entry.get("role_id") or 0)
        except (TypeError, ValueError):
            role_id = 0
        seen.add(key)
        steps.append(StepDef(key, label, auto, role_id))
    return steps if steps else list(DEFAULT_STEPS)


def step_config(conn: sqlite3.Connection, guild_id: int) -> list[StepDef]:
    return parse_steps(get_config_value(conn, STEPS_KEY, "", guild_id))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _int_config(conn: sqlite3.Connection, key: str, guild_id: int) -> int:
    try:
        return int(get_config_value(conn, key, "0", guild_id))
    except (TypeError, ValueError):
        return 0


def intake_channel_id(conn: sqlite3.Connection, guild_id: int) -> int:
    """The channel cards post to; falls back to the greeter chat channel."""
    explicit = _int_config(conn, CHANNEL_KEY, guild_id)
    if explicit > 0:
        return explicit
    return _int_config(conn, FALLBACK_CHANNEL_KEY, guild_id)


def is_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True once intake is switched on **and** a card channel resolves."""
    if not parse_bool(get_config_value(conn, ENABLED_KEY, "0", guild_id)):
        return False
    return intake_channel_id(conn, guild_id) > 0


def greeter_role_id(conn: sqlite3.Connection, guild_id: int) -> int:
    return _int_config(conn, GREETER_ROLE_KEY, guild_id)


def completion_code(conn: sqlite3.Connection, guild_id: int) -> str:
    """The code phrase that completes a card; empty = code detection off."""
    return str(get_config_value(conn, CODE_KEY, "", guild_id)).strip()


def stale_hours(conn: sqlite3.Connection, guild_id: int) -> float:
    raw = get_config_value(conn, STALE_HOURS_KEY, "", guild_id)
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALE_HOURS
    return hours if hours > 0 else DEFAULT_STALE_HOURS


def code_matches(content: str, code: str) -> bool:
    """Case-insensitive containment; an empty code never matches."""
    return bool(code) and code.lower() in content.lower()


# ---------------------------------------------------------------------------
# Card ledger
# ---------------------------------------------------------------------------


def create_card(
    conn: sqlite3.Connection, guild_id: int, user_id: int, created_at: float
) -> int | None:
    """Open a card and snapshot the configured steps onto it.

    Returns the new card id, or ``None`` if the member already has an open
    card (the partial unique index rejects the insert) — a rejoin while the
    old card is still open keeps that card rather than spawning a second.
    """
    try:
        cursor = conn.execute(
            "INSERT INTO intake_cards "
            "(guild_id, user_id, channel_id, message_id, created_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (guild_id, user_id, created_at),
        )
    except sqlite3.IntegrityError:
        return None
    card_id = int(cursor.lastrowid or 0)
    conn.executemany(
        "INSERT INTO intake_card_steps "
        "(card_id, position, step_key, label, auto_kind, auto_role_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (card_id, pos, s.key, s.label, s.auto_kind, s.auto_role_id)
            for pos, s in enumerate(step_config(conn, guild_id))
        ],
    )
    return card_id


def get_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM intake_cards WHERE id = ?", (card_id,)
    ).fetchone()


def get_open_card(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM intake_cards "
        "WHERE guild_id = ? AND user_id = ? AND resolved_at IS NULL",
        (guild_id, user_id),
    ).fetchone()


def set_card_message(
    conn: sqlite3.Connection, card_id: int, channel_id: int, message_id: int
) -> None:
    """Attach the posted message's location to a freshly created card."""
    conn.execute(
        "UPDATE intake_cards SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel_id, message_id, card_id),
    )


def delete_card(conn: sqlite3.Connection, card_id: int) -> None:
    """Roll back a created card whose post never made it to Discord."""
    conn.execute("DELETE FROM intake_card_steps WHERE card_id = ?", (card_id,))
    conn.execute("DELETE FROM intake_cards WHERE id = ?", (card_id,))


def resolve_card(
    conn: sqlite3.Connection,
    card_id: int,
    resolved_by: int,
    resolved_at: float,
    resolution: str,
) -> int:
    """Close an open card; returns rows updated (0 if already resolved)."""
    cursor = conn.execute(
        "UPDATE intake_cards "
        "SET resolved_at = ?, resolved_by = ?, resolution = ? "
        "WHERE id = ? AND resolved_at IS NULL",
        (resolved_at, resolved_by, resolution, card_id),
    )
    return cursor.rowcount


def close_for_member(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    resolution: str,
    resolved_by: int,
    resolved_at: float,
) -> sqlite3.Row | None:
    """Close a member's open card (leave/ban paths); returns it, or ``None``.

    The returned row is the pre-close snapshot so the caller still has the
    card's message location to edit.
    """
    card = get_open_card(conn, guild_id, user_id)
    if card is None:
        return None
    resolve_card(conn, int(card["id"]), resolved_by, resolved_at, resolution)
    return card


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def steps_for(conn: sqlite3.Connection, card_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM intake_card_steps WHERE card_id = ? ORDER BY position",
        (card_id,),
    ).fetchall()


def set_step_state(
    conn: sqlite3.Connection,
    card_id: int,
    step_key: str,
    *,
    done: bool,
    actor_id: int,
    at: float,
) -> bool:
    """Tick (or untick) one step; returns whether anything changed.

    Ticking only lands on an un-done step and unticking only on a done one,
    so a double-click race can't overwrite who originally ticked it.
    """
    if done:
        cursor = conn.execute(
            "UPDATE intake_card_steps SET done_at = ?, done_by = ? "
            "WHERE card_id = ? AND step_key = ? AND done_at IS NULL",
            (at, actor_id, card_id, step_key),
        )
    else:
        cursor = conn.execute(
            "UPDATE intake_card_steps SET done_at = NULL, done_by = NULL "
            "WHERE card_id = ? AND step_key = ? AND done_at IS NOT NULL",
            (card_id, step_key),
        )
    return cursor.rowcount > 0


def auto_tick(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    kind: str,
    at: float,
    *,
    role_id: int = 0,
    actor_id: int = AUTO_ACTOR,
) -> tuple[sqlite3.Row | None, list[str]]:
    """Tick the open card's un-done auto steps matching ``kind``.

    ``role_gained`` steps additionally match on ``role_id``. Returns the open
    card (``None`` if the member has no card — the common case, callers just
    bail) and the step keys that actually ticked, so the caller knows whether
    the card message needs a re-render.
    """
    card = get_open_card(conn, guild_id, user_id)
    if card is None:
        return None, []
    params: list[object] = [int(card["id"]), kind]
    role_clause = ""
    if kind == AUTO_ROLE_GAINED:
        role_clause = "AND auto_role_id = ? AND auto_role_id > 0 "
        params.append(role_id)
    rows = conn.execute(
        "SELECT step_key FROM intake_card_steps "
        "WHERE card_id = ? AND auto_kind = ? AND done_at IS NULL " + role_clause,
        params,
    ).fetchall()
    ticked = []
    for row in rows:
        key = str(row["step_key"])
        if set_step_state(
            conn, int(card["id"]), key, done=True, actor_id=actor_id, at=at
        ):
            ticked.append(key)
    return card, ticked


def count_progress(steps: list[sqlite3.Row]) -> tuple[int, int]:
    """(done, total) for the progress bar; skipped steps don't count as done."""
    done = sum(1 for s in steps if s["done_at"] is not None and not s["skipped"])
    return done, len(steps)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def complete_card(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    completed_by: int,
    at: float,
) -> tuple[sqlite3.Row, list[str]] | None:
    """Complete a member's open card (the completion code was posted).

    Stamps every still-unticked step as *skipped* (the code always wins —
    shortcuts surface in analytics instead of blocking), closes the card with
    the code's poster as the welcomer of record, and returns the pre-close
    card row plus the skipped step keys. ``None`` if there's no open card.
    """
    card = get_open_card(conn, guild_id, user_id)
    if card is None:
        return None
    card_id = int(card["id"])
    skipped = [
        str(r["step_key"])
        for r in conn.execute(
            "SELECT step_key FROM intake_card_steps "
            "WHERE card_id = ? AND done_at IS NULL ORDER BY position",
            (card_id,),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE intake_card_steps SET skipped = 1 "
        "WHERE card_id = ? AND done_at IS NULL",
        (card_id,),
    )
    resolve_card(conn, card_id, completed_by, at, RESOLUTION_COMPLETED)
    return card, skipped


ACTION_GREET = "greet"
ACTION_COMPLETE = "complete"


def evaluate_message(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_id: int,
    content: str,
    mentioned_ids: list[int],
    author_is_greeter: bool,
    author_is_mod: bool,
) -> list[tuple[str, int]]:
    """What a message means for intake: ``(action, newcomer_id)`` pairs.

    For each mentioned member with an open card:

    * :data:`ACTION_COMPLETE` — the message carries the completion code and
      comes from a greeter or mod; any channel. Wins over a greet (the card
      is closing anyway).
    * :data:`ACTION_GREET` — a greeter-role member mentioned them in the
      intake channel (the same signal the Greeter Response report measures).

    Pure decision logic so the whole matrix is unit-testable; the caller
    supplies the Discord-side facts (roles, mentions) as primitives.
    """
    if not mentioned_ids or not is_enabled(conn, guild_id):
        return []
    completes = (
        (author_is_greeter or author_is_mod)
        and code_matches(content, completion_code(conn, guild_id))
    )
    greets = author_is_greeter and channel_id == intake_channel_id(conn, guild_id)
    if not completes and not greets:
        return []
    actions: list[tuple[str, int]] = []
    for uid in dict.fromkeys(mentioned_ids):  # dedupe, keep order
        if get_open_card(conn, guild_id, uid) is None:
            continue
        actions.append((ACTION_COMPLETE if completes else ACTION_GREET, uid))
    return actions


def inviter_for(conn: sqlite3.Connection, guild_id: int, invitee_id: int) -> int | None:
    """Who invited this member, if invite attribution caught the join."""
    row = conn.execute(
        "SELECT inviter_id FROM invite_edges WHERE guild_id = ? AND invitee_id = ?",
        (guild_id, invitee_id),
    ).fetchone()
    return int(row["inviter_id"]) if row else None


# ---------------------------------------------------------------------------
# Stale-card nudges
# ---------------------------------------------------------------------------


def stale_cards(
    conn: sqlite3.Connection, guild_id: int, now: float
) -> list[sqlite3.Row]:
    """Open, never-nudged cards with no progress for ``intake_stale_hours``.

    "No progress" means no step has ticked within the window — any tick
    resets the clock, so an intake that's moving (however slowly) is never
    nudged, only one that's sitting.
    """
    cutoff = now - stale_hours(conn, guild_id) * 3600.0
    return conn.execute(
        "SELECT c.* FROM intake_cards c "
        "LEFT JOIN intake_card_steps s ON s.card_id = c.id "
        "WHERE c.guild_id = ? AND c.resolved_at IS NULL AND c.nudged_at IS NULL "
        "GROUP BY c.id "
        "HAVING COALESCE(MAX(s.done_at), c.created_at) <= ?",
        (guild_id, cutoff),
    ).fetchall()


def mark_nudged(conn: sqlite3.Connection, card_id: int, at: float) -> None:
    conn.execute(
        "UPDATE intake_cards SET nudged_at = ? WHERE id = ?", (at, card_id)
    )


# ---------------------------------------------------------------------------
# Reports (dashboard analytics)
# ---------------------------------------------------------------------------


def _display_name(conn: sqlite3.Connection, guild_id: int, user_id: int) -> str:
    row = conn.execute(
        "SELECT display_name, username FROM known_users "
        "WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return str(user_id)
    return str(row["display_name"] or row["username"] or user_id)


def report_open_cards(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    """The live queue: every open card with its progress, oldest first."""
    cards = conn.execute(
        "SELECT * FROM intake_cards "
        "WHERE guild_id = ? AND resolved_at IS NULL ORDER BY created_at",
        (guild_id,),
    ).fetchall()
    out = []
    for c in cards:
        steps = steps_for(conn, int(c["id"]))
        done, total = count_progress(steps)
        out.append(
            {
                "user_id": int(c["user_id"]),
                "user_name": _display_name(conn, guild_id, int(c["user_id"])),
                "created_at": float(c["created_at"]),
                "nudged": c["nudged_at"] is not None,
                "done": done,
                "total": total,
                "pending": [
                    str(s["label"]) for s in steps if s["done_at"] is None
                ],
            }
        )
    return out


def report_outcomes(
    conn: sqlite3.Connection, guild_id: int, since_ts: float
) -> dict:
    """Resolution counts + completion-time stats for cards created since."""
    rows = conn.execute(
        "SELECT resolution, resolved_at, created_at FROM intake_cards "
        "WHERE guild_id = ? AND resolved_at IS NOT NULL AND created_at >= ?",
        (guild_id, since_ts),
    ).fetchall()
    counts: dict[str, int] = {}
    durations: list[float] = []
    for r in rows:
        resolution = str(r["resolution"] or "")
        counts[resolution] = counts.get(resolution, 0) + 1
        if resolution == RESOLUTION_COMPLETED:
            durations.append(float(r["resolved_at"]) - float(r["created_at"]))
    durations.sort()
    n = len(durations)
    median = (
        (durations[n // 2] if n % 2 else (durations[n // 2 - 1] + durations[n // 2]) / 2)
        if n
        else 0.0
    )
    return {
        "resolved": len(rows),
        "counts": counts,
        "mean_seconds": (sum(durations) / n) if n else 0.0,
        "median_seconds": median,
    }


def report_welcomers(
    conn: sqlite3.Connection, guild_id: int, since_ts: float
) -> list[dict]:
    """Per-welcomer activity: completions owned + manual steps ticked."""
    stats: dict[int, dict] = {}

    def _bucket(uid: int) -> dict:
        return stats.setdefault(
            uid,
            {
                "user_id": uid,
                "user_name": _display_name(conn, guild_id, uid),
                "completions": 0,
                "ticks": 0,
            },
        )

    for r in conn.execute(
        "SELECT resolved_by, COUNT(*) AS n FROM intake_cards "
        "WHERE guild_id = ? AND resolution = ? AND created_at >= ? "
        "GROUP BY resolved_by",
        (guild_id, RESOLUTION_COMPLETED, since_ts),
    ).fetchall():
        if r["resolved_by"]:
            _bucket(int(r["resolved_by"]))["completions"] = int(r["n"])
    for r in conn.execute(
        "SELECT s.done_by, COUNT(*) AS n FROM intake_card_steps s "
        "JOIN intake_cards c ON c.id = s.card_id "
        "WHERE c.guild_id = ? AND c.created_at >= ? "
        "AND s.done_by IS NOT NULL AND s.done_by > 0 "
        "GROUP BY s.done_by",
        (guild_id, since_ts),
    ).fetchall():
        _bucket(int(r["done_by"]))["ticks"] = int(r["n"])
    return sorted(
        stats.values(), key=lambda w: (-w["completions"], -w["ticks"], w["user_id"])
    )


def report_skipped_steps(
    conn: sqlite3.Connection, guild_id: int, since_ts: float
) -> list[dict]:
    """How often each step was skipped on completed cards — the procedure's
    own feedback about which parts the team doesn't actually run."""
    rows = conn.execute(
        "SELECT s.step_key, s.label, COUNT(*) AS appeared, "
        "SUM(s.skipped) AS skipped FROM intake_card_steps s "
        "JOIN intake_cards c ON c.id = s.card_id "
        "WHERE c.guild_id = ? AND c.resolution = ? AND c.created_at >= ? "
        "GROUP BY s.step_key, s.label ORDER BY MIN(s.position)",
        (guild_id, RESOLUTION_COMPLETED, since_ts),
    ).fetchall()
    return [
        {
            "key": str(r["step_key"]),
            "label": str(r["label"]),
            "appeared": int(r["appeared"]),
            "skipped": int(r["skipped"] or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# In-memory watch registry — the message hot-path accelerator
# ---------------------------------------------------------------------------
#
# on_message fires constantly; greet detection and code detection only matter
# for messages that mention a member with an open card. We keep a per-guild
# set of those member ids so the hot path is a set-membership test per
# mention. Seeded at startup (warm), fed by card creation, drained on close.
# A stale positive is harmless: the DB re-check finds no open card and bails.


_watch: dict[int, set[int]] = {}
_lock = threading.Lock()


def open_card_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    return {
        int(r["user_id"])
        for r in conn.execute(
            "SELECT user_id FROM intake_cards "
            "WHERE guild_id = ? AND resolved_at IS NULL",
            (guild_id,),
        ).fetchall()
    }


def warm(db_path, guild_ids) -> None:
    """Seed the watch registry at startup with every open card, enabled or
    not.

    Deliberately NOT filtered on :func:`is_enabled`: the hooks re-check
    config for behavior that should pause while disabled (greet/code/role
    ticks), but open cards must keep closing on leave/ban — and resume
    ticking after a re-enable — even across a disable → restart → enable
    cycle. Filtering here stranded such cards as zombies.
    """
    with open_db(db_path) as conn:
        seeded: dict[int, set[int]] = {}
        for gid in guild_ids:
            ids = open_card_user_ids(conn, gid)
            if ids:
                seeded[gid] = ids
    with _lock:
        _watch.clear()
        _watch.update(seeded)


def is_watched(guild_id: int, user_id: int) -> bool:
    """O(1) hot-path test: does this member (maybe) have an open card?"""
    bucket = _watch.get(guild_id)
    return bucket is not None and user_id in bucket


def add_watched(guild_id: int, user_id: int) -> None:
    with _lock:
        _watch.setdefault(guild_id, set()).add(user_id)


def discard(guild_id: int, user_id: int) -> None:
    """Drop a member from the watch set once their card closes."""
    with _lock:
        bucket = _watch.get(guild_id)
        if bucket is not None:
            bucket.discard(user_id)


def _reset_watch_for_tests() -> None:
    with _lock:
        _watch.clear()
