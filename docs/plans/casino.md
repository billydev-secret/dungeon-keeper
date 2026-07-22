# The Golden Meadow Casino — implementation plan

House gambling games staking the guild currency: **coinflip, slots, blackjack,
roulette**. Decided with the user 2026-07-22:

- **Public play in one admin-configured casino channel** (dashboard knob;
  channel unset = the whole casino is off — ships dark, per sink convention).
- **Daily wager cap per member**, configurable on a dedicated Casino config
  page (0 = uncapped; default on).
- **Theme: "The Golden Meadow"** — sunny meadow flavor (🌻🍀🐝🌾🦋🍯) but
  unmistakably Vegas: reels, felt-green wins, a 7️⃣ jackpot line.
- **Roulette scope:** European single zero; red/black (2×), dozens (3×),
  straight numbers (36×).

## Design decisions (mine, veto welcome)

- **Zero new slash commands.** The bot maintains a persistent **casino hub
  panel** in the configured channel (shop-panel pattern) with game buttons;
  every flow is button + modal. Matches "one panel over subcommand sprawl".
- **House games mint on wins / burn on losses.** Each game's payout table is a
  fixed, tested constant with a house edge, so the casino is a net sink over
  volume: coinflip pays 1.9× (95% RTP), slots paytable tuned to ~90–95% RTP
  (asserted by an exact-EV test), blackjack 3:2 with dealer-stands-17,
  roulette single-zero (~97.3% RTP). The daily cap bounds minting variance.
- **Blackjack rules:** fresh shuffled deck per hand, dealer stands on all 17,
  blackjack pays 3:2, double down on the first two cards (extra debit), no
  split/insurance in round one. Idle hands auto-stand after a timeout.
- **Restart posture:** slots/coinflip are single-transaction (nothing to
  recover). Blackjack hands persist; on boot any live hand is **refunded**
  (honest reset, exactly-once via `settled_at IS NULL`). Roulette rounds
  persist and **re-arm their close timer** on boot (Risky Rolls pattern);
  a round already past its window resolves immediately.
- **No `pay_game_rewards`** — gambling must not mint participation rewards.
  Payouts/refunds are unboosted credits (the wager-service rule).
- **Economy gate:** every stake checks `EconSettings.enabled` first.

## Architecture

- `src/bot_modules/services/casino_service.py` — `CasinoSettings` dataclass
  persisted in the `config` KV table under `casino_` keys (econ-settings
  pattern: load/save with typed key routing, `KeyError` on unknown fields):
  `channel_id` (0=off), `min_bet` (5), `max_bet` (100), `daily_wager_cap`
  (500, 0=uncapped), per-game bools, `roulette_window_seconds` (45),
  `blackjack_idle_seconds` (180), `panel_message_id` (bot bookkeeping, not
  dashboard-editable). Plus the single money choke point:
  - `take_stake(conn, guild_id, user_id, amount, game)` — economy enabled →
    game enabled → min/max → daily-cap upsert (`casino_daily` counter,
    `local_day_for` + `get_tz_offset_hours`) → `apply_debit`
    (kind `casino_stake`). Returns member-facing error string or None.
  - `pay_out(...)` kind `casino_payout`, `refund(...)` kind `casino_refund`,
    both `booster=False`.
- `src/bot_modules/cogs/casino/` package (modern layout):
  `logic.py` (pure game math, RNG at module level = stable patch point),
  `db.py` (blackjack hands + roulette rounds/bets SQL),
  `embeds.py` (pure builders, accent color as param),
  `views.py` (hub panel + per-game views, static namespaced custom_ids
  `casino:*`, per-message registration for blackjack/roulette),
  `cog.py` (thin glue: panel upkeep loop, boot recovery, timers).
- **Migration `113_casino.sql`**: `casino_daily(guild_id,user_id,local_day,
  wagered, PK all-but-wagered)`; `casino_blackjack_hands(id, guild_id,
  channel_id, message_id, user_id, stake, doubled, state_json, created_at,
  last_action_at, settled_at)`; `casino_roulette_rounds(id, guild_id,
  channel_id, message_id, status open|settled|void, opened_at, closes_at,
  result, settled_at)`; `casino_roulette_bets(id, round_id, guild_id,
  user_id, bet_type, selection, amount, payout, created_at)`.
- **Register feed:** add `_KIND_DISPLAY` entries (🎰 stake/payout, ↩️ refund);
  add `casino_payout` to `metrics.FAUCET_GROUPS` so the faucet mix doesn't
  under-report.
- **Dashboard:** `_casino_section()` in the aggregate `GET /config` +
  `PUT /config/casino` (inline pydantic model, `extra="forbid"`, `Field`
  bounds, `require_perms({"admin"})`); `static/js/panels/config-casino.js`
  (config-helpers + `mountChannelPicker`, ids as strings); new "Casino"
  heading in `app.js` `SECTIONS`. Authz/snowflake/browser sweeps cover it
  automatically.
- **Docs:** `docs/casino_spec.md` + INDEX.md row; manual.html section +
  `help-sections.js` entry; README feature blurb (no command table changes —
  there are no commands).

## Stages (one commit each, tests ride along)

1. this plan doc
2. migration + `casino_service.py` + `logic.py` (all four games' math) —
   `tests/test_casino_service.py`, `tests/test_casino_logic.py`
   (RTP/EV assertions, blackjack engine, roulette payouts, daily cap,
   economy-off guard)
3. cog + views: hub panel upkeep, coinflip, slots, blackjack (persist +
   boot refund + idle auto-stand) — logic-layer tests for hand lifecycle
4. roulette timed rounds (persist, re-arm, resolve, void/refund paths) —
   `tests/test_casino_roulette.py`
5. dashboard page + register/metrics entries + docs/manual/README —
   route tests in `tests/web/`
