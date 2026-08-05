# Economy ledger core — battery review, 2026-08-05

Bundle: `economy/` (24 modules, 9.2k), `economy_cog.py` (4k),
`economy_service.py`, `economy_loop.py`, demurrage/boost_reconcile.
Prior art: `2026-07-25-economy-casino-sources-sinks.md`,
`2026-07-30-economy-health.md` (balance design covered there — not
re-reviewed). Loose-ends §1 holds the live retune verdict (+5k/day, missed
target) — that's prod-dial state, not code.

## Architecture — sound at the core

- **Wallet mutation funnel verified**: `econ_wallets` is written by exactly
  one module (`economy_service.py`); `apply_debit` is the textbook shape —
  conditional `UPDATE … WHERE balance >= ?` (atomic compare-and-debit, no
  read-modify-write race), wallet + ledger row on one connection, balances
  can never go negative, `amount < 1` raises. `apply_credit` mirrors it.
- A1 (minor): `economy_raffle_service.py:263` is the only second ledger
  writer — a 0-amount `'rental'`-kind marker row. Harmless (no balance
  mutation) but it means "all ledger rows go through the service" is
  *almost* true. Either route it through a service helper or leave a
  comment at the insert saying why it's exempt.
- A2 (UX, formalizing a known memory item): **perk renewals charge
  silently** — `economy_rentals_service.py:131` / `economy_loop.py:1397`
  re-read the live price and debit with no DM. After the 07-30 reprice this
  billed existing subscribers at new prices with no notice. Recommendation:
  renewal DM (respecting `econ_notify_prefs`) at least when the price
  changed since last cycle. **Priority: medium** (member-trust).
- `economy/` module split (wallet/shop/perks/quests/leaderboard/views) is
  the right shape for its size; cog is command glue over it.

## GDPR

- `econ_ledger.meta` sampled in prod: structured ids/amounts only
  (local_day, xp, qotd_id, rental_id) — **no content/PII beyond user ids** ✓.
- **G1 — the 48-table purge decision** (register's biggest undecided block).
  Recommendation, split by role:
  - **Preserve `econ_ledger`** (pseudonymous financial record; deleting one
    side of transfers/rakes breaks double-entry audit sums and every
    baseline report) — document as deliberate, like dm-audit.
  - **Purge the rest for an erased user**: wallet, streaks, logins,
    notify_prefs, quest progress/claims/marks, kind_activity, setup marks,
    conversions, personal_roles, rentals, vouchers, tickets, bids — they're
    per-member state with no audit role. Add an `econ_purge_user(conn,
    guild_id, user_id)` helper called from `purge_user_data` so the list
    lives beside the schema it mirrors.
- G2 — `econ_notify_prefs`, `econ_onboarding_dms`: preference rows, purge
  with G1. No sensitive content anywhere in econ tables.

## Docs

- `economy_spec.md` is large; not line-verified this pass (prior reviews
  keep it honest). The A2 renewal-notice behavior, if changed, must land
  in spec + manual.html same-commit.

## Verdict

Core is engineering-sound (funnel + atomic debit verified). One medium UX
item (silent renewals, A2), one big-but-mechanical GDPR package (G1 purge
helper), one comment-level nit (A1).
