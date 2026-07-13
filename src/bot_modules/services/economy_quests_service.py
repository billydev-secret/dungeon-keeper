"""Economy quests — the DB layer for the quest library and claim machine.

The claim state machine (spec §4) is the money-critical core. The two partial
unique indexes on ``econ_quest_claims`` — one ``WHERE state='pending'``, one
``WHERE state='paid'`` — are the race anchors: ``claim_quest`` and
``resolve_claim`` let those indexes decide the winner (catch IntegrityError →
distinct ValueErrors) rather than reading before writing.

Every credit rides the caller's connection/transaction alongside its state
change — no internal commits, matching the stage-0/1 economy functions. The
caller's ``with open_db(...) as conn`` is the commit boundary, which is what
makes settlement crash-safe: a partial community sweep rolls back whole and
the next tick replays it, and the reserve-before-credit payout rows make that
replay pay only the members it missed.

Ledger kinds added here: ``quest`` (instant claim + approved sign-off) and
``quest_community`` (community settlement payout).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from bot_modules.economy import quests
from bot_modules.services.economy_service import EconSettings, apply_credit

# Quests whose reward the member cannot self-claim (community goals pay via
# the settlement sweep, not the claim path).
_CLAIMABLE_TYPES = ("daily", "weekly")

_EXPIRE_SECONDS_PER_DAY = 86400

# Distinct claim-collision messages (keyed off the state being inserted).
_PAID_EXISTS_MSG = "You have already completed this quest this period."
_PENDING_EXISTS_MSG = "You already have a claim awaiting sign-off for this quest."

# Columns update_quest is allowed to write. ``active`` is intentionally NOT
# here: activation must go through set_quest_active so the slot rule is
# enforced (id/guild_id/created_* are fixed).
_UPDATABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "qtype",
        "reward",
        "signoff",
        "criteria",
        "starts_at",
        "ends_at",
        "rotate_tag",
        "community_target",
        "trigger_words",
        "trigger_channel_id",
    }
)


class SlotLimitError(Exception):
    """Raised when activating a quest would exceed the library slot rule."""


@dataclass(frozen=True)
class ClaimOutcome:
    state: str  # 'paid' (instant) or 'pending' (sign-off)
    claim_id: int
    paid: int  # post-booster credited amount, 0 when pending


@dataclass(frozen=True)
class ClaimResolution:
    user_id: int
    quest_id: int
    paid: int
    deny_reason: str | None


# ── library CRUD ──────────────────────────────────────────────────────


def create_quest(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    title: str,
    description: str,
    qtype: str,
    reward: int,
    signoff: int,
    criteria: str,
    starts_at: float | None,
    ends_at: float | None,
    rotate_tag: str,
    community_target: int | None,
    created_by: int | None,
    trigger_words: str = "",
    trigger_channel_id: int | None = None,
) -> int:
    """Insert a quest into the guild's library (inactive). Returns its id."""
    if qtype not in ("daily", "weekly", "community"):
        raise ValueError(f"unknown quest type: {qtype!r}")
    cur = conn.execute(
        """
        INSERT INTO econ_quests
            (guild_id, title, description, qtype, reward, signoff, criteria,
             starts_at, ends_at, active, rotate_tag, community_target,
             created_by, created_at, trigger_words, trigger_channel_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            title,
            description,
            qtype,
            int(reward),
            1 if signoff else 0,
            criteria,
            starts_at,
            ends_at,
            rotate_tag,
            community_target,
            created_by,
            time.time(),
            trigger_words,
            trigger_channel_id,
        ),
    )
    return int(cur.lastrowid or 0)


def update_quest(
    conn: sqlite3.Connection, guild_id: int, quest_id: int, values: dict
) -> None:
    """Patch a quest's mutable fields; KeyError names any unknown field."""
    unknown = set(values) - _UPDATABLE_FIELDS
    if unknown:
        raise KeyError(f"unknown quest field(s): {sorted(unknown)}")
    if not values:
        return
    assignments = ", ".join(f"{k} = ?" for k in values)
    params = [
        (1 if v else 0) if k == "signoff" and isinstance(v, bool) else v
        for k, v in values.items()
    ]
    conn.execute(
        f"UPDATE econ_quests SET {assignments} WHERE id = ? AND guild_id = ?",
        (*params, quest_id, guild_id),
    )


def delete_quest(conn: sqlite3.Connection, guild_id: int, quest_id: int) -> None:
    """Delete a quest and its claims/progress/payouts.

    Refuses (ValueError) when any 'paid' claim exists — a paid claim is an
    audit record and must not vanish; callers deactivate such quests instead.
    """
    paid = conn.execute(
        """
        SELECT 1 FROM econ_quest_claims
        WHERE quest_id = ? AND guild_id = ? AND state = 'paid' LIMIT 1
        """,
        (quest_id, guild_id),
    ).fetchone()
    if paid is not None:
        raise ValueError("cannot delete a quest with paid claims; deactivate it instead")
    conn.execute(
        "DELETE FROM econ_quest_claims WHERE quest_id = ? AND guild_id = ?",
        (quest_id, guild_id),
    )
    conn.execute("DELETE FROM econ_community_progress WHERE quest_id = ?", (quest_id,))
    conn.execute("DELETE FROM econ_community_payouts WHERE quest_id = ?", (quest_id,))
    conn.execute(
        "DELETE FROM econ_quests WHERE id = ? AND guild_id = ?", (quest_id, guild_id)
    )


def get_quest(
    conn: sqlite3.Connection, guild_id: int, quest_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_quests WHERE id = ? AND guild_id = ?",
        (quest_id, guild_id),
    ).fetchone()


def list_quests(
    conn: sqlite3.Connection, guild_id: int, *, active_only: bool = False
) -> list[sqlite3.Row]:
    if active_only:
        return conn.execute(
            """
            SELECT * FROM econ_quests
            WHERE guild_id = ? AND active = 1
            ORDER BY qtype, id
            """,
            (guild_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM econ_quests WHERE guild_id = ? ORDER BY qtype, id",
        (guild_id,),
    ).fetchall()


def list_trigger_quests(
    conn: sqlite3.Connection, guild_id: int
) -> list[sqlite3.Row]:
    """Active daily/weekly quests with trigger phrases set (spec §4.4).

    These are the quests the on-message listener watches for. Community
    quests never appear — they are not member-claimable, so a trigger phrase
    on one has nothing to claim.
    """
    return conn.execute(
        """
        SELECT * FROM econ_quests
        WHERE guild_id = ? AND active = 1
          AND qtype IN ('daily', 'weekly') AND trigger_words != ''
        ORDER BY id
        """,
        (guild_id,),
    ).fetchall()


def _active_qtypes(
    conn: sqlite3.Connection, guild_id: int, *, exclude_id: int
) -> list[str]:
    rows = conn.execute(
        """
        SELECT qtype FROM econ_quests
        WHERE guild_id = ? AND active = 1 AND id != ?
        """,
        (guild_id, exclude_id),
    ).fetchall()
    return [row["qtype"] for row in rows]


def set_quest_active(
    conn: sqlite3.Connection, guild_id: int, quest_id: int, active: bool
) -> None:
    """Activate/deactivate a quest; SlotLimitError if activation over-fills."""
    quest = get_quest(conn, guild_id, quest_id)
    if quest is None:
        raise ValueError("quest not found")
    if active:
        existing = _active_qtypes(conn, guild_id, exclude_id=quest_id)
        if not quests.can_activate(existing, quest["qtype"]):
            raise SlotLimitError(f"too many active {quest['qtype']} quests")
    conn.execute(
        "UPDATE econ_quests SET active = ? WHERE id = ? AND guild_id = ?",
        (1 if active else 0, quest_id, guild_id),
    )


# ── claim state machine ───────────────────────────────────────────────


def claim_quest(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    quest_id: int,
    user_id: int,
    *,
    period: str,
    booster: bool,
) -> ClaimOutcome:
    """Claim a daily/weekly quest for a member in a given period.

    Instant (signoff=0): inserts a 'paid' claim and credits kind="quest" in
    the same transaction. Sign-off (signoff=1): inserts a 'pending' claim (no
    credit). The partial unique index for the inserted state is the authority
    on collisions — a caught IntegrityError maps to a distinct message.

    ValueError when the quest is missing/inactive/out of date-range, when it
    is a community quest (those pay via settlement, never a self-claim), or
    when a 'paid'/'pending' claim already exists for this period.
    """
    quest = get_quest(conn, guild_id, quest_id)
    if quest is None:
        raise ValueError("quest not found")
    if quest["qtype"] not in _CLAIMABLE_TYPES:
        raise ValueError("this quest cannot be claimed directly")
    if not quest["active"]:
        raise ValueError("quest is not active")
    now = time.time()
    if quest["starts_at"] is not None and now < float(quest["starts_at"]):
        raise ValueError("quest has not started yet")
    if quest["ends_at"] is not None and now > float(quest["ends_at"]):
        raise ValueError("quest has ended")

    signoff = bool(quest["signoff"])
    # UX pre-check: a paid claim this period blocks re-claiming. This is not
    # the money guard (a race could still slip a pending through) — the guard
    # is the paid index enforced at approve time in resolve_claim.
    if signoff:
        already_paid = conn.execute(
            """
            SELECT 1 FROM econ_quest_claims
            WHERE quest_id = ? AND user_id = ? AND period = ? AND state = 'paid'
            LIMIT 1
            """,
            (quest_id, user_id, period),
        ).fetchone()
        if already_paid is not None:
            raise ValueError(_PAID_EXISTS_MSG)

    state = "pending" if signoff else "paid"
    try:
        cur = conn.execute(
            """
            INSERT INTO econ_quest_claims
                (quest_id, guild_id, user_id, period, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (quest_id, guild_id, user_id, period, state, now),
        )
    except sqlite3.IntegrityError as exc:
        # Only the index for the inserted state can fire, so the state maps
        # the collision to its message unambiguously.
        if state == "pending":
            raise ValueError(_PENDING_EXISTS_MSG) from exc
        raise ValueError(_PAID_EXISTS_MSG) from exc

    claim_id = int(cur.lastrowid or 0)
    if signoff:
        return ClaimOutcome(state="pending", claim_id=claim_id, paid=0)

    paid = _credit_reward(conn, settings, quest, user_id, booster=booster, claim_id=claim_id)
    return ClaimOutcome(state="paid", claim_id=claim_id, paid=paid)


def _credit_reward(
    conn: sqlite3.Connection,
    settings: EconSettings,
    quest: sqlite3.Row,
    user_id: int,
    *,
    booster: bool,
    claim_id: int,
) -> int:
    reward = int(quest["reward"])
    if reward < 1:
        return 0
    return apply_credit(
        conn,
        int(quest["guild_id"]),
        user_id,
        reward,
        "quest",
        meta={"quest_id": int(quest["id"]), "claim_id": claim_id},
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def set_claim_card(
    conn: sqlite3.Connection, claim_id: int, channel_id: int, message_id: int
) -> None:
    """Record the bank-channel sign-off card so views can find the claim."""
    conn.execute(
        """
        UPDATE econ_quest_claims
        SET card_channel_id = ?, card_message_id = ?
        WHERE id = ?
        """,
        (channel_id, message_id, claim_id),
    )


def resolve_claim(
    conn: sqlite3.Connection,
    settings: EconSettings,
    claim_id: int,
    *,
    approve: bool,
    resolver_id: int,
    deny_reason: str | None = None,
    booster: bool,
) -> ClaimResolution:
    """Approve or deny a pending sign-off claim (ValueError if not pending).

    Approve: transition to 'paid' first — the paid index fires if this period
    already paid, so the IntegrityError becomes a ValueError before any credit
    (the double-pay backstop) — then credit kind="quest". Deny: transition to
    'denied' with the reason (the claim stays re-claimable for the period).
    """
    claim = conn.execute(
        "SELECT * FROM econ_quest_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    if claim is None:
        raise ValueError("claim not found")
    if claim["state"] != "pending":
        raise ValueError("claim is not pending")

    now = time.time()
    quest_id = int(claim["quest_id"])
    user_id = int(claim["user_id"])

    if not approve:
        conn.execute(
            """
            UPDATE econ_quest_claims
            SET state = 'denied', resolved_at = ?, resolver_id = ?, deny_reason = ?
            WHERE id = ?
            """,
            (now, resolver_id, deny_reason, claim_id),
        )
        return ClaimResolution(
            user_id=user_id, quest_id=quest_id, paid=0, deny_reason=deny_reason
        )

    try:
        conn.execute(
            """
            UPDATE econ_quest_claims
            SET state = 'paid', resolved_at = ?, resolver_id = ?
            WHERE id = ?
            """,
            (now, resolver_id, claim_id),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(_PAID_EXISTS_MSG) from exc

    quest = get_quest(conn, int(claim["guild_id"]), quest_id)
    if quest is None:
        raise ValueError("quest not found")
    paid = _credit_reward(conn, settings, quest, user_id, booster=booster, claim_id=claim_id)
    return ClaimResolution(
        user_id=user_id, quest_id=quest_id, paid=paid, deny_reason=None
    )


def expire_stale_claims(
    conn: sqlite3.Connection, now_ts: float, max_age_days: int = 7
) -> list[sqlite3.Row]:
    """Expire pending claims older than ``max_age_days`` and return them once.

    The UPDATE ... RETURNING is atomic: rows transition out of 'pending' as
    they are returned, so a replay never re-emits the same expired claim (the
    caller DMs each returned claimant exactly once).
    """
    cutoff = now_ts - max_age_days * _EXPIRE_SECONDS_PER_DAY
    return conn.execute(
        """
        UPDATE econ_quest_claims
        SET state = 'expired', resolved_at = ?
        WHERE state = 'pending' AND created_at < ?
        RETURNING *
        """,
        (now_ts, cutoff),
    ).fetchall()


def list_claims(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    state: str | None = None,
    quest_id: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """List claims for a guild, newest first (addition beyond the frozen
    contract for the dashboard's pending-claims view). Optionally filter by
    state and quest."""
    where = ["guild_id = ?"]
    params: list[object] = [guild_id]
    if state is not None:
        where.append("state = ?")
        params.append(state)
    if quest_id is not None:
        where.append("quest_id = ?")
        params.append(quest_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT * FROM econ_quest_claims
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def deny_history(
    conn: sqlite3.Connection, quest_id: int, user_id: int
) -> list[sqlite3.Row]:
    """Prior denied/expired claims for a member on a quest, newest first."""
    return conn.execute(
        """
        SELECT * FROM econ_quest_claims
        WHERE quest_id = ? AND user_id = ? AND state IN ('denied', 'expired')
        ORDER BY resolved_at DESC, id DESC
        """,
        (quest_id, user_id),
    ).fetchall()


# ── community quests ──────────────────────────────────────────────────


def set_community_progress(
    conn: sqlite3.Connection, quest_id: int, current: int, *, target: int
) -> bool:
    """Set a community quest's running total; True on the completion crossing.

    ``completed_at`` is stamped exactly once — the first time progress reaches
    ``target`` — and never cleared afterward even if ``current`` later drops.
    Returns True only on the call that stamps it.
    """
    row = conn.execute(
        "SELECT current, completed_at FROM econ_community_progress WHERE quest_id = ?",
        (quest_id,),
    ).fetchone()
    now = time.time()
    already_complete = row is not None and row["completed_at"] is not None
    crossing = not already_complete and current >= target
    completed_at = now if crossing else (row["completed_at"] if row else None)
    conn.execute(
        """
        INSERT INTO econ_community_progress (quest_id, current, completed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(quest_id) DO UPDATE SET
            current = excluded.current,
            completed_at = excluded.completed_at
        """,
        (quest_id, current, completed_at),
    )
    return crossing


def settle_community_quest(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    quest_id: int,
    member_boosters: dict[int, bool],
) -> int:
    """Pay a completed community quest to its members, once each.

    Per member: INSERT OR IGNORE the payout reservation row, then credit
    kind="quest_community" only when that insert actually reserved (rowcount
    1). Re-invocation — a manual dashboard settle after the auto-sweep, or a
    replay after a rolled-back crash — pays only the members not yet reserved.
    ``settled_at`` is stamped last, after the whole sweep, so a mid-sweep crash
    rolls back cleanly and the next run resumes. Returns the count paid here.
    """
    quest = get_quest(conn, guild_id, quest_id)
    reward = int(quest["reward"]) if quest is not None else 0
    paid_count = 0
    for user_id, booster in member_boosters.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO econ_community_payouts (quest_id, user_id) VALUES (?, ?)",
            (quest_id, user_id),
        )
        if (cur.rowcount or 0) == 0:
            continue
        if reward >= 1:
            apply_credit(
                conn,
                guild_id,
                user_id,
                reward,
                "quest_community",
                meta={"quest_id": quest_id},
                booster=booster,
                multiplier=settings.booster_multiplier,
            )
        paid_count += 1
    conn.execute(
        """
        INSERT INTO econ_community_progress (quest_id, current, settled_at)
        VALUES (?, 0, ?)
        ON CONFLICT(quest_id) DO UPDATE SET settled_at = excluded.settled_at
        """,
        (quest_id, time.time()),
    )
    return paid_count


def list_settleable_community_quests(
    conn: sqlite3.Connection, guild_id: int
) -> list[sqlite3.Row]:
    """Completed-but-unsettled community quests the loop may auto-settle.

    Addition beyond the frozen contract, for Agent B's sweep. Enforces spec
    §4.3 "sign-off gates settlement": ``signoff=1`` quests are excluded here
    so the auto-sweep never pays them — the dashboard's manual
    ``settle_community_quest`` is the human sign-off and bypasses this filter.
    """
    return conn.execute(
        """
        SELECT q.* FROM econ_quests q
        JOIN econ_community_progress p ON p.quest_id = q.id
        WHERE q.guild_id = ? AND q.qtype = 'community' AND q.signoff = 0
          AND p.completed_at IS NOT NULL AND p.settled_at IS NULL
        ORDER BY q.id
        """,
        (guild_id,),
    ).fetchall()


def active_member_ids(
    conn: sqlite3.Connection, guild_id: int, days: int = 30
) -> list[int]:
    """Members active in the last ``days`` (member_activity.last_message_at)."""
    cutoff = time.time() - days * _EXPIRE_SECONDS_PER_DAY
    rows = conn.execute(
        """
        SELECT user_id FROM member_activity
        WHERE guild_id = ? AND last_message_at >= ?
        """,
        (guild_id, cutoff),
    ).fetchall()
    return [int(row["user_id"]) for row in rows]


# ── rotation ──────────────────────────────────────────────────────────


def rotate_pool(
    conn: sqlite3.Connection, guild_id: int, qtype: str
) -> int | None:
    """Advance a rotate-tag pool of the given type by one slot.

    No-op (None) unless an active quest of ``qtype`` carries a rotate_tag
    whose pool has more than one quest. Otherwise deactivate that active
    quest and activate the next by ``pick_rotation``, honoring the slot rule.
    Returns the newly-activated quest id, or None.
    """
    current = conn.execute(
        """
        SELECT id, rotate_tag FROM econ_quests
        WHERE guild_id = ? AND qtype = ? AND active = 1 AND rotate_tag != ''
        ORDER BY id LIMIT 1
        """,
        (guild_id, qtype),
    ).fetchone()
    if current is None:
        return None
    tag = current["rotate_tag"]
    current_id = int(current["id"])
    pool = conn.execute(
        """
        SELECT id FROM econ_quests
        WHERE guild_id = ? AND qtype = ? AND rotate_tag = ?
        ORDER BY id
        """,
        (guild_id, qtype, tag),
    ).fetchall()
    pool_ids = [int(row["id"]) for row in pool]
    next_id = quests.pick_rotation(pool_ids, current_id)
    if next_id is None or next_id == current_id:
        return None

    conn.execute(
        "UPDATE econ_quests SET active = 0 WHERE id = ? AND guild_id = ?",
        (current_id, guild_id),
    )
    existing = _active_qtypes(conn, guild_id, exclude_id=next_id)
    if not quests.can_activate(existing, qtype):
        return None
    conn.execute(
        "UPDATE econ_quests SET active = 1 WHERE id = ? AND guild_id = ?",
        (next_id, guild_id),
    )
    return next_id
