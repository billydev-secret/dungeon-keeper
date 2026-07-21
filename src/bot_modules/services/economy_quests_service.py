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

import hashlib
import sqlite3
import time
from dataclasses import dataclass

import logging

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.xp_system import apply_xp_award, load_xp_settings
from bot_modules.economy import live_signal, quests
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    apply_debit,
    get_balance,
    load_econ_settings,
)

log = logging.getLogger(__name__)

# Quests whose reward the member cannot self-claim (community goals pay via
# the settlement sweep, not the claim path). Event quests DO go through
# claim_quest, but only the trigger listener calls it for them — the member
# views never offer a claim button, and quest_period() has no calendar key
# for 'event' so a stray self-claim path can't even build a period.
_CLAIMABLE_TYPES = ("daily", "weekly", "monthly", "event")

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
        "trigger_kind",
        "target_count",
        "target_min",
        "target_max",
        "reward_xp",
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
    trigger_kind: str = "",
    target_count: int = 1,
    target_min: int = 0,
    target_max: int = 0,
    reward_xp: int = 0,
) -> int:
    """Insert a quest into the guild's library (inactive). Returns its id."""
    if qtype not in ("daily", "weekly", "monthly", "community", "event"):
        raise ValueError(f"unknown quest type: {qtype!r}")
    if reward_xp < 0:
        raise ValueError("XP reward cannot be negative")
    _check_trigger_config(qtype, trigger_kind, trigger_words, signoff)
    _check_target_count(qtype, trigger_kind, target_count, target_min, target_max)
    cur = conn.execute(
        """
        INSERT INTO econ_quests
            (guild_id, title, description, qtype, reward, signoff, criteria,
             starts_at, ends_at, active, rotate_tag, community_target,
             created_by, created_at, trigger_words, trigger_channel_id,
             trigger_kind, target_count, target_min, target_max, reward_xp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            trigger_kind,
            int(target_count),
            int(target_min),
            int(target_max),
            int(reward_xp),
        ),
    )
    return int(cur.lastrowid or 0)


def _check_trigger_config(
    qtype: str, trigger_kind: str, trigger_words: str, signoff: int = 0
) -> None:
    """Validate the trigger configuration for a quest type.

    Event quests require a known trigger kind (they have no other way to be
    claimed). Daily/weekly quests may carry one — "do the thing once this
    period" — or trigger words, but not both (two auto-claim paths racing for
    one period would be ambiguous to explain, so it's rejected outright).

    Community quests may carry a kind since stage 3 (quest-variety plan):
    the kind's events then bump the guild-wide counter automatically and the
    weekly scheduler owns activation/settlement. A kind community quest
    can't be sign-off (settlement is tiered and automatic; a human gate
    belongs on manual community quests) and never takes trigger words.

    The ``confession`` kind additionally forbids sign-off: a sign-off claim
    posts a bank-channel card naming the claimant, which is timing-correlatable
    against the anonymous confessions feed — exactly the deanonymization we
    keep the auto-claim silent to avoid.
    """
    if trigger_kind and trigger_kind not in quests.TRIGGER_KINDS:
        raise ValueError(f"unknown trigger kind: {trigger_kind!r}")
    if qtype == "event":
        if not trigger_kind:
            raise ValueError("event quests need a trigger kind")
    elif qtype == "community":
        if trigger_kind and signoff:
            raise ValueError(
                "auto-tracking community quests cannot require sign-off "
                "(tier settlement is automatic)"
            )
    if trigger_kind and trigger_words.strip():
        raise ValueError("a quest takes trigger words or a trigger kind, not both")
    if trigger_kind == "confession" and signoff:
        raise ValueError("confession quests cannot require sign-off (it would deanonymize the confessor)")


def _check_target_count(
    qtype: str,
    trigger_kind: str,
    target_count: int,
    target_min: int = 0,
    target_max: int = 0,
) -> None:
    """A target above 1 (or a target band) only fits counted trigger quests.

    Manual claims are one-shot per period (nothing increments a count), and
    an event quest pays *every* occurrence, so neither can carry a target. A
    band (``0 < target_min < target_max``) draws the per-member target from a
    Gaussian instead of a fixed count; it lives under the same rules and, like
    a fixed count > 1, must sit on a counted daily/weekly/monthly quest.
    """
    if target_count < 1:
        raise ValueError("target count must be at least 1")
    has_band = target_min != 0 or target_max != 0
    if has_band and not (0 < target_min < target_max):
        raise ValueError("target band needs 0 < target_min < target_max")
    if target_count == 1 and not has_band:
        return
    if not trigger_kind:
        raise ValueError("a target count needs a game trigger to count")
    if qtype not in ("daily", "weekly", "monthly"):
        raise ValueError("a target count needs a daily/weekly/monthly cadence")


def update_quest(
    conn: sqlite3.Connection, guild_id: int, quest_id: int, values: dict
) -> None:
    """Patch a quest's mutable fields; KeyError names any unknown field."""
    unknown = set(values) - _UPDATABLE_FIELDS
    if unknown:
        raise KeyError(f"unknown quest field(s): {sorted(unknown)}")
    if not values:
        return
    if {
        "qtype",
        "trigger_kind",
        "trigger_words",
        "target_count",
        "target_min",
        "target_max",
    } & set(values):
        quest = get_quest(conn, guild_id, quest_id)
        if quest is not None:
            merged_qtype = str(values.get("qtype", quest["qtype"]))
            merged_kind = str(values.get("trigger_kind", quest["trigger_kind"]))
            _check_trigger_config(
                merged_qtype,
                merged_kind,
                str(values.get("trigger_words", quest["trigger_words"])),
                int(values.get("signoff", quest["signoff"])),
            )
            _check_target_count(
                merged_qtype,
                merged_kind,
                int(values.get("target_count", quest["target_count"])),
                int(values.get("target_min", quest["target_min"])),
                int(values.get("target_max", quest["target_max"])),
            )
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
    conn.execute("DELETE FROM econ_quest_progress WHERE quest_id = ?", (quest_id,))
    conn.execute(
        "DELETE FROM econ_quest_progress_marks WHERE quest_id = ?", (quest_id,)
    )
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
          AND qtype IN ('daily', 'weekly', 'monthly') AND trigger_words != ''
        ORDER BY id
        """,
        (guild_id,),
    ).fetchall()


def list_kind_triggered_quests(
    conn: sqlite3.Connection, guild_id: int, trigger_kind: str
) -> list[sqlite3.Row]:
    """The guild's active quests auto-paid by a trigger kind (any qtype).

    An event quest pays per occurrence; a daily/weekly quest with the same
    kind auto-claims its calendar period ("do it once today/this week").
    """
    return conn.execute(
        """
        SELECT * FROM econ_quests
        WHERE guild_id = ? AND active = 1 AND trigger_kind = ?
        ORDER BY id
        """,
        (guild_id, trigger_kind),
    ).fetchall()


def list_active_pool_ids(
    conn: sqlite3.Connection, guild_id: int, qtype: str
) -> list[int]:
    """Active quest ids of a cadence — the pool the per-user board draws from."""
    return [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM econ_quests "
            "WHERE guild_id = ? AND active = 1 AND qtype = ? ORDER BY id",
            (guild_id, qtype),
        ).fetchall()
    ]


def board_sizes(settings: EconSettings) -> dict[str, int]:
    """The guild's configured personal-board size per cadence."""
    return {
        "daily": settings.quest_board_daily,
        "weekly": settings.quest_board_weekly,
        "monthly": settings.quest_board_monthly,
    }


# Ledger kinds that count as "a shop purchase" for the shop_purchase setup
# quest: voluntary member spends only. Renewal billing shares the "rental"
# kind, which is fine here — nobody's FIRST purchase is a renewal. Deliberate
# omissions: quest_reroll (board mechanics), wager stakes (not a purchase),
# transfers/gifts, admin adjustments.
PURCHASE_LEDGER_KINDS: tuple[str, ...] = (
    "rental",
    "streak_shield",
    "emoji_sponsor",
    "qotd_sponsor",
    "raffle_ticket",
)


def _setup_underlying_done(
    conn: sqlite3.Connection, guild_id: int, user_id: int, kind: str
) -> bool:
    """Whether the member has already done a one-time setup kind's real action.

    A direct existence check against the owning feature's table — a bio row
    means they filled one out, a birthday row means they set it, a role-menu
    grant or purchase-kind ledger row means they've picked/bought. Kept as
    inline SQL (rather than importing the owning modules) so quest assignment
    stays self-contained; these are stable core tables. role_pick has a known
    soft edge: announcement-button grants aren't recorded in
    ``role_menu_grants``, so those pickers stay visible until the paid-claim
    backstop in ``_setup_quest_done`` catches them.
    """
    if kind == "bio_set":
        row = conn.execute(
            "SELECT 1 FROM bios WHERE guild_id = ? AND user_id = ? LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
    elif kind == "birthday_set":
        row = conn.execute(
            "SELECT 1 FROM member_birthdays WHERE guild_id = ? AND user_id = ? "
            "LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
    elif kind == "role_pick":
        row = conn.execute(
            "SELECT 1 FROM role_menu_grants "
            "WHERE guild_id = ? AND user_id = ? AND action = 'grant' LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
    elif kind == "shop_purchase":
        placeholders = ",".join("?" * len(PURCHASE_LEDGER_KINDS))
        row = conn.execute(
            "SELECT 1 FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
            f"AND kind IN ({placeholders}) LIMIT 1",
            (guild_id, user_id, *PURCHASE_LEDGER_KINDS),
        ).fetchone()
    else:
        return False
    return row is not None


def _setup_quest_done(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    quest_id: int,
    kind: str,
) -> bool:
    """Whether a one-time setup quest should drop off ``user_id``'s board.

    True once they've done the underlying thing (bio/birthday exists) OR
    already claimed this quest — the latter a backstop for the odd member who
    deletes their bio after claiming, so a paid-out setup quest never re-shows
    as a dead nudge that can't re-pay (its claim sits on the constant period).
    """
    if _setup_underlying_done(conn, guild_id, user_id, kind):
        return True
    row = conn.execute(
        "SELECT 1 FROM econ_quest_claims "
        "WHERE quest_id = ? AND user_id = ? AND state = 'paid' LIMIT 1",
        (quest_id, user_id),
    ).fetchone()
    return row is not None


def _setup_kinds_by_id(
    conn: sqlite3.Connection, guild_id: int, quest_ids: set[int]
) -> dict[int, str]:
    """Map the setup-kind quest ids among ``quest_ids`` to their trigger kind."""
    if not quest_ids:
        return {}
    placeholders = ",".join("?" * len(quest_ids))
    rows = conn.execute(
        f"SELECT id, trigger_kind FROM econ_quests WHERE id IN ({placeholders})",
        tuple(quest_ids),
    ).fetchall()
    return {
        int(r["id"]): str(r["trigger_kind"])
        for r in rows
        if str(r["trigger_kind"] or "") in quests.SETUP_QUEST_KINDS
    }


def _drop_completed_setup(
    conn: sqlite3.Connection, guild_id: int, user_id: int, board: set[int]
) -> set[int]:
    """Remove one-time setup quests the member has already done from a board.

    Deliberately does **not** refill the freed slot: pulling a new quest in
    mid-period would shift the deterministic window and could strand a counted
    quest's in-progress work. A completed setup quest simply leaves the board
    one shorter for that member — rare (few setup quests, drawn occasionally)
    and self-heals next period.
    """
    kinds = _setup_kinds_by_id(conn, guild_id, board)
    if not kinds:
        return board
    done = {
        qid
        for qid, kind in kinds.items()
        if _setup_quest_done(conn, guild_id, user_id, qid, kind)
    }
    return board - done


def assigned_board_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    qtype: str,
    local_day: str,
    settings: EconSettings | None = None,
) -> set[int]:
    """The quest ids on ``user_id``'s personal board for ``qtype`` this period.

    A per-member subset of the cadence pool (the guild's configured
    ``board_size`` of them), stable within the period and spaced so repeats
    stay ~a week apart. Only daily/weekly/monthly have a board; other
    cadences return an empty set, as does a cadence sized to 0. One-time setup
    quests the member has already completed are dropped (see
    ``_drop_completed_setup``), so only members who haven't done them see them.
    """
    sizes = board_sizes(settings) if settings is not None else None
    n = quests.board_size(qtype, sizes)
    if n <= 0 or not quests.has_board(qtype):
        return set()
    pool = list_active_pool_ids(conn, guild_id, qtype)
    idx = quests.period_index(qtype, local_day)
    board = set(quests.assigned_quest_ids(pool, user_id, idx, n))
    # Reroll overrides sit on top of the pure draw: from → to per slot, this
    # period only. A replacement that has since left the active pool falls
    # back to the pure slot rather than dropping the board below size.
    pool_set = set(pool)
    for row in conn.execute(
        "SELECT from_quest_id, to_quest_id FROM econ_board_overrides "
        "WHERE guild_id = ? AND user_id = ? AND qtype = ? AND period_idx = ?",
        (guild_id, user_id, qtype, idx),
    ):
        frm, to = int(row["from_quest_id"]), int(row["to_quest_id"])
        if frm in board and to in pool_set:
            board.discard(frm)
            board.add(to)
    return _drop_completed_setup(conn, guild_id, user_id, board)


def reroll_board_slot(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    quest_id: int,
    local_day: str,
) -> tuple[sqlite3.Row, int]:
    """Swap one personal-board quest for a different pool quest (stage 5).

    One free reroll per member per guild-local day, across all cadences,
    then up to ``quest_reroll_daily_cap`` more at ``price_quest_reroll``
    each. The slot must be untouched this period (no claim, no counted
    progress). The replacement is the first pool quest in the member's own
    shuffle order that isn't on their board, preferring a **different
    trigger kind** — the reroll exists for "this quest doesn't fit how I use
    the server", so same-kind swaps are the last resort.

    Returns ``(new_quest_row, cost)`` where cost is 0 for the free reroll;
    raises ValueError with a member-facing message otherwise.
    """
    quest = get_quest(conn, guild_id, quest_id)
    if quest is None or not quest["active"]:
        raise ValueError("That quest is no longer active.")
    qtype = str(quest["qtype"])
    if qtype not in quests.BOARD_CADENCES:
        raise ValueError("Only board quests (daily/weekly/monthly) reroll.")
    board = assigned_board_ids(conn, guild_id, user_id, qtype, local_day, settings)
    if quest_id not in board:
        raise ValueError("That quest isn't on your board this period.")
    period = quests.quest_period(qtype, local_day)
    touched = conn.execute(
        "SELECT 1 FROM econ_quest_claims WHERE quest_id = ? AND user_id = ? "
        "AND period = ? AND state IN ('paid', 'pending') LIMIT 1",
        (quest_id, user_id, period),
    ).fetchone()
    if touched is None and get_progress(conn, quest_id, user_id, period) > 0:
        touched = True
    if touched:
        raise ValueError(
            "You've already made progress on that quest this period — "
            "rerolls only swap untouched quests."
        )

    idx = quests.period_index(qtype, local_day)
    pool = list_active_pool_ids(conn, guild_id, qtype)
    ordered = quests.assigned_quest_ids(pool, user_id, idx, len(pool))
    # Never swap a member INTO a one-time setup quest they've already done —
    # it would drop straight back off their board (_drop_completed_setup) and
    # waste the reroll on an invisible slot.
    done_setup = {
        qid
        for qid, kind in _setup_kinds_by_id(conn, guild_id, set(ordered)).items()
        if _setup_quest_done(conn, guild_id, user_id, qid, kind)
    }
    candidates = [q for q in ordered if q not in board and q not in done_setup]
    if not candidates:
        raise ValueError("The pool has nothing else to swap in.")
    old_kind = str(quest["trigger_kind"] or "")
    different = [
        q for q in candidates
        if str((get_quest(conn, guild_id, q) or {})["trigger_kind"] or "")
        != old_kind
    ]
    new_id = (different or candidates)[0]

    # Spend the reroll LAST, so validation failures never consume the free
    # allowance or charge the wallet. Free first: the row's existence is the
    # "free one is gone" flag, so a successful INSERT *is* the free reroll.
    cost = 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO econ_rerolls (guild_id, user_id, local_day) "
        "VALUES (?, ?, ?)",
        (guild_id, user_id, local_day),
    )
    if (cur.rowcount or 0) == 0:
        cost = _charge_paid_reroll(conn, settings, guild_id, user_id, local_day)

    # If the outgoing quest was itself a replacement, update that override
    # in place — application then never has to chain from→to→to.
    updated = conn.execute(
        "UPDATE econ_board_overrides SET to_quest_id = ? "
        "WHERE guild_id = ? AND user_id = ? AND qtype = ? AND period_idx = ? "
        "AND to_quest_id = ?",
        (new_id, guild_id, user_id, qtype, idx, quest_id),
    )
    if (updated.rowcount or 0) == 0:
        conn.execute(
            "INSERT OR REPLACE INTO econ_board_overrides "
            "(guild_id, user_id, qtype, period_idx, from_quest_id, to_quest_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, qtype, idx, quest_id, new_id),
        )
    new_quest = get_quest(conn, guild_id, new_id)
    assert new_quest is not None
    return new_quest, cost


def _paid_rerolls_today(
    conn: sqlite3.Connection, guild_id: int, user_id: int, local_day: str
) -> int:
    row = conn.execute(
        "SELECT paid_count FROM econ_rerolls WHERE guild_id = ? AND user_id = ? "
        "AND local_day = ?",
        (guild_id, user_id, local_day),
    ).fetchone()
    return int(row["paid_count"]) if row is not None else 0


def _charge_paid_reroll(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    local_day: str,
) -> int:
    """Debit one paid reroll and count it, or raise a member-facing ValueError.

    Only reached once the free reroll is gone. Every failure raises before any
    write, so a capped-out or broke member loses nothing.
    """
    price = int(settings.price_quest_reroll)
    cap = int(settings.quest_reroll_daily_cap)
    if price < 1 or cap < 1:
        raise ValueError("You've already used today's free reroll.")
    used = _paid_rerolls_today(conn, guild_id, user_id, local_day)
    if used >= cap:
        raise ValueError(
            f"You've used today's free reroll and all {cap} paid ones — "
            "your board refreshes tomorrow."
        )
    unit = settings.currency_plural or "coins"
    if not apply_debit(
        conn,
        guild_id,
        user_id,
        price,
        "quest_reroll",
        meta={"local_day": local_day},
    ):
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(
            f"Another reroll costs {price} {unit} — you have {have}."
        )
    conn.execute(
        "UPDATE econ_rerolls SET paid_count = paid_count + 1 "
        "WHERE guild_id = ? AND user_id = ? AND local_day = ?",
        (guild_id, user_id, local_day),
    )
    return price


def reroll_available(
    conn: sqlite3.Connection, guild_id: int, user_id: int, local_day: str
) -> bool:
    """Whether the member still has today's free reroll."""
    row = conn.execute(
        "SELECT 1 FROM econ_rerolls WHERE guild_id = ? AND user_id = ? "
        "AND local_day = ?",
        (guild_id, user_id, local_day),
    ).fetchone()
    return row is None


def reroll_quote(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    local_day: str,
) -> int | None:
    """What the member's next reroll would cost: 0 free, >0 paid, None if none left.

    Affordability is deliberately *not* checked here — the shop tells you the
    price and lets you find out you're short, rather than hiding the option
    and leaving you wondering where the reroll went.
    """
    if reroll_available(conn, guild_id, user_id, local_day):
        return 0
    price = int(settings.price_quest_reroll)
    cap = int(settings.quest_reroll_daily_cap)
    if price < 1 or cap < 1:
        return None
    if _paid_rerolls_today(conn, guild_id, user_id, local_day) >= cap:
        return None
    return price


def spotlight_kind(
    conn: sqlite3.Connection, guild_id: int, iso_week: str
) -> str | None:
    """This ISO week's ⚡ spotlight trigger kind — quest payouts double.

    Deterministic on (guild, week) over the kinds with at least one active
    quest, so every surface (claim credit, embed, /quests, live tracker)
    agrees without stored state. None when fewer than 2 kinds are active —
    a "rotating featured activity" is meaningless with nothing to rotate,
    and it would otherwise be a permanent silent 2× on a tiny library.
    """
    kinds = sorted(
        str(r["trigger_kind"])
        for r in conn.execute(
            "SELECT DISTINCT trigger_kind FROM econ_quests "
            "WHERE guild_id = ? AND active = 1 AND trigger_kind != '' "
            "AND qtype != 'community'",
            (guild_id,),
        )
    )
    if len(kinds) < 2:
        return None
    digest = hashlib.sha256(f"{guild_id}:{iso_week}".encode()).hexdigest()
    return kinds[int(digest, 16) % len(kinds)]


def load_member_quest_board(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    day: str,
) -> list[dict]:
    """Active quests for a member on ``day``, with progress/claim state.

    Shared by the ``/bank quests`` panel and the daily-login DM's quest
    recap — one place computing "what does this member still have to do".
    """
    rows = conn.execute(
        """
        SELECT * FROM econ_quests
        WHERE guild_id = ? AND active = 1
        ORDER BY qtype, id
        """,
        (guild_id,),
    ).fetchall()
    spot = spotlight_kind(conn, guild_id, quests.iso_week_for(day))
    boards: dict[str, set[int]] = {}
    out: list[dict] = []
    for row in rows:
        qtype = str(row["qtype"])
        quest_id = int(row["id"])
        if quests.has_board(qtype):
            if qtype not in boards:
                boards[qtype] = assigned_board_ids(
                    conn, guild_id, user_id, qtype, day, settings
                )
            if quest_id not in boards[qtype]:
                continue  # not on this member's board this period
        entry: dict = {
            "id": quest_id,
            "title": row["title"],
            "description": row["description"],
            "qtype": qtype,
            "reward": int(row["reward"]),
            "reward_xp": int(row["reward_xp"]),
            "signoff": bool(row["signoff"]),
            "criteria": row["criteria"],
            "spotlight": bool(
                spot and str(row["trigger_kind"] or "") == spot
            ),
        }
        if qtype == "community":
            prog = conn.execute(
                "SELECT current FROM econ_community_progress WHERE quest_id = ?",
                (quest_id,),
            ).fetchone()
            target = row["community_target"]
            entry["state"] = "community"
            entry["current"] = int(prog["current"]) if prog else 0
            entry["target"] = int(target) if target is not None else 0
        elif qtype == "event":
            # No calendar period — the trigger listener pays per
            # occurrence (e.g. per photo card), so the list shows the
            # standing how-to instead of a per-period claim state.
            entry["state"] = str(row["trigger_kind"]) or "trigger"
        else:
            period = quests.quest_period(qtype, day)
            claim = conn.execute(
                """
                SELECT state FROM econ_quest_claims
                WHERE quest_id = ? AND user_id = ? AND period = ?
                  AND state IN ('paid', 'pending')
                ORDER BY CASE state WHEN 'paid' THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (quest_id, user_id, period),
            ).fetchone()
            kind = str(row["trigger_kind"] or "")
            has_trigger = bool(str(row["trigger_words"] or "").strip())
            # Resolves (and stores) the member's dynamic target on first
            # sight, so the wallet shows the same number the fire path
            # will enforce all period.
            target = resolve_member_target(
                conn, guild_id, user_id, row,
                period=period, local_day=day,
            )
            if kind and target > 1:
                entry["progress_current"] = get_progress(
                    conn, quest_id, user_id, period
                )
                entry["progress_target"] = target
            if claim is None:
                # Trigger quests never enter the claim select — the
                # phrase/game event IS the verification, so a manual
                # claim would bypass it.
                entry["state"] = kind or (
                    "trigger" if has_trigger else "claimable"
                )
            elif claim["state"] == "paid":
                entry["state"] = "done"
            else:
                entry["state"] = "pending"
        out.append(entry)
    return out


def local_day_for_period(qtype: str, period: str) -> str:
    """A representative guild-local day inside a cadence period key."""
    from datetime import date

    if qtype == "daily":
        return period
    if qtype == "weekly":
        year, week = period.split("-W")
        return date.fromisocalendar(int(year), int(week), 1).isoformat()
    return f"{period}-01"  # monthly


def maybe_pay_set_bonus(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    qtype: str,
    period: str,
) -> int:
    """Pay the clear-the-board bonus if this claim finished the set.

    Called after any claim pays (instant or approved sign-off). The board is
    resolved for the CLAIM's period — a sign-off approved days later still
    completes that period's set. Exactly-once via the econ_set_bonus
    reservation row; flat, no booster multiplier. Returns the amount paid.
    """
    bonus = {
        "daily": settings.quest_set_bonus_daily,
        "weekly": settings.quest_set_bonus_weekly,
    }.get(qtype, 0)
    if bonus <= 0:
        return 0
    # A one-time setup quest (bio/birthday) is a daily by cadence but claims on
    # a constant occurrence period ("<kind>:set", not a calendar day). Such a
    # claim isn't part of any day's board set, and feeding its period to the
    # calendar math below would raise — so it never triggers a set bonus.
    if ":" in period:
        return 0
    day = local_day_for_period(qtype, period)
    board = assigned_board_ids(conn, guild_id, user_id, qtype, day, settings)
    # One-time setup quests never gate the clear-the-board bonus: they claim on
    # a constant period (not this calendar period), so their claim could never
    # appear in the per-period ``paid`` set below, and a member shouldn't have
    # to do their once-ever bio to earn today's daily set bonus.
    board -= set(_setup_kinds_by_id(conn, guild_id, board))
    if not board:
        return 0
    paid = {
        int(r["quest_id"])
        for r in conn.execute(
            "SELECT DISTINCT quest_id FROM econ_quest_claims "
            "WHERE user_id = ? AND period = ? AND state = 'paid' "
            f"AND quest_id IN ({','.join('?' * len(board))})",
            (user_id, period, *board),
        )
    }
    if paid < board:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO econ_set_bonus (guild_id, user_id, qtype, period) "
        "VALUES (?, ?, ?, ?)",
        (guild_id, user_id, qtype, period),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    return apply_credit(
        conn,
        guild_id,
        user_id,
        bonus,
        "quest_bonus",
        meta={"qtype": qtype, "period": period},
        booster=False,
        multiplier=settings.booster_multiplier,
    )


def source_enabled(conn: sqlite3.Connection, guild_id: int, source: str) -> bool:
    """Whether a custom income source (trigger kind) is on for this guild.

    Absent row = enabled: sources default ON so a newly shipped kind works
    without a dashboard visit.
    """
    row = conn.execute(
        "SELECT enabled FROM econ_income_sources WHERE guild_id = ? AND source = ?",
        (guild_id, source),
    ).fetchone()
    return True if row is None else bool(row["enabled"])


def list_income_sources(conn: sqlite3.Connection, guild_id: int) -> dict[str, bool]:
    """Enabled state for every known trigger kind (absent = enabled)."""
    stored = {
        str(r["source"]): bool(r["enabled"])
        for r in conn.execute(
            "SELECT source, enabled FROM econ_income_sources WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    }
    return {kind: stored.get(kind, True) for kind in quests.TRIGGER_KINDS}


def set_income_source(
    conn: sqlite3.Connection, guild_id: int, source: str, enabled: bool
) -> None:
    if source not in quests.TRIGGER_KINDS:
        raise ValueError(f"unknown income source: {source!r}")
    conn.execute(
        """
        INSERT INTO econ_income_sources (guild_id, source, enabled, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (guild_id, source) DO UPDATE SET
            enabled = excluded.enabled, updated_at = excluded.updated_at
        """,
        (guild_id, source, 1 if enabled else 0, time.time()),
    )


def _trailing_period_counts(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    kind: str,
    qtype: str,
    local_day: str,
) -> list[int]:
    """The member's kind-activity totals for the trailing completed periods.

    daily → previous 4 days, weekly → previous 4 ISO weeks, monthly →
    previous 2 calendar months (the 70-day ledger window can't hold 4).
    Quiet periods count as 0 — the median should reflect real pace.
    """
    from datetime import date, timedelta

    day = date.fromisoformat(local_day)

    def _range_sum(start: date, end: date) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS s FROM econ_kind_activity "
            "WHERE guild_id = ? AND user_id = ? AND kind = ? "
            "AND local_day >= ? AND local_day < ?",
            (guild_id, user_id, kind, start.isoformat(), end.isoformat()),
        ).fetchone()
        return int(row["s"])

    if qtype == "daily":
        return [
            _range_sum(day - timedelta(days=n), day - timedelta(days=n - 1))
            for n in range(1, 5)
        ]
    if qtype == "weekly":
        monday = day - timedelta(days=day.weekday())
        return [
            _range_sum(monday - timedelta(weeks=n), monday - timedelta(weeks=n - 1))
            for n in range(1, 5)
        ]
    # monthly
    first = day.replace(day=1)
    out: list[int] = []
    end = first
    for _ in range(2):
        start = (end - timedelta(days=1)).replace(day=1)
        out.append(_range_sum(start, end))
        end = start
    return out


def resolve_member_target(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    quest: sqlite3.Row,
    *,
    period: str,
    local_day: str,
) -> int:
    """A member's target for a counted quest this period (spec: dynamic).

    Fixed ``target_count`` quests pass straight through. Band quests resolve
    once per (quest, member, period) and the result is STORED on the
    progress row — stable all period, and the wallet shows exactly what the
    fire path enforces. Resolution prefers the member's own pace (trailing
    period median of the kind × DYNAMIC_STRETCH, clamped to the band) and
    falls back to the deterministic Gaussian draw when they have fewer than
    2 active trailing periods of that kind.
    """
    target_count = int(quest["target_count"])
    tmin, tmax = int(quest["target_min"]), int(quest["target_max"])
    if not (0 < tmin < tmax):
        return target_count
    qid = int(quest["id"])
    row = conn.execute(
        "SELECT target FROM econ_quest_progress "
        "WHERE quest_id = ? AND user_id = ? AND period = ?",
        (qid, user_id, period),
    ).fetchone()
    if row is not None and row["target"]:
        return int(row["target"])

    import statistics as _stats

    kind = str(quest["trigger_kind"])
    counts = _trailing_period_counts(
        conn, guild_id, user_id, kind, str(quest["qtype"]), local_day,
    )
    if sum(1 for c in counts if c > 0) >= 2:
        median = float(_stats.median(counts))
        # Channel-scoped quest on a message-shaped kind: the member's kind
        # activity is all-channel, so scale their median by THEIR share of
        # traffic in the scoped channel — "send N messages in #the-meadow"
        # sizes to their meadow pace, not their whole-server pace. The band
        # clamp below still bounds the result either way.
        scope = quest["trigger_channel_id"]
        if scope is not None and kind in quests.CHANNEL_SHARE_KINDS:
            median *= channel_message_share(
                conn, guild_id, int(scope), local_day, user_id=user_id
            )
        target = quests.dynamic_target(median, tmin, tmax)
    else:
        target = quests.effective_target(
            target_count, tmin, tmax,
            user_id=user_id, quest_id=qid, period=period,
        )
    conn.execute(
        """
        INSERT INTO econ_quest_progress (quest_id, user_id, period, current, target)
        VALUES (?, ?, ?, 0, ?)
        ON CONFLICT (quest_id, user_id, period) DO UPDATE SET
            target = COALESCE(econ_quest_progress.target, excluded.target)
        """,
        (qid, user_id, period, target),
    )
    return target


def record_kind_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    kind: str,
    local_day: str,
) -> None:
    """Count one occurrence of a trigger kind for a member's guild-local day.

    Fires for EVERY occurrence — before the income-source switch and the
    personal-board filter — because ``econ_kind_activity`` measures what
    members actually do, not what happened to pay. Dynamic target sizing
    (personal trailing-period medians, community guild sums) reads it.
    """
    conn.execute(
        """
        INSERT INTO econ_kind_activity (guild_id, user_id, kind, local_day, count)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT (guild_id, user_id, kind, local_day) DO UPDATE SET
            count = count + 1
        """,
        (guild_id, user_id, kind, local_day),
    )


def prune_kind_activity(
    conn: sqlite3.Connection, guild_id: int, today: str, *, keep_days: int = 70
) -> None:
    """Drop activity rows older than the trailing window (day-roll hygiene).

    70 days ≈ 10 ISO weeks — enough for 4-week sizing windows with slack;
    lexicographic compare works because local_day is zero-padded ISO.
    """
    from datetime import date, timedelta

    cutoff = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
    conn.execute(
        "DELETE FROM econ_kind_activity WHERE guild_id = ? AND local_day < ?",
        (guild_id, cutoff),
    )
    # Same hygiene pass: conversation_starter's reply-count rows only need a
    # couple of weeks — a message drawing its third reply later than that is
    # conversation necromancy we're happy to miss.
    conn.execute(
        "DELETE FROM econ_msg_replies WHERE guild_id = ? AND created_at < ?",
        (guild_id, time.time() - 14 * 86400),
    )


def fire_trigger_inline(
    conn: sqlite3.Connection,
    guild_id: int,
    trigger_kind: str,
    user_id: int,
    *,
    occurrence: str | None,
    booster: bool = False,
    channel_ids: tuple[int, ...] | None = None,
) -> list[tuple[sqlite3.Row, ClaimOutcome]]:
    """Fire a trigger from a call site that already holds the main-DB conn.

    One-stop wrapper for module hooks (voice tick, starboard insert, invite
    record, bio save, pen-pal pairing, QOTD award): loads settings (no-op
    when the economy is off), derives the guild-local day, and fires inside
    a savepoint. Never raises, and a failure rolls back only the quest work —
    economy trouble must not break or dirty the host module's transaction.
    Sign-off claims filed here get no bank-channel card (no bot object); they
    still surface in the Bank Manager pending-claims table.
    """
    try:
        settings = load_econ_settings(conn, guild_id)
        if not settings.enabled:
            return []
        offset = get_tz_offset_hours(conn, guild_id)
        day = local_day_for(time.time(), offset)
        conn.execute("SAVEPOINT quest_fire")
        try:
            fired = fire_trigger_quests(
                conn,
                settings,
                guild_id,
                trigger_kind,
                user_id,
                local_day=day,
                occurrence=occurrence,
                booster=booster,
                channel_ids=channel_ids,
            )
        except Exception:
            conn.execute("ROLLBACK TO quest_fire")
            raise
        finally:
            conn.execute("RELEASE quest_fire")
        return fired
    except Exception:
        log.exception(
            "inline trigger %s failed for user %s in guild %s",
            trigger_kind, user_id, guild_id,
        )
        return []


def fire_trigger_quests(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    trigger_kind: str,
    user_id: int,
    *,
    local_day: str,
    occurrence: str | None,
    booster: bool,
    channel_ids: tuple[int, ...] | None = None,
) -> list[tuple[sqlite3.Row, ClaimOutcome]]:
    """Claim every active quest with this trigger kind for one member.

    The claim period comes from the quest's own cadence: daily/weekly use the
    calendar period for ``local_day``; event quests use the per-occurrence key
    (skipped when the firing site has no stable ``occurrence`` id — paying an
    unkeyed event would be unbounded). Per-period repeats fall out silently
    via the claim collision; anything paid or filed is returned so the caller
    can announce or post sign-off cards.

    ``channel_ids`` is the firing message's channel (and thread parent) for
    kinds with channel context: a quest with ``trigger_channel_id`` set only
    fires when it matches, and never fires from a caller with no channel
    context. Returns nothing when the source is disabled on the Income
    Sources page.
    """
    record_kind_activity(conn, guild_id, user_id, trigger_kind, local_day)
    if not source_enabled(conn, guild_id, trigger_kind):
        return []
    out: list[tuple[sqlite3.Row, ClaimOutcome]] = []
    # A member only earns a daily/weekly/monthly quest when it's on *their*
    # personal board this period — compute each cadence's board once.
    boards: dict[str, set[int]] = {}
    for quest in list_kind_triggered_quests(conn, guild_id, trigger_kind):
        scope = quest["trigger_channel_id"]
        if scope is not None and (
            channel_ids is None or int(scope) not in channel_ids
        ):
            continue
        qtype = str(quest["qtype"])
        if trigger_kind in quests.SETUP_QUEST_KINDS and qtype in quests.BOARD_CADENCES:
            # One-time setup quest living in a board pool (bio/birthday): claim
            # once ever, keyed to a constant period, and independent of the
            # board draw. A lifetime action can't wait for a lucky daily roll,
            # so the completing member always gets paid even if it wasn't drawn
            # today; and re-saving must not re-earn it — claim_quest's collision
            # on this constant key makes the second save a silent no-op. (The
            # board draw still controls *visibility* via _drop_completed_setup.)
            period = quests.occurrence_period(trigger_kind, occurrence or "set")
        elif qtype == "event":
            if occurrence is None:
                continue
            period = quests.occurrence_period(trigger_kind, occurrence)
        else:
            if qtype not in boards:
                boards[qtype] = assigned_board_ids(
                    conn, guild_id, user_id, qtype, local_day, settings
                )
            if int(quest["id"]) not in boards[qtype]:
                continue  # not on this member's board this period
            period = quests.quest_period(qtype, local_day)
            target = resolve_member_target(
                conn, guild_id, user_id, quest,
                period=period, local_day=local_day,
            )
            if target > 1:
                # Counted quest: each distinct occurrence bumps the period's
                # progress; the claim only fires when the target is reached.
                if occurrence is None:
                    continue
                if not _bump_progress(
                    conn, int(quest["id"]), user_id, period, occurrence, target
                ):
                    continue
        try:
            outcome = claim_quest(
                conn,
                settings,
                guild_id,
                int(quest["id"]),
                user_id,
                period=period,
                booster=booster,
            )
        except ValueError:
            continue  # already claimed this period/occurrence, or window closed
        out.append((quest, outcome))
    _bump_community_kind(conn, guild_id, trigger_kind, user_id, channel_ids)
    return out


def _bump_community_kind(
    conn: sqlite3.Connection,
    guild_id: int,
    trigger_kind: str,
    user_id: int,
    channel_ids: tuple[int, ...] | None,
) -> None:
    """Advance any active auto-tracking community quest of this kind.

    Guild-wide by design — deliberately NOT filtered by personal boards, so
    every member's action counts toward the shared goal even when the kind
    isn't on their board this period. Per-member contribution rows feed the
    top-contributor bonus and the "N members contributed" line.
    """
    rows = conn.execute(
        """
        SELECT id, trigger_channel_id, community_target
        FROM econ_quests
        WHERE guild_id = ? AND qtype = 'community' AND active = 1
          AND trigger_kind = ?
        """,
        (guild_id, trigger_kind),
    ).fetchall()
    now = time.time()
    for quest in rows:
        scope = quest["trigger_channel_id"]
        if scope is not None and (
            channel_ids is None or int(scope) not in channel_ids
        ):
            continue
        target = int(quest["community_target"] or 0)
        if target <= 0:
            continue  # not activated through the scheduler yet
        qid = int(quest["id"])
        conn.execute(
            """
            INSERT INTO econ_community_progress (quest_id, current)
            VALUES (?, 1)
            ON CONFLICT(quest_id) DO UPDATE SET current = current + 1
            """,
            (qid,),
        )
        conn.execute(
            """
            INSERT INTO econ_community_contrib (quest_id, user_id, count)
            VALUES (?, ?, 1)
            ON CONFLICT(quest_id, user_id) DO UPDATE SET count = count + 1
            """,
            (qid, user_id),
        )
        conn.execute(
            """
            UPDATE econ_community_progress
            SET completed_at = ?
            WHERE quest_id = ? AND completed_at IS NULL AND current >= ?
            """,
            (now, qid, target),
        )
        # Progress moved without a payout — still worth a live-panel repaint.
        live_signal.mark_dirty(guild_id)


def _bump_progress(
    conn: sqlite3.Connection,
    quest_id: int,
    user_id: int,
    period: str,
    occurrence: str,
    target: int,
) -> bool:
    """Count one occurrence toward a counted quest; True when target reached.

    The marks table dedupes occurrences (a gateway replay or repeat event
    can't double-count), so the progress row only moves on genuinely new
    ones. Progress past the target keeps a mark but stops incrementing —
    the paid-claim index is the real payout guard either way.
    """
    marked = conn.execute(
        """
        INSERT OR IGNORE INTO econ_quest_progress_marks
            (quest_id, user_id, period, occurrence)
        VALUES (?, ?, ?, ?)
        """,
        (quest_id, user_id, period, occurrence),
    )
    if (marked.rowcount or 0) == 0:
        # Same occurrence seen before: it may have been the one that hit the
        # target (claim path could still be pending sign-off) — never re-pay,
        # never re-count.
        return False
    conn.execute(
        """
        INSERT INTO econ_quest_progress (quest_id, user_id, period, current)
        VALUES (?, ?, ?, 1)
        ON CONFLICT (quest_id, user_id, period) DO UPDATE SET
            current = current + 1
        """,
        (quest_id, user_id, period),
    )
    row = conn.execute(
        """
        SELECT current FROM econ_quest_progress
        WHERE quest_id = ? AND user_id = ? AND period = ?
        """,
        (quest_id, user_id, period),
    ).fetchone()
    return row is not None and int(row["current"]) >= target


def get_progress(
    conn: sqlite3.Connection, quest_id: int, user_id: int, period: str
) -> int:
    """A member's progress count for a counted quest's period (0 if none)."""
    row = conn.execute(
        """
        SELECT current FROM econ_quest_progress
        WHERE quest_id = ? AND user_id = ? AND period = ?
        """,
        (quest_id, user_id, period),
    ).fetchone()
    return int(row["current"]) if row else 0


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
        if quest["qtype"] == "event":
            kinds = [
                str(r["trigger_kind"])
                for r in conn.execute(
                    """
                    SELECT trigger_kind FROM econ_quests
                    WHERE guild_id = ? AND active = 1 AND qtype = 'event'
                      AND id != ?
                    """,
                    (guild_id, quest_id),
                ).fetchall()
            ]
            if not quests.can_activate_event(kinds, str(quest["trigger_kind"])):
                raise SlotLimitError(
                    "an event quest with this trigger is already active"
                )
        else:
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
    """Claim a daily/weekly/event quest for a member in a given period.

    Daily/weekly callers pass the calendar period from ``quest_period``;
    event-quest callers (the trigger listeners) pass their per-occurrence
    key (e.g. ``occurrence_period(kind, game_id)``).

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
    maybe_pay_set_bonus(
        conn, settings, guild_id, user_id, str(quest["qtype"]), period
    )
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
    guild_id = int(quest["guild_id"])
    reward_xp = int(quest["reward_xp"])
    if reward_xp > 0:
        # XP rides every quest payout (instant + approved sign-off — both
        # come through here). No booster multiplier: the ×1.5 is a currency
        # patron bonus, and minting XP would distort the level curve. Level
        # progression lands in the DB; the announcement, if any, happens on
        # the member's next ordinary XP award.
        apply_xp_award(
            conn,
            guild_id,
            user_id,
            float(reward_xp),
            event_source="quest",
            event_timestamp=time.time(),
            settings=load_xp_settings(conn, guild_id),
        )
    reward = int(quest["reward"])
    if reward < 1:
        return 0
    meta: dict[str, object] = {"quest_id": int(quest["id"]), "claim_id": claim_id}
    kind = str(quest["trigger_kind"] or "")
    if kind:
        # ⚡ Weekly spotlight: this week's featured kind pays double. Checked
        # at credit time, so a sign-off approved after the week flips pays at
        # the approval week's rate — acceptable drift for an advisory boost.
        offset = get_tz_offset_hours(conn, guild_id)
        week = quests.iso_week_for(local_day_for(time.time(), offset))
        if spotlight_kind(conn, guild_id, week) == kind:
            reward *= 2
            meta["spotlight"] = True
    return apply_credit(
        conn,
        guild_id,
        user_id,
        reward,
        "quest",
        meta=meta,
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
    maybe_pay_set_bonus(
        conn, settings, int(claim["guild_id"]), user_id,
        str(quest["qtype"]), str(claim["period"]),
    )
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
    owner = conn.execute(
        "SELECT guild_id FROM econ_quests WHERE id = ?", (quest_id,)
    ).fetchone()
    if owner is not None:
        live_signal.mark_dirty(int(owner["guild_id"]))
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
          AND q.trigger_kind = ''
          AND p.completed_at IS NOT NULL AND p.settled_at IS NULL
        ORDER BY q.id
        """,
        (guild_id,),
    ).fetchall()


def list_active_community_kind_quests(
    conn: sqlite3.Connection, guild_id: int
) -> list[sqlite3.Row]:
    """Active auto-tracking (kind-carrying) community quests for a guild."""
    return conn.execute(
        """
        SELECT q.*, p.current, p.completed_at, p.settled_at,
               p.notified_tier, p.final_notice_sent
        FROM econ_quests q
        LEFT JOIN econ_community_progress p ON p.quest_id = q.id
        WHERE q.guild_id = ? AND q.qtype = 'community' AND q.active = 1
          AND q.trigger_kind != ''
        ORDER BY q.id
        """,
        (guild_id,),
    ).fetchall()


def next_community_weekly(
    conn: sqlite3.Connection, guild_id: int
) -> sqlite3.Row | None:
    """The library's next community weekly: inactive, kind-carrying, least
    recently run ('' sorts first, so never-run quests lead the rotation)."""
    return conn.execute(
        """
        SELECT * FROM econ_quests
        WHERE guild_id = ? AND qtype = 'community' AND active = 0
          AND trigger_kind != ''
        ORDER BY last_run_week ASC, id ASC
        LIMIT 1
        """,
        (guild_id,),
    ).fetchone()


def channel_message_share(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    local_day: str,
    *,
    user_id: int | None = None,
) -> float:
    """The channel's fraction of messages over the trailing 28 days.

    Guild-wide by default; pass ``user_id`` for the member's own share.
    Read from ``processed_messages`` (the permanent archive — kind activity
    has no channel dimension). Zero traffic in the window returns 1.0: no
    data means no scaling, and the band/floor clamps still protect. Soft
    edge: thread messages archive under the thread's id while scoped fires
    credit the parent, so thready channels read slightly LOW — targets err
    forgiving, never impossible.
    """
    from datetime import date, timezone
    from datetime import datetime as dt

    end_d = date.fromisoformat(local_day)
    end_ts = dt(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc).timestamp()
    start_ts = end_ts - 28 * 86400
    member_clause = "AND user_id = ?" if user_id is not None else ""
    member_args: tuple = (user_id,) if user_id is not None else ()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM processed_messages "
        f"WHERE guild_id = ? {member_clause} "
        f"AND created_at >= ? AND created_at < ?",
        (guild_id, *member_args, start_ts, end_ts),
    ).fetchone()
    if int(total["n"]) == 0:
        return 1.0
    in_channel = conn.execute(
        f"SELECT COUNT(*) AS n FROM processed_messages "
        f"WHERE guild_id = ? AND channel_id = ? {member_clause} "
        f"AND created_at >= ? AND created_at < ?",
        (guild_id, channel_id, *member_args, start_ts, end_ts),
    ).fetchone()
    return int(in_channel["n"]) / int(total["n"])


def auto_size_community_target(
    conn: sqlite3.Connection,
    guild_id: int,
    kind: str,
    local_day: str,
    *,
    channel_id: int | None = None,
) -> int:
    """Target from the guild's trailing 28 full days of this kind's activity.

    Fully automatic by design decision (2026-07-18 Q&A) — no manual override.
    Cold kinds fall to the floor target (quests.community_auto_target), which
    keeps a first-week goal achievable rather than impossible. A
    channel-scoped quest on a message-shaped kind
    (quests.CHANNEL_SHARE_KINDS) scales the guild total by the channel's
    message share first — kind activity has no channel dimension, and an
    unscaled target would make the top tiers mathematically unreachable
    (e.g. a 43%-of-traffic channel sized against 100% of the activity).
    """
    from datetime import date, timedelta

    end = date.fromisoformat(local_day)  # exclusive: today is partial
    start = end - timedelta(days=28)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(count), 0) AS total FROM econ_kind_activity
        WHERE guild_id = ? AND kind = ? AND local_day >= ? AND local_day < ?
        """,
        (guild_id, kind, start.isoformat(), end.isoformat()),
    ).fetchone()
    total = int(row["total"])
    if channel_id is not None and kind in quests.CHANNEL_SHARE_KINDS:
        total = round(
            total * channel_message_share(conn, guild_id, channel_id, local_day)
        )
    return quests.community_auto_target(total)


def activate_community_weekly(
    conn: sqlite3.Connection,
    guild_id: int,
    quest_id: int,
    *,
    target: int,
    week: str,
) -> None:
    """Open a community weekly's run: size it, reset per-run state, activate.

    Contribution and tier-payout rows are per-run — cleared here (same
    transaction) so a library quest can re-run in a later week and pay
    again. Exactly-once only has to hold *within* a run, where settlement
    replays share the reservation rows.
    """
    conn.execute(
        """
        UPDATE econ_quests
        SET active = 1, community_target = ?, last_run_week = ?
        WHERE id = ? AND guild_id = ?
        """,
        (target, week, quest_id, guild_id),
    )
    conn.execute("DELETE FROM econ_community_contrib WHERE quest_id = ?", (quest_id,))
    conn.execute(
        "DELETE FROM econ_community_tier_payouts WHERE quest_id = ?", (quest_id,)
    )
    conn.execute(
        """
        INSERT INTO econ_community_progress
            (quest_id, current, completed_at, settled_at, notified_tier,
             final_notice_sent)
        VALUES (?, 0, NULL, NULL, 0, 0)
        ON CONFLICT(quest_id) DO UPDATE SET
            current = 0, completed_at = NULL, settled_at = NULL,
            notified_tier = 0, final_notice_sent = 0
        """,
        (quest_id,),
    )


def community_contrib_summary(
    conn: sqlite3.Connection, quest_id: int, *, top_n: int = 3
) -> tuple[int, list[tuple[int, int]]]:
    """(contributor count, top-N [(user_id, count)]) for a community run."""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_community_contrib "
        "WHERE quest_id = ? AND count > 0",
        (quest_id,),
    ).fetchone()
    top = conn.execute(
        """
        SELECT user_id, count FROM econ_community_contrib
        WHERE quest_id = ? AND count > 0
        ORDER BY count DESC, user_id ASC
        LIMIT ?
        """,
        (quest_id, top_n),
    ).fetchall()
    return int(total["n"]), [(int(r["user_id"]), int(r["count"])) for r in top]


def settle_community_weekly(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    quest: sqlite3.Row,
    member_boosters: dict[int, bool],
) -> dict[str, object]:
    """Close a community weekly's run: pay crossed tiers + bonus, deactivate.

    Each crossed tier pays the quest's flat ``reward`` to every member in
    ``member_boosters`` (the loop passes 30d-actives), exactly-once via the
    per-(quest, tier, user) reservation rows — the settle_community_quest
    pattern. Tier 0 rows reserve the top-contributor bonus (reward // 2,
    top 3 by contribution). Returns the resolution summary for the beat
    sheet. Idempotent: a replay pays only what it missed.
    """
    qid = int(quest["id"])
    reward = int(quest["reward"])
    target = int(quest["community_target"] or 0)
    row = conn.execute(
        "SELECT current FROM econ_community_progress WHERE quest_id = ?", (qid,)
    ).fetchone()
    current = int(row["current"]) if row else 0
    crossed = quests.community_tiers_crossed(current, target)

    paid_members = 0
    for tier in range(1, crossed + 1):
        for user_id, booster in member_boosters.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO econ_community_tier_payouts "
                "(quest_id, tier, user_id) VALUES (?, ?, ?)",
                (qid, tier, user_id),
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
                    meta={"quest_id": qid, "tier": tier},
                    booster=booster,
                    multiplier=settings.booster_multiplier,
                )
            paid_members += 1

    # Anonymous kinds pay flat tiers only: surfacing the top confessors /
    # repliers / whisperers (in the bonus ledger or the paste-ready beat
    # sheet) would deanonymize the feed the kind exists to protect.
    anonymous = str(quest["trigger_kind"] or "") in quests.ANON_COMMUNITY_KINDS
    contributors, top = community_contrib_summary(conn, qid)
    if anonymous:
        top = []
    bonus = 0 if anonymous else reward // 2
    bonus_paid: list[int] = []
    if crossed > 0 and bonus >= 1:
        for user_id, _count in top:
            cur = conn.execute(
                "INSERT OR IGNORE INTO econ_community_tier_payouts "
                "(quest_id, tier, user_id) VALUES (?, 0, ?)",
                (qid, user_id),
            )
            if (cur.rowcount or 0) == 0:
                continue
            apply_credit(
                conn,
                guild_id,
                user_id,
                bonus,
                "quest_community_bonus",
                meta={"quest_id": qid},
                booster=member_boosters.get(user_id, False),
                multiplier=settings.booster_multiplier,
            )
            bonus_paid.append(user_id)

    now = time.time()
    conn.execute(
        "UPDATE econ_community_progress SET settled_at = ? WHERE quest_id = ?",
        (now, qid),
    )
    conn.execute(
        "UPDATE econ_quests SET active = 0 WHERE id = ? AND guild_id = ?",
        (qid, guild_id),
    )
    return {
        "quest_id": qid,
        "title": str(quest["title"]),
        "current": current,
        "target": target,
        "tiers_crossed": crossed,
        "reward_per_tier": reward,
        "paid_member_tiers": paid_members,
        "contributors": contributors,
        "top_contributors": top,
        "bonus": bonus,
        "bonus_paid": bonus_paid,
        "anonymous": anonymous,
    }


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
