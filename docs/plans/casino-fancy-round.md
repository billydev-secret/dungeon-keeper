# Casino fancy round — jackpot, animations, celebrations

Decided with the user 2026-07-22 (PIL-rendered graphics explicitly parked):

- **Progressive jackpot, slots-only, loss-fed.** A configurable slice
  (default 25%) of every casino **loss** (any game — payout 0 on the play)
  feeds one guild-wide pot shown on the hub panel. Triple-7️⃣ on slots wins
  `max(pot, 120×stake)` (the flat jackpot multiplier is the floor, so a big
  bet never wins less than today), exactly-once, then the pot reseeds
  (default 100). The pot is bookkeeping over coins already burned — paying
  it re-mints a fraction of past losses, kind `casino_payout` with
  `meta.jackpot`, never boosted.
- **Animated reveals, tiered by bet size.** Small bets resolve instantly
  (slots spam stays one message). A "big bet" — ≥70% of `max_bet`, or
  ≥100 coins when the table is uncapped — gets the staged show: slots reels
  stop one at a time (3 edits ~1s apart), coinflip spins once, blackjack's
  dealer reveal pauses on the hole-card flip. Roulette resolution (once per
  round, never spammy) always gets a 2-edit ball bounce. **Money settles in
  the DB before the first animation frame** — a crash mid-show leaves a
  stale message, never a wrong balance.
- **Celebration & stats layer** (all four picked):
  - Big-win fanfare: payout ≥10× stake escalates the result embed (gold +
    trumpet copy); a jackpot additionally posts a standalone celebration.
  - Streak callouts: ≥3 wins or losses in a row appends a 🔥/🧊 line.
  - `/bank wallet` gains a casino section (wagered, returned, net, biggest
    win, current streak) once the member has played.
  - The leaderboard panel gains a **Night at the Tables** block: this
    ISO-week's biggest win and best multiplier.

## Architecture

- **Migration 114**: `casino_jackpot (guild_id PK, pot, updated_at,
  last_winner_id, last_amount, last_won_at)`;
  `casino_member_stats (guild_id, user_id PK pair, wagered, returned,
  plays, wins, biggest_win, biggest_win_game, streak, best_streak)`;
  `casino_weekly (guild_id, iso_week, user_id PK triple, wagered, won,
  biggest_win, biggest_mult_x100)` — bounded upserts, no per-play log.
- **Service** (`casino_service.py`): `feed_jackpot` (called wherever a play
  fully loses: instant games' cog txn, blackjack settle loss path, roulette
  losing bets), `claim_jackpot` (in-transaction read+reset, the usual
  claim-before-credit), `record_play` (stats + weekly upsert + streak math,
  same txn as each settlement; blackjack refunds and roulette voids do NOT
  count as plays). New settings: `jackpot_enabled` (default on),
  `jackpot_cut_pct` (25), `jackpot_seed` (100).
- **Logic** (`casino_logic.py`): pure `is_big_bet(stake, max_bet)`,
  `is_big_win(stake, payout)` (≥10×), streak update math, and the animation
  frame builders' data (reveal sequences), so all thresholds are tested.
- **Cog**: animation = settle-then-edit loops with `asyncio.sleep`;
  hub embed shows `🍯 Jackpot: <pot>` and the maintenance loop repaints the
  panel when the pot value changed since last render; fanfare/streak lines
  ride the existing result-embed builders.
- **Dashboard**: three jackpot knobs on the Casino page (+ PUT fields).
- **Docs**: casino_spec.md, manual casino section, README blurb.

## Stages

1. this plan doc
2. migration 114 + jackpot service + slots/other-games feed wiring + hub
   pot display — `tests/test_casino_service.py` (feed on losses only,
   claim exactly-once, reseed, `max(pot, 120×)` floor, disabled = flat)
3. stats/streaks/weekly service + record_play wiring + wallet section +
   leaderboard block + fanfare/streak embed logic — service tests
4. tiered animations (cog glue + pure threshold fns tested)
5. dashboard knobs + docs + route tests
