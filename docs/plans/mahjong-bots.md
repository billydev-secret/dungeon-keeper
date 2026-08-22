# Meadow Mahjong — AI seats (addon plan)

**Status: complete 2026-08-22** — stages 1–4 (brain 646fc2f5, driver +
money d98f0390, surface 4c26b339, review fixes with the stage-4 commit;
the review round's P-table below).
Second addon to the shipped v1 ([meadow-mahjong.md](meadow-mahjong.md));
builds directly on the assistance engine
([mahjong-assist.md](mahjong-assist.md)), which is the playing brain.
Shares the assist addon's merge gate: base-game live QA first.

Bot-controlled seats fill a table so one member can play solo (testing, or
just practice) and so a short-handed group can still seat a 4-player game.

Two modes, split by who's at the table (the user's call, 2026-08-22):

- **Practice** — one human + bots. **Stake-free**: no escrow, no stats, no
  quest credit; nothing recorded but the table row itself. The testing mode.
- **Fill** — 2+ humans + bot(s) topping up the count. **Real stakes**: the
  bot's escrow is funded by the house, so human winners are genuinely paid
  and losers genuinely pay. Gated behind a dashboard dial, default **off**,
  until the brain has proven itself in live practice games.

---

## 0. Decisions

| # | Decision | Why |
|---|---|---|
| B1 | The brain is the assist engine, not new AI | `closest_lines` picks the target lines, `suggest_discard` (with the A6 danger rail) picks the throw, `match_hand` decides Mahjong, the exposure machinery decides claims. Coach mode is already a player that tells a human what to do; a bot is those functions with the human removed. One brain, one strength level in v1. |
| B2 | Decisions live in a new pure module, `bot_logic.py` | `(state, seat, card, rng) → action` per phase, no I/O, no Discord — same layering as the rest of the engine, and the gate's new-logic-file rule means `tests/test_mahjong_bot_logic.py` ships with it. The service only schedules; it never decides. |
| B3 | Bot seats are synthetic ids: `-(table_id·10 + seat_index)` | `mahjong_seats.user_id` is NOT NULL with a one-live-seat-per-member unique index — per-table negative ids satisfy both, can never collide with a Discord snowflake or with another live bot table, and are recognizable at a glance in any query. |
| B4 | The driver rides the existing timer machinery | The service already auto-acts for absent humans (auto-pass, blind-pass fallback, strikes). Bot seats are that path playing well: after each transition the service schedules the bot's action with a short randomized delay (~1.5–4 s) so instant responses don't leak information or feel robotic. Restart recovery re-arms bots from state exactly like timers. |
| B5 | Practice tables hold no escrow at all | Not stake-0 escrow rows — **no** `econ_game_wagers` rows, no `settle_split`, no `mahjong_stats`, no results-driven quest credit. A practice table settles points on the card only. What IS recorded: the table row (flagged), so the tables report can show practice traffic. |
| B6 | Fill tables fund the bot through a house wallet, visibly | Before `hold_stake`, the bot id's wallet is topped up to exactly its escrow (ledger reason `mahjong_house_stake`); after settle/refund, whatever the bot id holds is burned back (`mahjong_house_settle`). Net house exposure per hand is bounded by the escrow formula, every coin of it is in the ledger under the synthetic id, and `settle_split`'s zero-sum invariant is untouched — the bot is just a funded player. Humans' wins/losses/stats record normally. |
| B7 | Fill mode defaults OFF behind a dial; practice defaults ON | A farmable bot is a faucet (see the casino payout history). Practice is risk-free and ships enabled; `mahjong_fill_bots` ships disabled until live practice games show the brain holds its own. Both dials on the existing panel; both enforced, per the no-dead-toggles rule. |
| B8 | Bots skip the no-contact check and never occupy a human's seat rules | No-contact protects member-member contact; a bot is not a member. The one-seat-per-member rule stays for humans; bot ids satisfy it structurally (B3). |
| B9 | Purge/export: nothing to do, one register note | Synthetic ids match no data subject, so `purge_user_data` and the access export are unaffected; `docs/data_register.md`'s mahjong rows get a note that negative `user_id`s are house bots, not members, so an auditor doesn't chase them. |
| B10 | Charleston/courtesy behavior is fixed, not clever | Bots pass their three worst tiles (dead-weight-intersection first, then highest-distance contribution), never blind-pass, vote **yes** to the second Charleston in practice (more reps for the human) and **no** in fill games (brisker), and always propose courtesy 0. Deterministic given the rng seed — testable. |
| B11 | Bot names are flora, marked as bots | Seat names render "🌱 Fern", "🌱 Bramble", "🌱 Wisteria" (deterministic per seat). The 🌱 prefix + the table card's footer note make it impossible to mistake a bot for a member. |
| B12 | A leaver mid-game does NOT convert to a bot | Tempting, but it changes the money story mid-hand (whose escrow is it?) and resurrects every leaver edge case the fold-fallow path already settled. Leavers keep folding fallow, exactly as today. |

## Stage 1 — the brain (`bot_logic.py`)

Pure per-phase deciders: `charleston_pick`, `charleston_vote`,
`courtesy_propose`/`courtesy_give`, `turn_action` (declare mahjong via
`match_hand`, else redeem a joker when it advances the best line, else
discard via `suggest_discard` falling back to highest-distance-contribution
tile — a bot must always produce a legal discard even when coach would stay
silent), `claim_response` (mahjong if `match_hand(rack+tile)`, call when the
tile stands up a 3+ group of a top-line under the same legality the engine
enforces, else pass).

**Tests**: per-decider legality + quality on crafted states, and the
integration proof: N seeded bot-vs-bot games driven through the real engine
to completion — every game ends in a mahjong or a wall game, no exception,
no stalled phase, at both seat counts.

## Stage 2 — the service driver + money

Synthetic seats (B3), the scheduler (B4), practice tables (B5, flag on the
table row — migration 177, re-check the number at rebase), house wallet flow
(B6), the two dials (B7). Restart recovery test with a bot mid-turn.

## Stage 3 — the surface

Create flow gains **Practice vs Bots** (visible per dial); a lobby-card
**Add Bot** button for the host on short tables (fill dial). Bot names in
every embed (B11); the no-contact skip (B8); `manual.html` section; spec
amendment 4; register notes (B9).

## Stage 4 — gate + QA + review

Full gate, browser checks for the dials, adversarial review round (the
assist addon's three rounds each paid for themselves), QA card — practice
solo game first, then a 3-human + 1-bot fill game at real stakes.

### Review round (2026-08-22)

Four lenses, two isolated skeptics per finding, every verdict an executed
reproduction. Twelve raw confirmations deduped to seven fixes; one candidate
was killed as documented-intended (bot names on the *dashboard* report show
the raw id — B11 scopes flora names to Discord surfaces):

| # | Finding | Fix |
|---|---|---|
| P1 | **The pump never scheduled its successor after a bot's own act** (three lenses converged): the schedule attempt fires inside the pump's own `await act()`, sees itself alive, and is swallowed — every bot-after-bot chain stalled to the phase timers and bots struck themselves toward fallow, house-staked ones included. The stage-2 test missed it by calling `_pump_bots` directly. | The pump is now a **loop**: it keeps playing bot actions, humanly paced, until no bot has a move. Regression test drives the real funnel — human discard, then bot pass → draw → discard with zero timers. |
| P2 | **The unloadable-table net stranded house coins**: it refunds every hold — the bot's included — but couldn't run the state-based sweep (the state is what's broken), leaving the escrow on the synthetic wallet forever. | The net sweeps by the seat table (`user_id < 0`), the same pattern the privacy purge already used. |
| P3 | **"User -11" ranked on the public Top Earners board** — house top-ups and settle credits are positive ledger rows, and the board summed all of them. | Both boards exclude negative ids: they are not members. |
| P5 | **All-joker rack crashed the discard fallback** (heavy exposures make it reachable): `worst_tiles` excludes jokers and the fallback indexed an empty list. | A joker is the last-resort discard — a bot must always act. |
| P7 | **A fallow bot never voted rematch** though the engine accepts it (and the next deal resets fallow) — the table hung to settle expiry. | The rematch-follow rule now precedes the fallow guard. |
| P9 | The pump done-callback popped its registry key without an identity check, able to evict a newer pump. | Identity-checked callback. |
| P4 | Practice copy said "nothing recorded anywhere"; the table row and seat row are kept (B5 always said "nothing but the table row"). | Copy made precise at all four sites. |

Also from the review, contested but fixed as cheap robustness: the pump's
sleep now clamps to the armed deadline, so a dial-legal 3-second claim
window can't structurally out-race the bot's 1.5–4 s reaction delay.
