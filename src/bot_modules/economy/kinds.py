"""Ledger-kind vocabulary shared by the money path and the metrics.

Lives in ``economy/`` rather than a service because both sides need it and
``services/pools_service`` already sits above ``services/economy_service``
(via ``casino_service``) — defining it there and importing it here would be
a cycle. ``pools_service`` re-exports these names, so
``scripts/economy_tuning_report.py`` and every existing importer keep
working unchanged.

Everything here is **derived from ``ESCROW_PAIRS``, never restated**: a pair
added to one list and forgotten in another is the asymmetry that once made a
cancelled bounty read as a mint (see
docs/reviews/2026-08-06-economy-ledger-data-audit.md M2).
"""

from __future__ import annotations

# EVERY return kind must be listed. bounty_refund was missed on the first
# pass: cancel/expire credits it rather than bounty_payout
# (economy_bounty_service.py:44), so a cancelled bounty booked a full
# spurious mint with no offsetting burn.
ESCROW_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("auction_bid", ("auction_refund",)),
    ("bounty_stake", ("bounty_payout", "bounty_refund")),
    ("tip_out", ("tip_in",)),                       # transfer, rake burned
    ("emoji_sponsor", ("emoji_sponsor_refund",)),
    ("pin_sponsor", ("pin_sponsor_refund",)),
)

# Ledger kinds that move currency sideways rather than minting it. Casino
# payouts belong here: a returned bet is the member's own stake coming
# back. Canonical — scripts/economy_tuning_report.py imports these so the
# offline report and the live line cannot diverge.
NON_FAUCET_KINDS = (
    "transfer_in", "wager_payout", "wager_refund", "casino_payout",
    "casino_refund",
    *(k for _, returns in ESCROW_PAIRS for k in returns),
)

# Kinds that don't actually destroy currency (transfers/wagers move it
# sideways; most of a casino stake is handed straight back, so the real
# casino burn is the hold, booked separately; escrow debits come back
# unless they win).
BURN_KINDS_EXCLUDED = (
    "transfer_out", "wager_stake", "casino_stake",
    *(debit for debit, _ in ESCROW_PAIRS),
)

# Credits that ``faucet_scale_pct`` must NOT touch. Two groups:
#
#   • everything in NON_FAUCET_KINDS — money moving sideways. Scaling a
#     casino payout or an incoming transfer would take a cut of the
#     member's own coins, which is theft, not tuning. Derived, so a new
#     escrow pair is covered the day it is added.
#   • money whose amount was chosen deliberately and must arrive intact:
#     an admin typing 500 into a grant means 500, and a refund returns what
#     was paid. Scaling either would be a bug that looks like a rounding
#     error.
#
# Everything else — every earned faucet, present and future — scales. The
# denylist is the point: a faucet added next month is covered without
# anyone remembering to list it, which is the failure mode that let the
# 2026-07-30 retune go stale.
UNSCALED_CREDIT_KINDS: frozenset[str] = frozenset(
    {
        *NON_FAUCET_KINDS,
        "grant",
        "rental_refund",
        "survivor_refund",
    }
)
