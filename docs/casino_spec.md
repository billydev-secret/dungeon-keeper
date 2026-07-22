# The Golden Meadow Casino — Feature Spec

House gambling games staking the guild currency, played publicly in one
admin-configured **casino channel**. Built 2026-07-22 (plan:
[plans/casino.md](plans/casino.md)). Golden-meadow theming over an
unmistakably Vegas core — the server is The Golden Meadow, the casino leans
into it.

**Zero slash commands.** The bot maintains a persistent **hub panel** in the
casino channel (🪙 Coinflip · 🎰 Slots · 🃏 Blackjack · 🎡 Roulette ·
❓ How It Works); every flow is buttons + amount modals. Results post
publicly (mentions live in embeds, so nothing pings). The panel is
**bottom-sticky** (the economy sticky-panel pattern): channel traffic
debounces a 20s restick that deletes and reposts it, since it is the
casino's only entry point. Roulette round embeds repaint on a 2s debounce
per round (one edit per burst of bets, the live_signal idea). Both the
casino config PUT **and the economy config PUT** dispatch
`casino_config_change`, so enabling/disabling/moving anything updates or
tears down the panel without a restart.

## Money

All movement goes through `services/casino_service.py`:

- `take_stake` — the only debit path. Guard order: economy enabled → casino
  channel set → table enabled → min/max bet → **daily wager cap** → funds.
  Kind `casino_stake`, meta `{"game": ...}`. A blackjack double-down skips
  the min/max re-check (`enforce_bet_limits=False`) but never the cap or
  balance.
- `pay_out` / `refund` — credits (`casino_payout` / `casino_refund`),
  **always `booster=False`**: a house payout must never mint through the
  booster multiplier.
- Daily cap accounting: `casino_daily (guild_id, user_id, local_day,
  wagered)` upsert **in the same transaction as the debit**, guild-local day
  via `tz_offset_hours`. Cap 0 = uncapped (and keeps no books). **Refunds
  hand the headroom back** (current-day row, clamped at 0) — a
  house-initiated refund must not leave the cap consumed by a bet that
  never resolved.
- `take_stake` also takes the interaction's `channel_id` (from the cog
  entry points) and refuses play outside the configured casino channel —
  an orphaned hub panel a failed delete left behind can't run games
  elsewhere.
- A departing member's live stakes ride the wager-escrow rule:
  `refund_member_live_stakes` (called from the cog's `on_member_remove`)
  refunds an open blackjack hand and deletes+refunds their bets on open
  roulette rounds, so nothing settles into a ghost wallet.
- House edge is **fixed paytables in `services/casino_logic.py`, not
  settings** — enforced by exact-EV tests (see Testing). RTPs: coinflip
  95%, slots ≈93.3%, roulette ≈97.3%, blackjack rules-derived.
- The register feed **skips** `casino_stake`/`casino_payout` (results are
  already public in the casino channel, and bet-per-play volume would
  outrun the feed's drain budget and starve other kinds); `casino_refund`
  still posts (↩️, memo names the table), and all three keep their
  `/bank wallet` display entries. Casino kinds are deliberately **absent
  from `FAUCET_GROUPS`** (the `wager_payout` precedent — gross winnings
  aren't faucet income) and `casino_stake` is in `BURN_EXCLUDED_KINDS`
  (gross turnover isn't "spending" on the biggest-spenders board).
- Casino games deliberately do **not** call `pay_game_rewards` — gambling
  pays no participation/win faucet.

## Settings (`casino_*` keys in the config KV table)

| Field | Default | Notes |
|---|---|---|
| `channel_id` | 0 | **Master switch** — 0 = casino closed (ships dark) |
| `min_bet` / `max_bet` | 5 / 100 | max 0 = no ceiling |
| `daily_wager_cap` | 500 | per member per guild-local day; 0 = uncapped |
| `{game}_enabled` ×4 | true | closed tables refuse bets + drop off the panel |
| `roulette_window_seconds` | 45 | betting window (dashboard bounds 15–600) |
| `blackjack_idle_seconds` | 180 | idle hand auto-stands (bounds 30–3600) |
| `panel_message_id` / `panel_channel_id` | 0 | bot bookkeeping, not dashboard-editable |

Dashboard: **Economy → Casino** (`config-casino.js`, admin-only;
`PUT /api/config/casino`, ids as strings). Saves dispatch
`casino_config_change` so the cog re-ensures the panel without a restart
(post/edit/move/tear down; a channel move deletes the old panel).

## Games

- **Coinflip** — heads/tails picker → amount modal. Win pays total
  `stake*19//10` (1.9×).
- **Slots** — one weighted 26-symbol reel × 3 pulls
  (🌻6 🍀5 🐝5 🌾4 🦋3 🍯2 7️⃣1). Precedence triple > two-sevens (5×) >
  non-seven pair (1.5× floored); triples 6/8/9/12/18/40/**120×** (jackpot
  embed goes gold).
- **Blackjack** — fresh shuffled deck per hand, dealer stands all 17,
  naturals 3:2 (resolved at deal, either side), double on first two cards
  only (second debit through `take_stake`), no split/insurance. One live
  hand per member (partial unique index backstops the pre-check). Buttons
  are DynamicItems (`casino_bj:{action}:{hand_id}`) so they survive
  restarts; only the owner may press. Idle hands auto-stand via the 60s
  maintenance sweep; **boot refunds every live hand** (honest reset,
  message edited best-effort).
- **Roulette** — European single zero. One open round per channel (partial
  unique index). Any member opens a round from the hub; bets (red/black 2×,
  dozens 3×, straight 0–36 36×) debit at placement via buttons
  (`casino_rl:{kind}:{round_id}`) + amount modal; the round embed updates
  as bets land. At `closes_at` the timer spins once and settles everyone
  (`status='open'` claim → exactly-once), edits the round message and posts
  a recap. Boot re-arms timers (elapsed windows resolve immediately);
  a round whose guild is gone is **voided** (all bets refunded).

Every terminal path settles or refunds, exactly-once via
`settled_at IS NULL` / `status='open'` claims — a stake can never evaporate
or double-pay, including replayed timers and double-clicks. Because the
pre-checks run in autocommit (legacy DEFERRED isolation), every money-moving
path **re-claims its row inside the write transaction** with a guarded
no-op UPDATE before the debit: `place_roulette_bet` (a buzzer-beater bet
racing the spin misses the claim instead of stranding a stake),
`double_blackjack_stake`, and `resolve_blackjack_action` (which also bumps
`last_action_at`, resetting the idle clock per press, and reports
"already finished" instead of rendering an outcome the settle didn't pay).

Recovery is layered: roulette close timers arm **before** the round
message sends (a failed send voids the round instead of stranding it);
the 60s maintenance sweep auto-stands idle blackjack hands **and resolves
any open round past `closes_at`** (self-healing after a crashed timer);
boot re-arms/refunds as before. Blackjack game rules (double only on two
cards, hit/stand/dealer flow) live in `resolve_blackjack_action` /
`stand_idle_blackjack_hand` in the service — tested, not cog glue — and
the double's second stake is derived from the hand row, never
caller-supplied.

## Storage (migration 113)

`casino_daily`, `casino_blackjack_hands` (state_json = deck/player/dealer,
`settled_at` guard, partial unique live index), `casino_roulette_rounds`
(open|settled|void, partial unique open-per-channel), `casino_roulette_bets`.

## Files

`services/casino_service.py` (money + settings + persistence) ·
`services/casino_logic.py` (paytables, RNG at module level) ·
`cogs/casino/` (cog + views + embeds glue) · `web_server/routes/config.py`
(`_casino_section`, `update_casino`) · `static/js/panels/config-casino.js`.

## Testing

`tests/test_casino_logic.py` — exact-EV enumeration pins each paytable's
RTP band (slots 0.90–0.96, coinflip 0.95, roulette single-zero), blackjack
settle matrix, wheel/dozen/straight payouts.
`tests/test_casino_service.py` — the full `take_stake` guard cascade, cap
accounting across local days, no-boost payouts, blackjack lifecycle
(exactly-once settle, boot sweep, idle sweep, double), roulette rounds
(one-per-channel, window close, exactly-once settle/void, conservation).
`tests/web/test_casino_routes.py` — section shape (string ids), PUT
persistence + guards; authz/snowflake/browser sweeps cover the panel
automatically.
