# Community Bounty — crowdfunded, mod-awarded task pots

Anyone posts a bounty (a freeform task), anyone chips coins into its pot, and a
mod awards the pot to whoever completed it — minus a configurable house rake
that evaporates. If nobody's awarded it, it refunds every contributor. The
economy's first *many-payer* mechanic; built on the pin/sponsor escrow + card
patterns, with a per-contributor ledger for exact refunds.

## Locked decisions (user)

- **Freeform + mod-awarded.** A bounty is a text task; a mod picks the winner
  (a `UserSelect` on the card), so the winner is unambiguous and it isn't
  coupled to the quest system.
- **House rake %.** The winner takes `pot − floor(pot × bounty_rake_pct / 100)`;
  the rake evaporates (never credited to any wallet = a real sink, next to
  `wager_rake_pct` / `demurrage`). Refunds on cancel/expiry are **never** raked.
  `bounty_rake_pct = 0` (default) = pure pot until an admin sets it.

## Money

- **Escrow at contribute.** Every stake (the poster's opener and every chip-in)
  is an `apply_debit` (kind `bounty_stake`) into the pot, recorded as its own
  `econ_bounty_contributions` row. Pot = SUM of that bounty's non-refunded
  contributions (computed, never denormalized → can't drift).
- **Award:** one `apply_credit` of the payout to the winner (kind
  `bounty_payout`); the rake is simply never credited back — that's the burn.
- **Cancel / expire:** refund each contribution exactly once (`refunded_at`
  predicate guard, kind `bounty_refund`). Contributions are per-row so a partial
  state can't double-pay.

## Lifecycle

`open ──award──> awarded` / `──cancel──> cancelled` / `──(expiry)──> expired`.
The hourly loop's `run_bounty_expiry` refunds + DMs every contributor of an open
bounty past `bounty_expire_days` (default 14) and marks it expired.

## Surface

- A **bounty board channel** (`bounty_channel_id`, dashboard) holds one card per
  bounty: title, description, live pot, contributor count, state. Buttons
  (persistent `DynamicItem`, custom_id carries the bounty id):
  - 💰 **Chip in** — any member; opens an amount modal, escrows into the pot.
  - 🏆 **Award** — mod only; opens a `UserSelect`, pays the winner minus rake.
  - **Cancel** — mod only; refunds every contributor (plain label, no glyph — the
  style guide's Cancel rule).
- Posting is member self-service: `/bounty` opens a modal (title, description,
  opening stake).

## Guardrails / config (all dashboard, dark by default)

- Enabled only when `bounty_channel_id` is set.
- `bounty_min_stake` (default 10): floor for the opener and each chip-in.
- `bounty_max_open` (default 3): open bounties one member may have posted at once.
- `bounty_expire_days` (default 14), `bounty_rake_pct` (default 0).

## Pieces

- Migration `109_econ_bounty.sql`: `econ_bounties` + `econ_bounty_contributions`.
- `economy_bounty_service.py`: create / contribute / award / cancel / expire /
  refund — pure DB, exactly-once refunds, rake maths.
- `economy/bounty_views.py`: board card + persistent Chip-in/Award/Cancel, the
  award `UserSelect`, DMs.
- Cog `/bounty` (modal) + persistent-view registration.
- `economy_loop.run_bounty_expiry` on the hourly tick.
- Settings + register kinds (`bounty_stake`/`bounty_payout`/`bounty_refund`) +
  web route + dashboard (channel picker + rake/knobs near the other sinks).
- Tests: service (escrow, guards, rake maths, refund-all exactly-once), loop
  sweep, cog guardrails. Docs: manual, README, spec.
