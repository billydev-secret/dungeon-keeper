"""Community Bounty — crowdfunded, mod-awarded task pots (migration 109).

Anyone posts a freeform task and seeds a pot; anyone chips in; a mod awards the
pot to the winner minus a house rake that evaporates; an unawarded bounty
refunds every contributor. The economy's first *many-payer* mechanic, so each
contribution is its own ``econ_bounty_contributions`` row keyed by bounty — the
pot is ``SUM(non-refunded contributions)`` (never stored, can't drift) and
refunds are per-member and exactly-once.

Pure DB, no Discord — the caller owns the board card and DMs, matching the pin
and sponsor services.

Money::

    contribute  -> apply_debit  (bounty_stake)   escrow into the pot
    award       -> apply_credit (bounty_payout)  winner gets pot - rake
                   (the rake is escrow never credited back = the burn)
    cancel/     -> apply_credit (bounty_refund)  each contribution back,
    expire         exactly-once (refunded_at guard), NEVER raked
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
)

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

MAX_TITLE_LEN = 100
MAX_DESC_LEN = 500
MIN_TITLE_LEN = 3

STAKE_KIND = "bounty_stake"
PAYOUT_KIND = "bounty_payout"
REFUND_KIND = "bounty_refund"


def bounty_enabled(settings: EconSettings) -> bool:
    """On only when a board channel is set (nowhere to post otherwise)."""
    return int(settings.bounty_channel_id) > 0


def bounty_min_stake(settings: EconSettings) -> int:
    """The floor for the opening stake and each chip-in (at least 1)."""
    return max(1, int(settings.bounty_min_stake))


def bounty_rake_pct(settings: EconSettings) -> int:
    """House cut on award, clamped to 0–100. 0 = the winner takes the whole pot."""
    return max(0, min(100, int(settings.bounty_rake_pct)))


@dataclass(frozen=True)
class BountyOutcome:
    """A freshly-posted bounty: its id and the opening stake escrowed."""

    bounty_id: int
    stake: int


def pot_of(conn: sqlite3.Connection, bounty_id: int) -> int:
    """The live pot: the sum of this bounty's non-refunded contributions."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS pot FROM econ_bounty_contributions "
        "WHERE bounty_id = ? AND refunded_at IS NULL",
        (bounty_id,),
    ).fetchone()
    return int(row["pot"])


def contributor_count(conn: sqlite3.Connection, bounty_id: int) -> int:
    """How many distinct members have live contributions in the pot."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM econ_bounty_contributions "
        "WHERE bounty_id = ? AND refunded_at IS NULL",
        (bounty_id,),
    ).fetchone()
    return int(row["n"])


def open_count_for(conn: sqlite3.Connection, guild_id: int, poster_id: int) -> int:
    """How many open bounties this member currently has posted."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_bounties "
        "WHERE guild_id = ? AND poster_id = ? AND state = 'open'",
        (guild_id, poster_id),
    ).fetchone()
    return int(row["n"])


def _escrow(
    conn: sqlite3.Connection,
    guild_id: int,
    bounty_id: int,
    user_id: int,
    amount: int,
    now: float,
) -> bool:
    """Debit ``amount`` into the pot and record the contribution. False if broke."""
    if not apply_debit(
        conn, guild_id, user_id, amount, STAKE_KIND, meta={"bounty_id": bounty_id}
    ):
        return False
    conn.execute(
        "INSERT INTO econ_bounty_contributions "
        "(bounty_id, guild_id, user_id, amount, created_at) VALUES (?, ?, ?, ?, ?)",
        (bounty_id, guild_id, user_id, amount, now),
    )
    return True


def create_bounty(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    poster_id: int,
    *,
    title: str,
    description: str,
    stake: int,
    now: float | None = None,
) -> BountyOutcome:
    """Open a bounty, escrowing the poster's opening stake. ValueError = member text."""
    if not bounty_enabled(settings):
        raise ValueError("Bounties aren't enabled here.")
    title = " ".join(title.split())
    description = "\n".join(line.rstrip() for line in description.splitlines()).strip()
    if len(title) < MIN_TITLE_LEN:
        raise ValueError("Give your bounty a title (a few words at least).")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"Titles are limited to {MAX_TITLE_LEN} characters.")
    if len(description) > MAX_DESC_LEN:
        raise ValueError(f"Descriptions are limited to {MAX_DESC_LEN} characters.")
    floor = bounty_min_stake(settings)
    if stake < floor:
        raise ValueError(f"The opening stake must be at least {floor}.")
    cap = max(0, int(settings.bounty_max_open))
    if cap and open_count_for(conn, guild_id, poster_id) >= cap:
        raise ValueError(
            f"You already have {cap} open bounties — award or cancel one first."
        )

    # Affordability is checked BEFORE the insert so an unaffordable post is a
    # zero-write (the row would otherwise sit until the caller's transaction
    # unwinds). Within this one transaction the balance can't move between the
    # check and the debit, so the escrow below can't then fail on funds.
    now = time.time() if now is None else now
    have = get_balance(conn, guild_id, poster_id)
    if have < stake:
        unit = settings.currency_plural or "coins"
        raise ValueError(
            f"Posting this bounty costs {stake} {unit} up front — you have {have}."
        )
    days = max(0, int(settings.bounty_expire_days))
    expires_at = now + days * 86400.0 if days > 0 else None
    cur = conn.execute(
        "INSERT INTO econ_bounties "
        "(guild_id, poster_id, title, description, state, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (guild_id, poster_id, title, description, now, expires_at),
    )
    bounty_id = int(cur.lastrowid or 0)
    if not _escrow(conn, guild_id, bounty_id, poster_id, stake, now):
        # Defensive: the balance was just confirmed, so this shouldn't fire — but
        # if it does, raise so the caller's transaction rolls back the insert.
        raise ValueError("Couldn't take the opening stake — try again.")
    # shop_purchase quest trigger (one-time setup kind); deferred import.
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", poster_id, occurrence="set")
    return BountyOutcome(bounty_id=bounty_id, stake=stake)


def contribute(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    bounty_id: int,
    user_id: int,
    amount: int,
    *,
    now: float | None = None,
) -> int:
    """Chip ``amount`` into an open bounty's pot. Returns the new pot. ValueError = text."""
    floor = bounty_min_stake(settings)
    if amount < floor:
        raise ValueError(f"Chip in at least {floor}.")
    row = get_bounty(conn, bounty_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("That bounty no longer exists.")
    if str(row["state"]) != "open":
        raise ValueError(f"That bounty is {row['state']} — you can't add to it now.")
    now = time.time() if now is None else now
    if not _escrow(conn, guild_id, bounty_id, user_id, amount, now):
        have = get_balance(conn, guild_id, user_id)
        unit = settings.currency_plural or "coins"
        raise ValueError(f"That's {amount} {unit} — you have {have}.")
    return pot_of(conn, bounty_id)


@dataclass(frozen=True)
class AwardResult:
    """An awarded bounty: the fresh row, the winner, and how the pot split."""

    bounty: sqlite3.Row
    winner_id: int
    payout: int
    rake: int


def award_bounty(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    bounty_id: int,
    *,
    winner_id: int,
    resolver_id: int,
    now: float | None = None,
) -> AwardResult:
    """Award the pot to ``winner_id`` minus the house rake. ValueError = member text.

    The rake is ``floor(pot × rake_pct / 100)`` and simply isn't credited back to
    anyone — the escrowed coins that fund it evaporate. An empty pot can't be
    awarded (nothing to pay).
    """
    row = get_bounty(conn, bounty_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("That bounty no longer exists.")
    if str(row["state"]) != "open":
        raise ValueError(f"That bounty is already {row['state']}.")
    pot = pot_of(conn, bounty_id)
    if pot <= 0:
        raise ValueError("That bounty's pot is empty — nothing to award.")

    now = time.time() if now is None else now
    rake = pot * bounty_rake_pct(settings) // 100
    payout = pot - rake
    cur = conn.execute(
        "UPDATE econ_bounties SET state = 'awarded', winner_id = ?, payout = ?, "
        "rake_amount = ?, resolver_id = ?, resolved_at = ? "
        "WHERE id = ? AND state = 'open'",
        (winner_id, payout, rake, resolver_id, now, bounty_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("That bounty was just resolved by someone else.")
    if payout > 0:
        apply_credit(
            conn, guild_id, winner_id, payout, PAYOUT_KIND,
            meta={"bounty_id": bounty_id, "rake": rake}, booster=False,
        )
    fresh = get_bounty(conn, bounty_id)
    assert fresh is not None
    return AwardResult(bounty=fresh, winner_id=winner_id, payout=payout, rake=rake)


def _refund_all(conn: sqlite3.Connection, bounty_id: int, reason: str) -> list[int]:
    """Refund every not-yet-refunded contribution exactly once. Returns user ids paid."""
    rows = conn.execute(
        "SELECT * FROM econ_bounty_contributions "
        "WHERE bounty_id = ? AND refunded_at IS NULL",
        (bounty_id,),
    ).fetchall()
    now = time.time()
    paid: list[int] = []
    for row in rows:
        cur = conn.execute(
            "UPDATE econ_bounty_contributions SET refunded_at = ? "
            "WHERE id = ? AND refunded_at IS NULL",
            (now, int(row["id"])),
        )
        if (cur.rowcount or 0) == 0:
            continue
        apply_credit(
            conn, int(row["guild_id"]), int(row["user_id"]), int(row["amount"]),
            REFUND_KIND, meta={"bounty_id": bounty_id, "reason": reason}, booster=False,
        )
        paid.append(int(row["user_id"]))
    return paid


def cancel_bounty(
    conn: sqlite3.Connection,
    guild_id: int,
    bounty_id: int,
    *,
    resolver_id: int,
    now: float | None = None,
) -> tuple[sqlite3.Row, list[int]]:
    """Cancel an open bounty and refund every contributor. Returns (row, refunded ids)."""
    row = get_bounty(conn, bounty_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("That bounty no longer exists.")
    if str(row["state"]) != "open":
        raise ValueError(f"That bounty is already {row['state']}.")
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE econ_bounties SET state = 'cancelled', resolver_id = ?, resolved_at = ? "
        "WHERE id = ? AND state = 'open'",
        (resolver_id, now, bounty_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("That bounty was just resolved by someone else.")
    refunded = _refund_all(conn, bounty_id, "cancelled")
    fresh = get_bounty(conn, bounty_id)
    assert fresh is not None
    return fresh, refunded


@dataclass(frozen=True)
class ExpiredBounty:
    """An expired bounty and the contributors refunded (for post-commit DMs/cards)."""

    bounty: sqlite3.Row
    refunded_user_ids: list[int]


def expire_bounties(
    conn: sqlite3.Connection, settings: EconSettings, guild_id: int, *, now: float
) -> list[ExpiredBounty]:
    """Expire and refund open bounties nobody awarded within the window."""
    days = max(0, int(settings.bounty_expire_days))
    if days <= 0:
        return []
    due = conn.execute(
        "SELECT * FROM econ_bounties WHERE guild_id = ? AND state = 'open' "
        "AND expires_at IS NOT NULL AND expires_at <= ?",
        (guild_id, now),
    ).fetchall()
    out: list[ExpiredBounty] = []
    for row in due:
        cur = conn.execute(
            "UPDATE econ_bounties SET state = 'expired', resolved_at = ? "
            "WHERE id = ? AND state = 'open'",
            (now, int(row["id"])),
        )
        if (cur.rowcount or 0) == 0:
            continue
        refunded = _refund_all(conn, int(row["id"]), "expired")
        fresh = get_bounty(conn, int(row["id"]))
        assert fresh is not None
        out.append(ExpiredBounty(bounty=fresh, refunded_user_ids=refunded))
    return out


def get_bounty(conn: sqlite3.Connection, bounty_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_bounties WHERE id = ?", (bounty_id,)
    ).fetchone()


def list_bounties(
    conn: sqlite3.Connection, guild_id: int, state: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    limit = min(max(limit, 1), 500)
    if state:
        return conn.execute(
            "SELECT * FROM econ_bounties WHERE guild_id = ? AND state = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (guild_id, state, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM econ_bounties WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()


def set_bounty_card(
    conn: sqlite3.Connection, bounty_id: int, channel_id: int, message_id: int
) -> None:
    conn.execute(
        "UPDATE econ_bounties SET card_channel_id = ?, card_message_id = ? WHERE id = ?",
        (channel_id, message_id, bounty_id),
    )
