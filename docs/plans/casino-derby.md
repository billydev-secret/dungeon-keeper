# Casino expansion — the Meadow Derby (horse race)

Decided with the user 2026-07-24: expand the casino with a **communal race
game** — bet on a runner during a betting window, watch the race, winners
paid by fixed odds. Chosen over Mines / Higher-Lower / Blackjack round two
for being the most social option: it reuses roulette's windowed-round
machinery and gives the casino channel a spectator moment.

## Shape

**Derby** rides the roulette pattern almost exactly: any member opens a
race from the hub panel (🏇 Derby button), a betting window runs
(`derby_window_seconds`, default 60), members back one of six runners via
buttons + the shared amount modal (multiple bets allowed, same as
roulette), and at `closes_at` the timer draws the winner, settles every
bet exactly-once, plays a short animated race (money already banked —
the casino's "settle before the first frame" rule), and posts a recap
with a 🏇 Next Race button.

## The field (fixed paytable, not settings)

Six meadow critters, weights out of 100, total-return multipliers as
integer ratios (the coinflip pattern). Favorite pays least, the snail is
the moonshot:

| Runner | Weight | Pays | RTP |
|---|---|---|---|
| 🐇 Hazel the Hare | 38 | 2.5× | .950 |
| 🦔 Bramble the Hedgehog | 19 | 5× | .950 |
| 🐝 Buzz the Bee | 13 | 7× | .910 |
| 🦋 Flutter the Butterfly | 12 | 8× | .960 |
| 🐢 Sheldon the Tortoise | 10 | 9.5× | .950 |
| 🐌 Turbo the Snail | 8 | 12× | .960 |

Per-runner RTP pinned to 0.90–0.97 by an exact-EV test (the slots band);
weights must sum to exactly 100. Win bets only in round one — no
place/show. Losing stakes feed the jackpot at the usual cut; `record_play`
logs every bet.

The race animation is a pure `casino_logic` function: given the drawn
winner, produce N frames of per-runner track positions where positions
only ever advance and the winner crosses the line first (and alone).
The finishing order of the rest falls out of their final positions —
purely cosmetic, only the winner pays.

## Storage (migration 127)

`casino_race_rounds` + `casino_race_bets`, mirroring the roulette pair:
open|settled|void status, partial unique open-round-per-channel index,
`winner` column instead of `result`, bets carry `runner` (index into the
fixed field) instead of bet_type/selection.

## Settings

Two new knobs on the existing dashboard Casino panel (no new panel):
`derby_enabled` (default true, joins the per-game enable row; "derby"
joins `GAMES`) and `derby_window_seconds` (default 60, bounds 15–600 like
roulette's). Everything else — min/max bet, daily cap, jackpot — applies
automatically because the money moves through `take_stake` / `pay_out`.

## Stages

1. **Logic + storage + service** — paytable/weights/frames in
   `casino_logic.py`; migration 127; `casino_service.py` grows the
   race-round family mirroring roulette (`open_race_round`,
   `place_race_bet` with the in-transaction claim, `settle_race_round`,
   `void_race_round`, `open_race_rounds`), `refund_member_live_stakes`
   extends to open race bets, `CasinoSettings` grows the two knobs.
   Tests: EV band, frames invariants, the full service suite mirroring
   roulette's (one-per-channel, guard cascade, exactly-once settle, void
   refunds, conservation, member-leave refund).
2. **Discord glue** — hub button, `DerbyBetButton` DynamicItem +
   runner-picker view, round/race/result embeds (`cap_lines` on the
   recap), race timers + repaint debounce + maintenance-sweep leg + boot
   re-arm in the cog, `DerbyNextView`.
3. **Dashboard + docs** — `_casino_section` + PUT + `config-casino.js`
   fields with route tests; `casino_spec.md`, `manual.html` casino
   section, INDEX.md note.

## Explicitly out (this round)

Place/show bets, pari-mutuel pools (degenerate at small player counts —
fixed odds is right for this scale), PIL-rendered race graphics (still
parked from the fancy round), per-member self-service spend limits (a
separate open review suggestion, not derby scope).
