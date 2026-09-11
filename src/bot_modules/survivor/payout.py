"""Season-end payouts — stage 6b of docs/plans/survivor.md (spec §1.6).

A season can end four ways: a sole survivor, a final-week split, a wipeout
split, or the Accord. Only the **wipeout split** is built here; the other
three arrive with 6d and 6e. What they all share is this module's promise,
decided 2026-08-17: *any season end settles both pots* — the main pot to the
finishers, the Ghost Streak side-pot to the longest post-death streak — on
that day's standings.

Two things make the payout safe to run from a settle sweep that may be
retried at any moment:

- The season is marked ``complete`` in the **same transaction** as the
  credits. There is no separate "already paid" flag to get out of step; the
  status *is* the flag, and :func:`settle_season_end` refuses a season that
  already carries it.
- Shares are split with the remainder handed out one coin at a time in
  user-id order, so the sum of what is paid equals the pot exactly. A pot
  that pays out 9,999 of 10,000 is a rounding bug nobody notices for a
  season; one that pays 10,003 is a mint.

Pure DB logic over a caller-owned connection — the caller commits.
"""

from __future__ import annotations

import sqlite3

from bot_modules.services.economy_service import apply_credit
from bot_modules.services.survivor_service import SeasonError, set_season_status
from bot_modules.survivor.logic import KIND_PAYOUT, ghost_streaks, pot_totals

# Why the wipeout split ended the season, for the ledger meta and the
# Reckoning's ceremony copy. More arrive with 6d (the Accord) and 6e.
REASON_WIPEOUT_SPLIT = "wipeout_split"


def even_shares(pot: int, user_ids: list[int]) -> list[tuple[int, int]]:
    """Split ``pot`` between ``user_ids``, to the coin.

    The remainder goes one coin each to the lowest user ids — arbitrary, but
    deterministic and total, which is what matters: ``sum`` of the result is
    always exactly ``pot``. Recipients whose share rounds to nothing are
    dropped rather than credited zero, since the ledger has no use for a row
    that moved no money.
    """
    if pot <= 0 or not user_ids:
        return []
    order = sorted(set(user_ids))
    share, remainder = divmod(pot, len(order))
    return [
        (user_id, share + (1 if i < remainder else 0))
        for i, user_id in enumerate(order)
        if share + (1 if i < remainder else 0) > 0
    ]


def streak_winners(conn: sqlite3.Connection, season: dict, now: float) -> list[int]:
    """Who takes the Ghost Streak side-pot: the longest *best-ever* streak,
    ties split (§1.7).

    Best-ever and not the current run, because the spec judges the streak
    that was achieved — "best-ever streak stays on record" is the same
    sentence that says a missed week resets the current one to zero. A ghost
    who ran off seven and then stopped showing up still ran off seven.

    Nobody with a streak of zero wins anything, so a season that ends before
    any ghost has strung a week together returns [] and the side-pot folds
    into the main split rather than vanishing.
    """
    streaks = ghost_streaks(conn, season, now)
    best = max((st["best"] for st in streaks.values()), default=0)
    if best <= 0:
        return []
    return sorted(uid for uid, st in streaks.items() if st["best"] == best)


def settle_season_end(
    conn: sqlite3.Connection,
    season: dict,
    week: int,
    now: float,
    *,
    reason: str = REASON_WIPEOUT_SPLIT,
    finishers: list[int] | None = None,
) -> dict:
    """End the season and pay both pots. Returns the receipt.

    ``finishers`` are the players the main pot splits between; the wipeout
    default is *that week's players* (§1.6) — everyone who was still standing
    when the week began, which after a wipeout is everyone it killed,
    whatever killed them. A player who went out in Week 3 does not share in
    a Week 15 wipeout.

    The side-pot pays the longest streak; if no ghost has one, it folds into
    the main split so that every coin of the seed reaches a player. Raises
    :class:`SeasonError` on a season already complete — the backstop that
    makes a retried sweep safe.
    """
    fresh = conn.execute(
        "SELECT status FROM survivor_seasons WHERE id = ?", (season["id"],)
    ).fetchone()
    if fresh is None:
        raise SeasonError("No such season.")
    if fresh["status"] == "complete":
        raise SeasonError("This season has already been settled.")

    if finishers is None:
        finishers = [
            int(r["user_id"])
            for r in conn.execute(
                "SELECT user_id FROM survivor_players "
                "WHERE season_id = ? AND eliminated_week = ?",
                (season["id"], week),
            ).fetchall()
        ]

    pots = pot_totals(conn, season)
    ghosts = streak_winners(conn, season, now)
    main_pot, ghost_pot = pots["main"], pots["ghost"]
    if not ghosts:
        main_pot, ghost_pot = main_pot + ghost_pot, 0

    receipt = {
        "reason": reason,
        "week": week,
        "main_pot": main_pot,
        "ghost_pot": ghost_pot,
        "main": even_shares(main_pot, finishers),
        "ghost": even_shares(ghost_pot, ghosts),
    }
    for pot_name in ("main", "ghost"):
        for user_id, amount in receipt[pot_name]:
            apply_credit(
                conn, season["guild_id"], user_id, amount, KIND_PAYOUT,
                meta={
                    "season_id": season["id"],
                    "week": week,
                    "reason": reason,
                    "pot": pot_name,
                },
            )
    set_season_status(conn, season["id"], "complete")
    return receipt


def payout_receipt(conn: sqlite3.Connection, season: dict) -> list[dict]:
    """What a settled season actually paid, read back from the ledger.

    Read back rather than stored, so the ceremony can only ever report money
    that really moved. The ``json_extract`` season scoping is ``pot_totals``'
    pattern verbatim, for its reason: ``'"season_id": 1'`` is a substring of
    ``'"season_id": 12'``, so a LIKE would hand season 1 the payouts of
    seasons 10-19.
    """
    rows = conn.execute(
        "SELECT user_id, amount, meta FROM econ_ledger "
        "WHERE guild_id = ? AND kind = ? AND amount > 0 "
        "AND COALESCE(json_extract("
        "CASE WHEN json_valid(meta) THEN meta ELSE '{}' END, "
        "'$.season_id'), 0) = ? ORDER BY amount DESC, user_id",
        (season["guild_id"], KIND_PAYOUT, season["id"]),
    ).fetchall()
    return [
        {
            "user_id": int(r["user_id"]),
            "amount": int(r["amount"]),
            "pot": "ghost" if (r["meta"] and '"pot": "ghost"' in r["meta"])
                   else "main",
        }
        for r in rows
    ]
