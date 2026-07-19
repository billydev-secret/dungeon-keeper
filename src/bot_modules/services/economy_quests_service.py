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

import logging

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.xp_system import apply_xp_award, load_xp_settings
from bot_modules.economy import quests
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
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
    cadences return an empty set, as does a cadence sized to 0.
    """
    sizes = board_sizes(settings) if settings is not None else None
    n = quests.board_size(qtype, sizes)
    if n <= 0 or not quests.has_board(qtype):
        return set()
    pool = list_active_pool_ids(conn, guild_id, qtype)
    idx = quests.period_index(qtype, local_day)
    return set(quests.assigned_quest_ids(pool, user_id, idx, n))


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
        if qtype == "event":
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
            target = quests.effective_target(
                int(quest["target_count"]),
                int(quest["target_min"]),
                int(quest["target_max"]),
                user_id=user_id,
                quest_id=int(quest["id"]),
                period=period,
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
    return apply_credit(
        conn,
        guild_id,
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


def auto_size_community_target(
    conn: sqlite3.Connection, guild_id: int, kind: str, local_day: str
) -> int:
    """Target from the guild's trailing 28 full days of this kind's activity.

    Fully automatic by design decision (2026-07-18 Q&A) — no manual override.
    Cold kinds fall to the floor target (quests.community_auto_target), which
    keeps a first-week goal achievable rather than impossible.
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
    return quests.community_auto_target(int(row["total"]))


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

    contributors, top = community_contrib_summary(conn, qid)
    bonus = reward // 2
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
