"""PvP coin wagers on the duel games — escrow, settlement, refunds.

Economy sinks round 2, stage 4b (migration 094). Equal ante, winner takes the
pot, **no rake** — this moves currency sideways between members and absorbs
nothing, which is the round's deliberate trade (wagers make the games matter;
the absorption target rests on the other stages).

Two rules here invert the surrounding game code, both on purpose:

1. ``pay_game_rewards`` swallows every exception because "economy must never
   block game flow". An escrow **debit** cannot do that — :func:`hold_stake`
   raises, and the caller must refuse to start the game.
2. Every terminal state refunds unless it settles. The hook that calls
   :func:`settle` / :func:`refund_game` may fire more than once (the 1-minute
   sweep, the resume path, and a normal resolution can all reach it), so both
   predicate on ``settled_at IS NULL`` and are safe to replay.

Ledger kinds: ``wager_stake`` (debit into escrow), ``wager_payout`` (the pot
to the winner), ``wager_refund`` (escrow returned). Payout and refund are
plain credits with **no booster multiplier** — a transfer between members must
never mint, the same rule ``/bank pay`` follows.
"""

from __future__ import annotations

import sqlite3
import time

from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
)

STAKE_KIND = "wager_stake"
PAYOUT_KIND = "wager_payout"
REFUND_KIND = "wager_refund"

_LIVE_STATES = ("pending", "held")


def declare_stake(
    conn: sqlite3.Connection,
    guild_id: int,
    game_type: str,
    game_id: int,
    user_id: int,
    amount: int,
) -> None:
    """Record an intended ante with NO money moved (duel challenger).

    The row exists so the amount survives until the target accepts; a decline
    or an expired challenge just drops it (:func:`drop_pending`). Raises
    ValueError for a non-positive amount.
    """
    if amount < 1:
        raise ValueError("A wager has to be at least 1.")
    conn.execute(
        "INSERT INTO econ_game_wagers "
        "(guild_id, game_type, game_id, user_id, amount, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (guild_id, game_type, game_id, user_id, amount, time.time()),
    )


def hold_stake(
    conn: sqlite3.Connection,
    guild_id: int,
    game_type: str,
    game_id: int,
    user_id: int,
    amount: int,
    *,
    currency_plural: str = "coins",
) -> None:
    """Debit a player's ante into escrow. Raises ValueError = member-facing.

    Deliberately raises rather than returning False: a failed debit MUST stop
    the game from starting (see the module docstring). An existing pending row
    for this player is promoted to held; a second call for an already-held
    player is a no-op, so a double-click can't double-charge.
    """
    if amount < 1:
        raise ValueError("A wager has to be at least 1.")
    existing = conn.execute(
        "SELECT * FROM econ_game_wagers "
        "WHERE game_type = ? AND game_id = ? AND user_id = ?",
        (game_type, game_id, user_id),
    ).fetchone()
    if existing is not None and str(existing["state"]) == "held":
        return  # already escrowed — replayed click
    if existing is not None and str(existing["state"]) not in _LIVE_STATES:
        raise ValueError("That wager is already settled.")

    if not apply_debit(
        conn, guild_id, user_id, amount, STAKE_KIND,
        actor_id=user_id, meta={"game_type": game_type, "game_id": game_id},
    ):
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(
            f"You need {amount} {currency_plural} to stake this game — "
            f"you have {have}."
        )
    if existing is None:
        conn.execute(
            "INSERT INTO econ_game_wagers "
            "(guild_id, game_type, game_id, user_id, amount, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'held', ?)",
            (guild_id, game_type, game_id, user_id, amount, time.time()),
        )
    else:
        conn.execute(
            "UPDATE econ_game_wagers SET state = 'held', amount = ? WHERE id = ?",
            (amount, int(existing["id"])),
        )


def drop_pending(
    conn: sqlite3.Connection, game_type: str, game_id: int
) -> None:
    """Delete never-funded pending rows (challenge declined or expired)."""
    conn.execute(
        "DELETE FROM econ_game_wagers "
        "WHERE game_type = ? AND game_id = ? AND state = 'pending'",
        (game_type, game_id),
    )


def game_ante(
    conn: sqlite3.Connection, game_type: str, game_id: int
) -> int:
    """The game's per-player ante (0 = not a wagered game).

    Every row of a game carries the same amount, so the first live row answers
    it — this is how a lobby joiner learns what to pay.
    """
    row = conn.execute(
        "SELECT amount FROM econ_game_wagers "
        "WHERE game_type = ? AND game_id = ? AND state IN ('pending', 'held') "
        "ORDER BY id ASC LIMIT 1",
        (game_type, game_id),
    ).fetchone()
    return int(row["amount"]) if row else 0


def pot_total(conn: sqlite3.Connection, game_type: str, game_id: int) -> int:
    """Currently escrowed total for a game (held rows only)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM econ_game_wagers "
        "WHERE game_type = ? AND game_id = ? AND state = 'held'",
        (game_type, game_id),
    ).fetchone()
    return int(row["t"])


def staked_players(
    conn: sqlite3.Connection, game_type: str, game_id: int
) -> list[int]:
    return [
        int(r["user_id"])
        for r in conn.execute(
            "SELECT user_id FROM econ_game_wagers "
            "WHERE game_type = ? AND game_id = ? AND state = 'held' "
            "ORDER BY id ASC",
            (game_type, game_id),
        )
    ]


def refund_player(
    conn: sqlite3.Connection, game_type: str, game_id: int, user_id: int
) -> int:
    """Refund one player's escrow (lobby leave, or they left the guild).

    Exactly-once via the ``settled_at IS NULL`` predicate. Returns the amount
    refunded (0 when there was nothing live).
    """
    now = time.time()
    row = conn.execute(
        "UPDATE econ_game_wagers SET state = 'refunded', settled_at = ? "
        "WHERE game_type = ? AND game_id = ? AND user_id = ? "
        "AND state = 'held' AND settled_at IS NULL "
        "RETURNING *",
        (now, game_type, game_id, user_id),
    ).fetchone()
    if row is None:
        # Never funded? Then just drop the intent so it can't be revived.
        conn.execute(
            "DELETE FROM econ_game_wagers "
            "WHERE game_type = ? AND game_id = ? AND user_id = ? "
            "AND state = 'pending'",
            (game_type, game_id, user_id),
        )
        return 0
    amount = int(row["amount"])
    apply_credit(
        conn, int(row["guild_id"]), user_id, amount, REFUND_KIND,
        meta={"game_type": game_type, "game_id": game_id},
        booster=False,
    )
    return amount


def refund_game(
    conn: sqlite3.Connection, game_type: str, game_id: int
) -> dict[int, int]:
    """Refund every live stake on a game (abandon / void / wipeout / cancel).

    Safe to replay — a second call finds nothing held and returns {}. Also
    clears never-funded pending rows so a dead game leaves no trace.
    Returns {user_id: amount refunded}.
    """
    out: dict[int, int] = {}
    for user_id in staked_players(conn, game_type, game_id):
        amount = refund_player(conn, game_type, game_id, user_id)
        if amount:
            out[user_id] = amount
    drop_pending(conn, game_type, game_id)
    return out


def settle(
    conn: sqlite3.Connection, game_type: str, game_id: int, winner_id: int | None
) -> int:
    """Pay the whole pot to the winner. Returns the amount paid.

    ``winner_id`` None (a wipeout, or nobody eligible) refunds instead — the
    pot never evaporates. A winner who somehow holds no escrow row still gets
    paid (they won the game; the pot is the pot), but a winner who isn't in
    the game at all can't be conjured — callers pass the game's own winner.
    Exactly-once: the settling UPDATE predicates on ``settled_at IS NULL``, so
    a replayed terminal hook pays nothing the second time.
    """
    if winner_id is None:
        refund_game(conn, game_type, game_id)
        return 0
    now = time.time()
    rows = conn.execute(
        "UPDATE econ_game_wagers SET state = 'settled', settled_at = ? "
        "WHERE game_type = ? AND game_id = ? AND state = 'held' "
        "AND settled_at IS NULL "
        "RETURNING *",
        (now, game_type, game_id),
    ).fetchall()
    if not rows:
        return 0
    pot = sum(int(r["amount"]) for r in rows)
    guild_id = int(rows[0]["guild_id"])
    apply_credit(
        conn, guild_id, winner_id, pot, PAYOUT_KIND,
        meta={
            "game_type": game_type,
            "game_id": game_id,
            "players": len(rows),
        },
        booster=False,  # a transfer between members must never mint
    )
    drop_pending(conn, game_type, game_id)
    return pot


def live_stakes_for_member(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> list[sqlite3.Row]:
    """Every live escrow row a member holds (the member-leave listener)."""
    return conn.execute(
        "SELECT * FROM econ_game_wagers "
        "WHERE guild_id = ? AND user_id = ? AND state IN ('pending', 'held')",
        (guild_id, user_id),
    ).fetchall()


def orphaned_games(
    conn: sqlite3.Connection, *, older_than: float
) -> list[tuple[str, int]]:
    """(game_type, game_id) pairs holding escrow that started long ago.

    The boot-time / periodic safety net for escrow whose game row is gone or
    stuck — the caller checks each game's real state before refunding, so this
    only narrows the search.
    """
    return [
        (str(r["game_type"]), int(r["game_id"]))
        for r in conn.execute(
            "SELECT DISTINCT game_type, game_id FROM econ_game_wagers "
            "WHERE state = 'held' AND created_at < ?",
            (older_than,),
        )
    ]
