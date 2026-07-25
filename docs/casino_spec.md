# Casino — Feature Spec

House gambling games staking the guild currency in one admin-configured
**casino channel**. Built 2026-07-22 (plan: [plans/casino.md](plans/casino.md));
the Meadow Derby joined 2026-07-24 (plan:
[plans/casino-derby.md](plans/casino-derby.md)); the ephemeral-play UX
landed 2026-07-24 (plan:
[plans/casino-ephemeral-ux.md](plans/casino-ephemeral-ux.md)); Baccarat
joined 2026-07-25 (plan: [plans/casino-classics-and-prediction-market.md](
plans/casino-classics-and-prediction-market.md), Stage 1a). Sunny-meadow
theming over an unmistakably Vegas core.

**The casino's name is per-guild branding**, not a constant: it comes from
`branding_config.casino_name` (`branding_service.resolve_casino_name*`,
default `DEFAULT_CASINO_NAME = "Golden Meadow"` — the home server's name, kept
as the fallback so nothing moved for it). The cog reads it alongside its other
settings and passes it to the pure builders; the hub title is
`🎲 The {name} Casino` and the help embed `How the {name} pays`
(`casino/embeds.casino_title`). Edited on **Config → Branding**, which
dispatches `casino_config_change` so a rename repaints the hub panel. Flavor
copy stays name-agnostic (2026-07-24: dropped the "meadow"/"honeypot"
imagery that used to survive a rename) — result text says "the house" or
"the jackpot" rather than assuming any particular theme, and the slots/
jackpot embed titles interpolate the configured name directly.

**Zero slash commands.** The bot maintains a persistent **hub panel** in the
casino channel (🪙 Coinflip · 🎰 Slots · 🃏 Blackjack · 🎡 Roulette ·
🏇 Derby · 🎴 Baccarat · ❓ How It Works); every flow is buttons + amount
modals.

**Private play, public moments** (2026-07-24; before this, every result was
its own public message and heavy slots play scrolled the channel non-stop,
yanking active UI around): instant games (coinflip/slots/blackjack) render
**ephemerally** — each player's private machine edits itself in place
(`_respond_private`: a press on your own ephemeral message edits it via
`response.edit_message`; any other press opens a fresh ephemeral). Animation
frames ride `edit_original_response` (the interaction webhook, not the
channel edit bucket). The channel itself carries only shared surfaces:

- the **hub panel**, whose **📡 On the floor ticker** (`casino_ticker`
  table, `record_ticker`/`recent_ticker`, written inside `record_play`'s
  settlement transaction for instant games only) lists the last few plays;
  a debounced per-guild repaint (`_schedule_hub_repaint`, 8s) coalesces a
  burst of plays into one in-place panel edit — an edit never moves the
  panel. The panel also carries a **📊 Today at the tables** line naming the
  day's biggest net winner and biggest net loser (`casino_daily_net` table,
  `daily_standings`, folded into the same `record_play` transaction for
  *every* game). Net = returned − wagered over the guild-local day (the same
  YYYY-MM-DD boundary the wager cap uses); the winner shows only while up
  (net > 0) and the loser only while down (net < 0), so an all-green day has
  no loser line and one member can't hold both slots. Refunds/voids never
  reach `record_play`, so a handed-back bet never sways the board. The line
  refreshes on the instant-game repaint; a roulette/derby-only settle
  updates the table but the panel catches up on the next repaint;
- **communal roulette/derby rounds** (public as ever, repainting on a 2s
  debounce per round — one edit per burst of bets, the live_signal idea);
- **broadcast moments**: the jackpot celebration (always), and any
  instant-game win paying ≥ `broadcast_min_payout` (0 = off) — the result
  embed reposted publicly with its 🔁 Play Again button, so the "me too"
  invitation survives for wins worth advertising (`_after_instant`;
  skipped when the jackpot celebration already announced the spin).

The panel is **bottom-sticky** (the economy sticky-panel pattern): channel
traffic debounces a restick (delete + repost, since it is the casino's only
entry point) after 60s — but the restick **holds while a roulette/derby
round is open** in the channel (rechecking every 60s, up to a 5-minute
cap) so the panel never jumps under members who are mid-bet
(`RESTICK_QUICK_SECONDS` / `RESTICK_ROUND_HOLD_SECONDS`). Both the
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
  roulette rounds and derby races, so nothing settles into a ghost wallet.
- House edge is **fixed paytables in `services/casino_logic.py`, not
  settings** — enforced by exact-EV tests (see Testing). RTPs: coinflip
  95%, slots ≈93.3%, roulette ≈97.3%, blackjack rules-derived, derby
  0.90–0.97 per runner (weights × multipliers, pinned per runner so no
  pick strictly dominates).
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
| `{game}_enabled` ×6 | true | closed tables refuse bets + drop off the panel — embed line, hub **button** (`build_hub_view` pares the sent copy; the full view stays registered for stale panels) and How It Works field alike |
| `jackpot_enabled` | true | the progressive pot (armed only while the casino is) |
| `jackpot_cut_pct` | 25 | % of each fully-lost stake skimmed into the pot |
| `jackpot_seed` | 100 | what the pot resets to after a win (minted on claim) |
| `roulette_window_seconds` | 45 | betting window (dashboard bounds 15–600) |
| `derby_window_seconds` | 60 | derby betting window (bounds 15–600) |
| `baccarat_window_seconds` | 45 | baccarat betting window (bounds 15–600) |
| `blackjack_idle_seconds` | 180 | idle hand auto-stands (bounds 30–**840**: an ephemeral hand is editable only through its interaction webhook, whose token dies at 15 min — a longer window would stand hands nobody can repaint; a larger pre-2026-07-24 stored value still loads and settles fine, its message just goes stale) |
| `broadcast_min_payout` | 0 | instant-game wins paying at least this get a public broadcast; 0 = never (jackpot celebrations always post) |
| `panel_message_id` / `panel_channel_id` | 0 | bot bookkeeping, not dashboard-editable |

Dashboard: **Economy → Casino** (`config-casino.js`, admin-only;
`PUT /api/config/casino`, ids as strings) — the panel titles itself with the
guild's casino name, which is edited on **Config → Branding**
(`PUT /api/config/branding`, `casino_name`; blank = the built-in default). Saves dispatch
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
  maintenance sweep — the hand's ephemeral message is repainted through a
  cog-held `(followup webhook, message_id)` handle (`_bj_followups`,
  in-memory, swept after the 15-minute token TTL); the settle itself never
  depends on that edit. **Boot refunds every live hand** (honest reset; the
  old message is unreachable post-restart — the register feed's
  `casino_refund` entry is the player-facing notice, and stale buttons
  answer "already finished").
- **Roulette** — European single zero. One open round per channel (partial
  unique index). Any member opens a round from the hub; bets (red/black 2×,
  dozens 3×, straight 0–36 36×) debit at placement via buttons
  (`casino_rl:{kind}:{round_id}`) + amount modal; the round embed updates
  as bets land. At `closes_at` the timer spins once and settles everyone
  (`status='open'` claim → exactly-once), edits the round message and posts
  a recap. Boot re-arms timers (elapsed windows resolve immediately);
  a round whose guild is gone is **voided** (all bets refunded).
- **Roulette, the derby and baccarat are ONE machine** — the windowed-round
  family (`RoundTables` descriptor in `casino_service.py`: open/place/
  settle/void with the exactly-once claims, and the `_WindowUI` descriptor
  driving the cog's open/repaint/timer/resolve flow) is implemented once and
  parameterized per game, so a money-safety fix can never land in one game
  and miss the others. Per-game code is the paytable, the bet columns/
  validation, the embeds and the show frames.
- **Derby** (plan: [plans/casino-derby.md](plans/casino-derby.md)) — the
  shared windowed machinery re-raced: six fixed runners
  (`casino_logic.DERBY_FIELD`, weights /100 × total-return ratios:
  🐇 38·2.5×, 🦔 19·5×, 🐝 13·7×, 🦋 12·8×, 🐢 10·9.5×, 🐌 8·12×), win
  bets only. One open race per channel; runner buttons
  (`casino_dy:{runner}:{round_id}`) + amount modal debit at placement; at
  `closes_at` the timer draws the winner (weighted), settles exactly-once,
  then plays `derby_frames` on the race message (money **before** the
  first frame) and posts a recap with 🏇 Next Race. Same boot re-arm,
  maintenance backstop, and void rules as roulette.
- **Baccarat** (plan: [plans/casino-classics-and-prediction-market.md](
  plans/casino-classics-and-prediction-market.md) Stage 1a) — punto banco
  on the shared windowed machinery: side buttons
  (`casino_bc:{player|banker|tie}:{round_id}`) + amount modal, fixed
  third-card tableau (`casino_logic._banker_draws`), cards drawn from an
  **infinite shoe** (rank uniform /13) so the RTP is exact enumeration,
  not sampling. Paytable is **EZ-Baccarat commission-free**: Player/Banker
  1:1, ties push the side bets, a Banker win on a **three-card 7 pushes
  Banker bets** (the Dragon-7 bar standing in for the 5% commission), Tie
  pays 8:1 (9× total return). Pinned RTPs (exact-EV test): Player 98.77%,
  Banker 98.98%, Tie 85.88% — Player/Banker are deliberately the best odds
  in the house; Tie is the labeled long shot. The dealt coup persists as
  JSON in the round's `result` column (the outcome is the cards, not a
  number). One deal frame, then the result embed — pushes list under
  "Pushed" and only a genuine win (payout > stake) goes green. Recap
  carries 🎴 Next Hand.

Every terminal path settles or refunds, exactly-once via
`settled_at IS NULL` / `status='open'` claims — a stake can never evaporate
or double-pay, including replayed timers and double-clicks. Because the
pre-checks run in autocommit (legacy DEFERRED isolation), every money-moving
path **re-claims its row inside the write transaction** with a guarded
no-op UPDATE before the debit: `place_roulette_bet`, `place_race_bet` and
`place_baccarat_bet`
(a buzzer-beater bet racing the resolution misses the claim instead of
stranding a stake), `double_blackjack_stake`, and `resolve_blackjack_action` (which also bumps
`last_action_at`, resetting the idle clock per press, and reports
"already finished" instead of rendering an outcome the settle didn't pay).

Recovery is layered: roulette close timers arm **before** the round
message sends (a failed send voids the round instead of stranding it);
the 60s maintenance sweep auto-stands idle blackjack hands **and resolves
any open round or race past `closes_at`** (self-healing after a crashed
timer; the backstop path skips the cosmetic frames so a pile-up can't
stall the sweep); boot re-arms/refunds as before. Blackjack game rules (double only on two
cards, hit/stand/dealer flow) live in `resolve_blackjack_action` /
`stand_idle_blackjack_hand` in the service — tested, not cog glue — and
the double's second stake is derived from the hand row, never
caller-supplied.

## The fancy layer (plan: [plans/casino-fancy-round.md](plans/casino-fancy-round.md))

- **Progressive jackpot** — `feed_jackpot` skims `jackpot_cut_pct`% of every
  fully-lost stake (any game; refunds/voids never feed) into
  `casino_jackpot`; the hub panel shows the pot and the maintenance loop
  repaints on drift. Slots triple-7️⃣ claims `max(pot, 120×stake)`
  exactly-once inside the spin's transaction (`claim_jackpot`: read+reset
  with the flat multiplier as a floor), reseeds to `jackpot_seed`, and
  posts a standalone gold celebration beside the result. The pot is
  bookkeeping over coins the ledger already burned; paying it re-mints
  that recorded slice (`casino_payout`, `meta.jackpot`, never boosted).
- **Play stats** — `record_play` (same transaction as every settlement;
  never refunds) maintains `casino_member_stats` (lifetime wagered /
  returned / plays / wins / biggest win / signed streak) and the bounded
  per-ISO-week `casino_weekly` rollup. Surfaces: an **At the Tables**
  section on `/bank wallet`, and **Night at the Tables** on the live
  leaderboard (week's biggest win + best multiplier, named — public play
  is opting in, the raffle rule).
- **Celebrations** — `is_big_win` (≥10×) escalates result embeds to gold
  with a 💥; streak callouts (🔥/🧊 at |streak| ≥ 3) ride instant-game and
  blackjack results.
- **Tiered animations** — `is_big_bet` (≥70% of `max_bet`, or ≥100 coins
  uncapped; fixed constants) gates the staged reveals: slots reels stop
  one at a time (`SLOTS_REEL_STOP_SECONDS`, 1.4s per stop; the reel row
  sits in a text-art cabinet on every slots frame, spinning reels shown
  as 🌀), coinflip hangs in the air, blackjack pauses on the
  hole-card flip. Roulette's once-per-round resolution always gets a
  two-frame ball bounce. **Money settles before the first frame** — a
  crash mid-show leaves a stale message, never a wrong balance.

## UX layer (2026-07-22 review round)

- **Loop-closers:** every instant/blackjack result carries a persistent
  🔁 button (`casino_again:{game}:{side}:{amount}`) that replays the same
  stake **for whoever clicks** (their coins; every guard re-applies). On
  your own ephemeral machine it respins the same message in place; on a
  public big-win broadcast it opens the clicker's own machine — results
  stay invitations, not dead ends. Roulette recaps carry 🎡 Next Round.
  Stale buttons stay safe: stakes re-validate at click.
- **Informed bets:** the bet modal's label carries live limits and cap
  headroom ("Your bet (5–100 · 340 left today)") and pre-fills the
  member's last stake per game (in-memory). The cap error names its reset
  time; the hub's 📊 My Stats button shows the personal tally + today's
  cap usage ephemerally.
- **Jackpot feedback:** losing results append "the loss feeds the jackpot
  — now N" (from the settle's own transaction), so the jackpot's funding is
  visible instead of silent. The round-already-running note
  carries a jump link to the live round message. Blackjack hides Double
  Down when the clicker can't afford the second stake.

## Storage (migrations 113 + 114 + 127 + 128)

`casino_daily`, `casino_blackjack_hands` (state_json = deck/player/dealer,
`settled_at` guard, partial unique live index), `casino_roulette_rounds`
(open|settled|void, partial unique open-per-channel), `casino_roulette_bets`;
114 adds `casino_jackpot` (one row per guild), `casino_member_stats` and
`casino_weekly` (bounded upserts, no per-play log); 127 adds
`casino_race_rounds` + `casino_race_bets` (the roulette pair's shape, with
`winner`/`runner` in place of `result`/bet type); 128 adds `casino_ticker`
(the hub floor ticker's bounded per-play log — instant games only,
trimmed to `TICKER_KEEP` rows per guild on insert).

## Files

`services/casino_service.py` (money + settings + persistence) ·
`services/casino_logic.py` (paytables, RNG at module level) ·
`cogs/casino/` (cog + views + embeds glue) · `web_server/routes/config.py`
(`_casino_section`, `update_casino`) · `static/js/panels/config-casino.js`.

## Testing

`tests/test_casino_logic.py` — exact-EV enumeration pins each paytable's
RTP band (slots 0.90–0.96, coinflip 0.95, roulette single-zero, derby
per-runner 0.90–0.97 with weights summing to 100), blackjack settle
matrix, wheel/dozen/straight payouts, derby race-frame invariants (winner
finishes first and alone, positions only advance).
`tests/test_casino_service.py` — the full `take_stake` guard cascade, cap
accounting across local days, no-boost payouts, blackjack lifecycle
(exactly-once settle, boot sweep, idle sweep, double), roulette rounds
and derby races (one-per-channel, window close, exactly-once settle/void,
conservation, jackpot feeding, the buzzer-beater claim, leaver refunds).
The ticker rides `tests/test_casino_service.py` (rows land via each
instant settle path, communal games and refunds stay off it, per-guild
trim to `TICKER_KEEP`) and `tests/test_casino_embeds.py` (hub "On the
floor" section renders newest-first, omitted when empty, push/partial
lines). `tests/web/test_casino_routes.py` — section shape (string ids), PUT
persistence + guards, `broadcast_min_payout` roundtrip/bounds, the 840s
idle cap; authz/snowflake/browser sweeps cover the panel automatically.
