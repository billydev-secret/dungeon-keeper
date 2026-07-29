"""Reaction tips — reactor pays the poster, the house burns a rake.

A tip is a **transfer with a rake**, never a mint. The reactor is debited from
their own wallet, the poster is credited the remainder, and the difference is
simply never credited to anyone — that uncredited amount *is* the burn. Net
effect on the money supply is negative, so reactions are a sink.

This deliberately does not use ``economy_service.transfer_currency``: that
credits the recipient exactly what was debited ("transfers do NOT mint" is
explicit in its docstring) and has no way to express a rake.

Charging is idempotent per ``(guild_id, message_id, user_id)`` — the shape
``xp_reaction_awards`` already uses — so react/unreact/re-react charges once
and refunds nothing.

See docs/economy_spec.md and docs/plans/nsfw-classifier-and-reaction-tips.md.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.services.auto_react_service import (
    get_placement_with_conn,
    parse_emojis,
)
from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
)

log = logging.getLogger("dungeonkeeper.economy")

#: Fraction of a tip that is burned rather than delivered. A flat percentage
#: alone rounds to zero on small tips, so a floor of one coin keeps every
#: real tip a net sink.
RAKE_FRACTION = 0.10
MIN_RAKE = 1

LEDGER_KIND_OUT = "tip_out"
LEDGER_KIND_IN = "tip_in"


def min_rung_amount() -> int:
    """Smallest price that actually delivers the poster something.

    Derived from the rake rather than restated, so the API validator and the
    dashboard can't keep accepting rungs that :func:`plan_tip` has started
    declining at every tap after a change to ``RAKE_FRACTION``/``MIN_RAKE``.
    """
    amount = 1
    while amount - compute_rake(amount) < 1:
        amount += 1
    return amount


@dataclass(frozen=True)
class TipOutcome:
    """What a reaction did. ``paid == 0`` means nothing was charged."""

    paid: int = 0
    delivered: int = 0
    burned: int = 0
    reason: str = ""

    @property
    def charged(self) -> bool:
        return self.paid > 0


def compute_rake(paid: int) -> int:
    """Coins burned out of a tip of *paid*.

    Rounds half **up** explicitly rather than via ``round()``, which uses
    banker's rounding — ``round(2.5)`` is 2, so a 25-coin tip would quietly
    rake 2 instead of 3. Ties going to the sink is a choice here, not an
    artifact of the default rounding mode.

    Never exceeds the tip itself, so a partial payment can't burn more than
    was taken.
    """
    if paid < 1:
        return 0
    scaled = math.floor(paid * RAKE_FRACTION + 0.5)
    return min(paid, max(MIN_RAKE, scaled))


def plan_tip(rung_amount: int, balance: int) -> tuple[int, int]:
    """Return ``(paid, delivered)`` for a tap on a rung worth *rung_amount*.

    Partial payment: a reactor short of the full rung tips what they have
    rather than being refused. A tap that would deliver the poster nothing
    after the minimum burn is not worth making — it would debit the reactor
    and credit no one, which is a pure burn dressed up as a tip — so it is
    declined outright and returns ``(0, 0)``.
    """
    paid = min(rung_amount, max(0, balance))
    if paid < 1:
        return 0, 0
    delivered = paid - compute_rake(paid)
    if delivered < 1:
        return 0, 0
    return paid, delivered


def set_rung(
    db_path: Path, guild_id: int, channel_id: int, emoji: str, amount: int
) -> None:
    """Set (or clear, with ``amount <= 0``) the price of one emoji."""
    with open_db(db_path) as conn:
        if amount <= 0:
            conn.execute(
                "DELETE FROM reaction_tip_rungs "
                "WHERE guild_id=? AND channel_id=? AND emoji=?",
                (guild_id, channel_id, emoji),
            )
            return
        conn.execute(
            """
            INSERT INTO reaction_tip_rungs (guild_id, channel_id, emoji, amount)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, channel_id, emoji)
                DO UPDATE SET amount = excluded.amount
            """,
            (guild_id, channel_id, emoji, amount),
        )


def replace_rungs(
    db_path: Path, guild_id: int, channel_id: int, rungs: dict[str, int]
) -> None:
    """Make the channel's ladder exactly *rungs* (0-priced entries dropped).

    One connection and one transaction — the dashboard save used to open one
    per emoji, and wrote prices for emoji it then read back and cleared.
    """
    priced = {emoji: amount for emoji, amount in rungs.items() if amount > 0}
    with open_db(db_path) as conn:
        conn.execute(
            "DELETE FROM reaction_tip_rungs WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        )
        conn.executemany(
            "INSERT INTO reaction_tip_rungs (guild_id, channel_id, emoji, amount) "
            "VALUES (?, ?, ?, ?)",
            [(guild_id, channel_id, e, a) for e, a in priced.items()],
        )


def get_rungs(db_path: Path, guild_id: int, channel_id: int) -> dict[str, int]:
    with open_db(db_path) as conn:
        return get_rungs_with_conn(conn, guild_id, channel_id)


def get_rungs_for_guild_with_conn(
    conn: sqlite3.Connection, guild_id: int
) -> dict[int, dict[str, int]]:
    """All ladders for a guild, keyed by channel — one query, not one per rule."""
    rungs: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT channel_id, emoji, amount FROM reaction_tip_rungs WHERE guild_id=?",
        (guild_id,),
    ):
        rungs.setdefault(int(row["channel_id"]), {})[row["emoji"]] = int(row["amount"])
    return rungs


def get_rungs_with_conn(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> dict[str, int]:
    rows = conn.execute(
        "SELECT emoji, amount FROM reaction_tip_rungs WHERE guild_id=? AND channel_id=?",
        (guild_id, channel_id),
    ).fetchall()
    return {row["emoji"]: int(row["amount"]) for row in rows}


def apply_tip(
    db_path: Path,
    *,
    guild_id: int,
    message_id: int,
    reactor_id: int,
    emoji: str,
    reactor_is_bot: bool = False,
    now: int | None = None,
) -> TipOutcome:
    """Charge *reactor_id* for tapping *emoji* on *message_id*.

    Every guard that can decline lives here rather than in the listener, so
    the reasons are testable and uniform. Returns a :class:`TipOutcome` whose
    ``reason`` names the guard that declined; nothing is written unless the
    tip actually goes through.
    """
    if reactor_is_bot:
        # The auto-react bot places these emoji in the first place, and has no
        # wallet — without this it would charge itself on every single post.
        return TipOutcome(reason="bot")

    created_at = int(time.time()) if now is None else now

    with open_db(db_path) as conn:
        placement = get_placement_with_conn(conn, guild_id, message_id)
        if placement is None:
            # No receipt: the bot never placed emoji here, so nothing on this
            # message is tippable. Stops a hand-pasted rung on a text post or
            # an old message from becoming a payment target.
            return TipOutcome(reason="not_tippable")

        if emoji not in parse_emojis(placement["emojis"]):
            # The receipt lists what the bot actually attached. An emoji that
            # has a price but failed to attach (or was never on this rule) is
            # not a payment button someone can hand-place onto the post.
            return TipOutcome(reason="not_placed")

        author_id = int(placement["author_id"])
        if author_id == reactor_id:
            # Self-tips are ignored entirely — no debit, no credit, no row —
            # so a self-tap can't pad the count the emoji is meant to signal.
            return TipOutcome(reason="self")

        rungs = get_rungs_with_conn(conn, guild_id, int(placement["channel_id"]))
        rung_amount = rungs.get(emoji, 0)
        if rung_amount < 1:
            return TipOutcome(reason="not_a_rung")

        if _already_charged(conn, guild_id, message_id, reactor_id):
            # One charge per reactor per message, forever. Removing the
            # reaction refunds nothing and re-adding costs nothing.
            return TipOutcome(reason="already_charged")

        balance = get_balance(conn, guild_id, reactor_id)
        paid, delivered = plan_tip(rung_amount, balance)
        if paid < 1:
            return TipOutcome(reason="insufficient")

        burned = paid - delivered
        meta = {
            "message_id": message_id,
            "emoji": emoji,
            "rung": rung_amount,
            "rake": burned,
        }

        if not apply_debit(
            conn,
            guild_id,
            reactor_id,
            paid,
            LEDGER_KIND_OUT,
            actor_id=reactor_id,
            meta={**meta, "to": author_id},
        ):
            # Balance moved between the read and the write.
            return TipOutcome(reason="insufficient")

        apply_credit(
            conn,
            guild_id,
            author_id,
            delivered,
            LEDGER_KIND_IN,
            actor_id=reactor_id,
            meta={**meta, "from": reactor_id},
        )
        # `burned` is never credited anywhere. That omission is the sink.

        conn.execute(
            """
            INSERT INTO reaction_tip_awards
                (guild_id, message_id, user_id, author_id, emoji,
                 amount_paid, rake, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                message_id,
                reactor_id,
                author_id,
                emoji,
                paid,
                burned,
                created_at,
            ),
        )

    return TipOutcome(paid=paid, delivered=delivered, burned=burned, reason="ok")


def _already_charged(
    conn: sqlite3.Connection, guild_id: int, message_id: int, user_id: int
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM reaction_tip_awards "
            "WHERE guild_id=? AND message_id=? AND user_id=?",
            (guild_id, message_id, user_id),
        ).fetchone()
        is not None
    )


