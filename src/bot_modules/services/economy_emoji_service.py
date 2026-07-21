"""Emoji sponsorship — pay weekly to keep a custom emoji in the server.

Economy sinks round 3, stage 4 (spec §6, migration 092). The submission queue
is the sponsored-QOTD shape (``economy_qotd_sponsor_service``): money taken at
submit, deny/cancel/expiry refund exactly-once via the ``refunded_at``
predicate, one submission in flight per member via a partial unique index.
The difference is what approval produces: instead of a post queue, the row
graduates into an ordinary ``econ_rentals`` row (``perk = 'emoji'``), so
weekly billing, the grace window, and lapse all ride the existing rental
engine — a lapse deletes the emoji (the loop's post-commit effect).

Approval is two-phase because the upload is a Discord side effect:

    pending ──approve (claim)──> approved ──upload ok──> live (+ rental)
       │                            │
       │                            └──upload failed──> denied (refund)
       ├──deny──> denied (refund)
       ├──cancel (member)──> cancelled (refund)
       └──expire──> expired (refund)

The ``approved`` claim happens in its own transaction BEFORE the upload
(claim-before-side-effect, the scheduled-games pattern), so two racing
resolvers can't both upload; ``finalize_upload`` / ``fail_upload`` land the
outcome afterwards. A crash between upload and finalize leaves an
``approved`` row with no ``emoji_id`` — visible on the dashboard queue, and
resolved by hand (deny refunds it; the orphan emoji is deleted in Discord's
own UI). The escrowed submit debit covers the FIRST week, so the rental's
``next_bill_at`` starts one week after the upload.

Ledger kinds: ``emoji_sponsor`` (escrow debit at submit),
``emoji_sponsor_refund`` (plain credit, never boosted). Renewals bill the
ordinary ``rental`` kind at the current ``price_emoji`` /
``price_emoji_animated``.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
)
from bot_modules.services.voice_master_service import name_is_blocked

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

# Discord's own emoji-name rule.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
# Discord rejects emoji images over 256 KiB.
MAX_IMAGE_BYTES = 256 * 1024

_OPEN_STATES = ("pending", "approved", "live")
SPEND_KIND = "emoji_sponsor"
REFUND_KIND = "emoji_sponsor_refund"


@dataclass(frozen=True)
class EmojiSubmitOutcome:
    submission_id: int
    price: int


def emoji_price(settings: EconSettings, *, animated: bool) -> int:
    return max(
        0,
        int(
            settings.price_emoji_animated if animated else settings.price_emoji
        ),
    )


def sponsoring_enabled(settings: EconSettings) -> bool:
    """Sponsoring is off when the static price is 0, like the other sinks."""
    return int(settings.price_emoji) > 0


def open_submission(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's in-flight submission (pending/approved/live), if any."""
    placeholders = ", ".join("?" for _ in _OPEN_STATES)
    return conn.execute(
        "SELECT * FROM econ_emoji_submissions "
        f"WHERE guild_id = ? AND user_id = ? AND state IN ({placeholders})",
        (guild_id, user_id, *_OPEN_STATES),
    ).fetchone()


def open_submission_count(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many sponsorships currently hold a slot (pending/approved/live)."""
    placeholders = ", ".join("?" for _ in _OPEN_STATES)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM econ_emoji_submissions "
        f"WHERE guild_id = ? AND state IN ({placeholders})",
        (guild_id, *_OPEN_STATES),
    ).fetchone()
    return int(row["c"])


def validate_emoji_name(
    name: str,
    *,
    blocklist_patterns: list[str],
    taken_names: set[str],
) -> str:
    """Return the cleaned name or raise ValueError with member-facing text.

    ``taken_names`` is every name already claimed — the guild's existing
    emojis plus open submissions (the caller collects both; the partial
    unique index backstops the race anyway). Comparison is case-insensitive
    to match how Discord resolves ``:name:`` typing.
    """
    cleaned = name.strip().strip(":")
    if not _NAME_RE.match(cleaned):
        raise ValueError(
            "Emoji names are 2–32 characters of letters, numbers, and "
            "underscores."
        )
    if name_is_blocked(cleaned, blocklist_patterns):
        raise ValueError("That name isn't allowed here.")
    if cleaned.lower() in {t.lower() for t in taken_names}:
        raise ValueError("That emoji name is already taken here.")
    return cleaned


def submit_sponsorship(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    *,
    name: str,
    image_path: str,
    animated: bool,
    blocklist_patterns: list[str],
    taken_names: set[str],
    guild_slots_free: bool,
) -> EmojiSubmitOutcome:
    """Charge for and queue one emoji sponsorship. ValueError = member-facing.

    Validation runs before the debit and the debit before the insert, so a
    rejected submission never costs anything and a failed insert never
    strands a payment (all in the caller's transaction).
    ``guild_slots_free`` is the caller's live Discord check (the guild has a
    free emoji slot of the right kind AND the sponsor cap has room) — this
    layer can't see the gateway.
    """
    if not sponsoring_enabled(settings):
        raise ValueError("Emoji sponsorship isn't enabled here.")
    cleaned = validate_emoji_name(
        name, blocklist_patterns=blocklist_patterns, taken_names=taken_names
    )
    if open_submission(conn, guild_id, user_id) is not None:
        raise ValueError(
            "You already have an emoji in flight — once it's resolved (or "
            "lapses) you can sponsor another."
        )
    if not guild_slots_free:
        raise ValueError(
            "No sponsored-emoji slots are free right now — try again when "
            "one lapses."
        )

    price = emoji_price(settings, animated=animated)
    unit = settings.currency_plural or "coins"
    if not apply_debit(
        conn, guild_id, user_id, price, SPEND_KIND, meta={"name": cleaned}
    ):
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(
            f"Sponsoring :{cleaned}: costs {price} {unit} for the first "
            f"week — you have {have}."
        )
    try:
        cur = conn.execute(
            "INSERT INTO econ_emoji_submissions "
            "(guild_id, user_id, name, image_path, animated, state, price, "
            " created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (guild_id, user_id, cleaned, image_path, int(animated), price,
             time.time()),
        )
    except sqlite3.IntegrityError as exc:
        # The partial unique indexes (per-member, per-name) close the race the
        # SELECT-based guards above can lose.
        raise ValueError("That emoji name is already taken here.") from exc
    # shop_purchase quest trigger (one-time setup kind); deferred import —
    # the quests service imports the wider economy machinery.
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", user_id, occurrence="set")
    return EmojiSubmitOutcome(submission_id=int(cur.lastrowid or 0), price=price)


def _refund(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> int:
    """Give the escrow back exactly once. Returns the amount refunded."""
    price = int(row["price"])
    if price < 1:
        return 0
    cur = conn.execute(
        "UPDATE econ_emoji_submissions SET refunded_at = ? "
        "WHERE id = ? AND refunded_at IS NULL",
        (time.time(), int(row["id"])),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    apply_credit(
        conn,
        int(row["guild_id"]),
        int(row["user_id"]),
        price,
        REFUND_KIND,
        meta={"submission_id": int(row["id"]), "reason": reason},
        booster=False,
    )
    return price


def deny_submission(
    conn: sqlite3.Connection,
    submission_id: int,
    *,
    resolver_id: int,
    deny_reason: str = "",
) -> sqlite3.Row:
    """Deny a pending (or upload-limbo approved) submission; refunds."""
    row = conn.execute(
        "SELECT * FROM econ_emoji_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise ValueError("That submission no longer exists.")
    cur = conn.execute(
        "UPDATE econ_emoji_submissions SET state = 'denied', resolver_id = ?, "
        "resolved_at = ?, deny_reason = ? "
        "WHERE id = ? AND state IN ('pending', 'approved')",
        (resolver_id, time.time(), deny_reason[:500], submission_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError(f"That submission is already {row['state']}.")
    _refund(conn, row, "denied")
    return _fresh(conn, submission_id)


def cancel_submission(
    conn: sqlite3.Connection, submission_id: int, *, user_id: int
) -> sqlite3.Row:
    """A member pulls their own PENDING submission back; refunds.

    Only pending — an approved row is mid-upload and a live one is a running
    rental (cancelled from the rental, not here).
    """
    row = conn.execute(
        "SELECT * FROM econ_emoji_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None or int(row["user_id"]) != user_id:
        raise ValueError("That submission isn't yours to cancel.")
    cur = conn.execute(
        "UPDATE econ_emoji_submissions SET state = 'cancelled', resolved_at = ? "
        "WHERE id = ? AND state = 'pending'",
        (time.time(), submission_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("Only a still-pending submission can be cancelled.")
    _refund(conn, row, "cancelled")
    return _fresh(conn, submission_id)


def claim_approval(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int
) -> sqlite3.Row:
    """Claim a pending submission for upload (pending → approved).

    The claim lands BEFORE the Discord upload so two racing resolvers can't
    both upload; the winner's caller then runs the upload and calls
    :func:`finalize_upload` or :func:`fail_upload`.
    """
    row = conn.execute(
        "SELECT * FROM econ_emoji_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise ValueError("That submission no longer exists.")
    cur = conn.execute(
        "UPDATE econ_emoji_submissions SET state = 'approved', resolver_id = ?, "
        "resolved_at = ? WHERE id = ? AND state = 'pending'",
        (resolver_id, time.time(), submission_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError(f"That submission is already {row['state']}.")
    return _fresh(conn, submission_id)


def finalize_upload(
    conn: sqlite3.Connection,
    settings: EconSettings,
    submission_id: int,
    *,
    emoji_id: int,
    now: float | None = None,
) -> sqlite3.Row:
    """Record a successful upload: approved → live + open the weekly rental.

    The submit escrow already paid week one, so ``next_bill_at`` is a week
    out from now. The rental row is inserted directly (not via ``rent_perk``)
    because the money for the first week has already moved.
    """
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT * FROM econ_emoji_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None or str(row["state"]) != "approved":
        raise ValueError("That submission isn't awaiting an upload.")
    import json

    from bot_modules.economy.rentals import WEEK_SECONDS

    cur = conn.execute(
        """
        INSERT INTO econ_rentals
            (guild_id, user_id, perk, state, price, started_at, next_bill_at,
             cancel_at_period_end, suspended, beneficiary_id, meta, created_at)
        VALUES (?, ?, 'emoji', 'active', ?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (
            int(row["guild_id"]), int(row["user_id"]), int(row["price"]),
            now, now + WEEK_SECONDS, int(row["user_id"]),
            json.dumps(
                {
                    "submission_id": submission_id,
                    "emoji_id": emoji_id,
                    "name": str(row["name"]),
                    "animated": bool(row["animated"]),
                }
            ),
            now,
        ),
    )
    rental_id = int(cur.lastrowid or 0)
    conn.execute(
        "UPDATE econ_emoji_submissions SET state = 'live', emoji_id = ?, "
        "rental_id = ? WHERE id = ?",
        (emoji_id, rental_id, submission_id),
    )
    return _fresh(conn, submission_id)


def fail_upload(
    conn: sqlite3.Connection, submission_id: int, *, reason: str
) -> sqlite3.Row:
    """Upload failed after a claim: approved → denied, with a refund."""
    row = conn.execute(
        "SELECT * FROM econ_emoji_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise ValueError("That submission no longer exists.")
    cur = conn.execute(
        "UPDATE econ_emoji_submissions SET state = 'denied', deny_reason = ?, "
        "resolved_at = ? WHERE id = ? AND state = 'approved'",
        (reason[:500], time.time(), submission_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError(f"That submission is already {row['state']}.")
    _refund(conn, row, "upload failed")
    return _fresh(conn, submission_id)


def mark_lapsed(conn: sqlite3.Connection, rental_id: int) -> sqlite3.Row | None:
    """Close the live submission when its rental ends (lapse/cancel).

    No refund — the member got the weeks they paid for. Frees the member's
    one-in-flight slot and the name claim. Returns the row (for the loop's
    emoji delete) or None when no live submission points at the rental.
    """
    row = conn.execute(
        "SELECT * FROM econ_emoji_submissions "
        "WHERE rental_id = ? AND state = 'live'",
        (rental_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE econ_emoji_submissions SET state = 'cancelled', resolved_at = ? "
        "WHERE id = ? AND state = 'live'",
        (time.time(), int(row["id"])),
    )
    return row


def expire_stale_submissions(
    conn: sqlite3.Connection, now: float, *, expire_days: int
) -> list[sqlite3.Row]:
    """Refund + expire pending submissions nobody resolved in time.

    Pending only — 'approved' is an active upload (or a limbo a human should
    look at) and 'live' is a running rental. Mirrors the sponsored-QOTD
    sweep. Returns the expired rows for the caller's notices.
    """
    if expire_days <= 0:
        return []
    cutoff = now - expire_days * 86400
    rows = conn.execute(
        "SELECT * FROM econ_emoji_submissions "
        "WHERE state = 'pending' AND created_at < ?",
        (cutoff,),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in rows:
        cur = conn.execute(
            "UPDATE econ_emoji_submissions SET state = 'expired', "
            "resolved_at = ? WHERE id = ? AND state = 'pending'",
            (now, int(row["id"])),
        )
        if (cur.rowcount or 0) == 0:
            continue
        _refund(conn, row, "expired")
        out.append(_fresh(conn, int(row["id"])))
    return out


def list_submissions(
    conn: sqlite3.Connection, guild_id: int, *, state: str | None = None
) -> list[sqlite3.Row]:
    """Queue view for the dashboard — newest last, optionally one state."""
    if state:
        return conn.execute(
            "SELECT * FROM econ_emoji_submissions "
            "WHERE guild_id = ? AND state = ? ORDER BY created_at ASC, id ASC",
            (guild_id, state),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM econ_emoji_submissions "
        "WHERE guild_id = ? ORDER BY created_at ASC, id ASC",
        (guild_id,),
    ).fetchall()


def get_submission(
    conn: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_emoji_submissions WHERE id = ?", (submission_id,)
    ).fetchone()


def _fresh(conn: sqlite3.Connection, submission_id: int) -> sqlite3.Row:
    row = get_submission(conn, submission_id)
    assert row is not None  # updated in this transaction
    return row
