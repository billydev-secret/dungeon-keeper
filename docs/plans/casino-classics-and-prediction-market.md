# Casino classics + prediction market

**Status:** Aspirational / in-progress. Research basis: the "Dungeon Keeper
Casino & Prediction-Market Extension" report (game-design + systems-engineering
survey of Discord casino bots, RTP tables, and prediction-market microstructure).
This plan adapts that report to what the codebase *actually* has.

## Goal

Expand the casino beyond its current five tables (coinflip, slots, blackjack,
roulette, derby) with four new classic games, then add a play-money prediction
market. Ship one game at a time, each landing with tests and docs in the same
commit, each mergeable to main for live testing on its own.

## Correction to the research report

The report assumes an existing **"Meadow Options Market"** (binary CALL/PUT
contracts on server metrics). **No such feature exists in this repo** — the only
matches are stray mentions in `docs/`. So the report's Stage-2 framing ("keep
the Options Market, optionally upgrade it to LMSR") is moot: the prediction
market is **entirely net-new**, and the v1 should be the simplest safe design
(parimutuel), not an AMM successor.

## What we leverage (already built)

- **Windowed communal-round engine.** `casino_service.py` drives roulette + derby
  through a shared `RoundTables` descriptor (`ROULETTE_TABLES`, `DERBY_TABLES`)
  and generic `_open_round` / `_live_round` / settle plumbing, with exactly-once
  settlement (`settled_at IS NULL`) surviving replayed timers, boot sweeps, and
  double-clicks. **Baccarat, Sic Bo, Keno, and the parimutuel market are all new
  bet-bucket sets on this same machine** — the report's central feasibility claim,
  confirmed against the code.
- **Per-member live-hand pattern.** Blackjack persists one live hand per member
  with persistent `DynamicItem` buttons and idle auto-resolve. **War reuses this.**
- **Money choke point.** `take_stake` (economy → casino open → table open → bet
  limits → daily cap → funds) and unboosted `pay_out`/`refund`. Every new game
  routes money through these — no new debit/credit paths.
- **Exact-EV testing culture.** Slots/derby payouts are pinned by enumeration
  tests. Every new paytable is pinned the same way (see Testing standard).
- **Sinks.** Fully-lost stakes feed the progressive jackpot (`feed_jackpot`).
  New games feed it on net loss too. **Pools is the exception:** its takeout is
  burned outright rather than routed to the pot, because the pot re-mints what
  it holds — see Stage 2's "Where the takeout goes".
- **Loss visibility.** The hub already shows daily standings (biggest net
  winner/loser, `casino_daily_net` / `daily_standings`), and My Stats shows
  lifetime net + daily cap headroom — the report's "surface net position" harm-
  reduction tool is partly in place already.

## Conventions these games must follow

- **Generic, rename-safe names.** The casino is renameable per guild (default
  "Golden Meadow"); existing games are keyed generically (`coinflip`, `slots`,
  …). New games are `baccarat`, `dice` (Sic Bo), `war`, `keno`, `pools` — **not**
  "Meadow Baccarat" etc. Flavor copy stays name-agnostic.
- **RTP band 93–97%**, pinned by exact-EV enumeration, no sucker bets. If a
  paytable can't hit the band without a sucker bet, cut the bet — don't widen the
  band. (Keno's real 65–75% RTP is *rebuilt* bespoke; do not copy casino keno.)
- **Config on the dashboard.** Each game gets an enable toggle on the Casino
  config panel; bet min/max and the daily wager cap already apply globally. No
  slash commands, no in-Discord admin config.
- Accent color via `resolve_accent_color`; semantic green/red only for genuine
  win/loss; `COLOR_GOLD` for the casino's own panels. Section spacing + monospace
  tables per `docs/embed_style_guide.md`.

## Deliverables & sequencing

Each row is a standalone commit/merge with its own tests.

### Stage 1a — Baccarat (windowed) — **built 2026-07-25**

- **Mechanics:** windowed round. Members bet **Player / Banker / Tie**. Two hands
  dealt by fixed punto-banco drawing rules (zero in-hand decisions); nearest to 9
  wins.
- **Paytable (EZ-Baccarat style, avoids fractional 5% commission):** Player 1:1;
  Banker 1:1 **except a Banker win on a 3-card total of 7 pushes** (the Dragon-7
  removal that replaces commission); Tie 8:1. Result: Banker RTP ≈ 98.0%, Player
  ≈ 98.76%, Tie ≈ 85.6%. **Decision (default):** keep Tie 8:1, clearly labeled as
  the high-edge bet (the fun long shot) rather than dropping it.
- **EV test:** exact enumeration over the punto-banco drawing tree (combinatorial,
  not simulation) → assert Player/Banker/Tie RTP to the pinned values.
- **Machinery:** reuses the windowed-round machine (Player/Banker/Tie are three
  bet buckets like roulette's red/black/dozen). New `BACCARAT_TABLES` descriptor +
  `casino_baccarat_rounds`/`_bets` tables.
- **Flow:** `Baccarat` hub button → pick side (3 buttons) → amount modal → round
  settles publicly with a card-reveal embed (animated like derby optional).

### Stage 1b — Sic Bo / "Dice" (windowed) — **built 2026-07-25**

- **Mechanics:** windowed round; three dice rolled once, settles all bets.
- **v1 bets:** Big (11–17), Small (4–10), Odd, Even — all **1:1, lose on any
  triple** → 2.78% edge → 97.22% RTP, already in band, keep as-is. (Exact-total
  and any-triple higher-pay buttons are a *later* iteration with bespoke in-band
  pays over all 216 outcomes — deferred to keep v1 tight.)
- **EV test:** enumerate all 216 three-dice outcomes → assert each bet's RTP.
- **Machinery:** windowed machine; animated 3-dice reveal like derby.

### Stage 1c — War (per-member live hand) — **built 2026-07-25**

- **Mechanics:** member vs. house, one card each, high card wins 1:1. On a tie:
  **Go to War** (double stake, burn, one more card; the raise pays even, the
  original pushes) or **Retreat** (surrender half). **No Tie side bet** (18.65%
  edge violates the philosophy).
- **Paytable:** standard six-deck go-to-war → ~2.88% edge, in band as-is.
- **EV test:** exact enumeration over rank matchups (six-deck) → assert RTP.
- **Machinery:** per-member live-hand pattern (like blackjack — persistent
  buttons, idle auto-resolve defaulting to the lower-edge **auto-War**).
- **Flow:** `War` button → amount modal → cards revealed → (only on the ~7.4% tie)
  War / Retreat buttons.

### Stage 1d — Keno (windowed, bespoke paytable) — **built 2026-07-25**
(v1 is quick-pick only; a manual "lucky numbers" modal is a possible
follow-up. Shipped paytables land 94.7–95.5% per tier — see
`casino_logic.KENO_PAYTABLE`.)

- **Mechanics:** windowed round; member picks a spot-count tier; bot quick-picks
  (or accepts a numbers modal); 20 of 80 drawn; pays by catch count.
- **Paytable:** **bespoke, NOT casino keno.** Design catch-count pays per tier so
  exact-EV lands ~94–96%, using the hypergeometric
  `P(catch k) = C(t,k)·C(80−t,20−k) / C(80,20)`. Keep a splashy top prize for
  spectacle without breaking the band. Tiers: Pick-4/6/8/10.
- **EV test:** exact hypergeometric enumeration per tier → assert RTP in band.
- **Machinery:** windowed machine + quick-pick helper + spot-count selector; the
  shared 20-number reveal is a natural group moment.

### Stage 2 — Prediction market: parimutuel "Pools" (net-new)

> **COMMITTED SPEC (2026-07-27).** Superseded the paused 2026-07-25 draft after
> a design session with the user, informed by a read of live prod data. The
> subject changed: this is **not** an admin-authored question market. It is a
> single, self-running daily over/under on the server economy's own net change.
> Most of the 07-25 draft's apparatus (question metadata, manipulability
> classes, named data sources, dispute window, member submissions) is
> **deliberately dropped** — see "What the earlier draft got wrong" below.

#### The market

One round per guild-local day, opened and resolved by the bot with **no admin
authoring and no admin resolution**.

- **Metric:** the day's **net change in the economy — mint minus burn**: positive
  ledger rows excluding `NON_FAUCET_KINDS`, minus negative rows excluding
  `BURN_KINDS_EXCLUDED`, plus `casino_hold` as a burn. This is the quantity
  `scripts/economy_tuning_report.py` computes, **plus two changes it does not yet
  make**: session-day attribution (below) and the Pools-row exclusion (see
  "Metric exclusions"). The script must gain both in the same commit, or the
  offline report and the live line silently diverge once Pools has volume. The
  ledger is the source of truth, which is why there is no dispute path.
- **Session-day attribution (not row timestamps).** A round's or hand's stake and
  payout are both attributed to **the day the round or hand was created**, not to
  each ledger row's own `created_at`. This closes a near-free lever documented
  under "Accepted risks": stake and payout are written at different times, so a
  blackjack hand dealt at 23:59 and stood at 00:00:30 would otherwise push its
  whole stake into day D's `casino_hold` and its payout into D+1, moving the
  metric by up to 1,000 for an expected ~7. Only live hands (blackjack, war) and
  windowed rounds can straddle — coinflip and slots settle instantly.
  **Correction to an earlier draft of this spec:** payouts already carried
  `round_id`/`hand_id` in their ledger `meta`, but stakes did **not** —
  `take_stake` wrote only `{"game": …}`. Attributing one side and not the other
  would have created a mirror-image mismatch, so `take_stake` gained a `meta`
  parameter and `_place_bet` now records `round_id`. Rows with no linkage (all
  history predating that change, and every non-casino kind) fall back to their
  own timestamp. **Measured on prod 2026-07-28: 20 rows re-attribute**, moving
  2026-07-24 from −1,083 to −1,908 — the lever was real, not theoretical.
- **PvP wager escrow is not yet attributed.** `wager_stake`/`wager_payout` can
  straddle midnight the same way. It nets to zero across the pair, so it shifts
  value between adjacent days rather than creating it, and duel volume is a
  rounding error beside casino flow. Worth revisiting if that changes.
- **Outcome:** binary **over/under** a line. Not brackets — see sizing below.
- **Line:** set by the bot at open as the **trailing 7-day median** of the same
  metric, **then offset by +0.5** so it can never be hit exactly. The median of
  seven integer observations is itself an integer, and a day landing on it would
  have no defined winner; a half-integer line removes the tie case rather than
  needing a push rule. Self-recalibrating, so a step change in the economy (as
  happened 2026-07-25) is absorbed within a week without anyone touching a dial.
- **Window:** opens at the day roll (00:00 guild-local, tz −7), **closes ~18:00**,
  resolves after the day rolls. Closing in the early evening is deliberate and
  does double duty: bettors can see nearly all of the day's mint (the steady
  term) but none of the night's casino play (the volatile term), so there is
  real information to trade on — and it leaves only ~6h in which a bettor could
  act on a locked position.
- **Day-roll ordering is fixed, not incidental:** at the roll, **settle day D−1
  first, then open day D**. Day D's line is the median of days D−7…D−1
  inclusive, computed only after D−1 has settled and its late-night ledger rows
  have all landed. Opening first would compute the line off a partial day.
- **Payout:** parimutuel. `payout_i = κ · TotalPool · (stake_i / winningPool)`,
  κ = 0.95. Live implied odds displayed during the window = pool share.
- **Bet limits:** the casino's existing `min_bet`/`max_bet` and `daily_wager_cap`,
  with **no market-specific cap** — decided deliberately (see "Accepted risks").
  Be clear about what that means in the guild this ships to
  (`1469491362444480666`): `daily_wager_cap` is **0**, i.e. uncapped, `_place_bet`
  imposes no per-round or per-user bet-count limit, and `max_bet` is **1000**
  *per bet* over an 18-hour window. The effective constraint is therefore a
  member's wallet, not a limit — a p99 wallet (5,097) can go in entirely on one
  side. `pools_max_bet` on the Casino config panel is the escape hatch if pools
  come out lopsided; it is written down here so it is not rediscovered.

#### Naming: takeout vs. cut

The casino now has two different 5%s on two different bases. Keep the words
distinct in code, config and copy:

- **cut** — `casino_jackpot_cut_pct`, skimmed from each *fully-lost stake* in
  `feed_jackpot`.
- **takeout** — `casino_pools_takeout_pct` (default 5), deducted from the *whole
  pool* at settle. κ = 1 − takeout. **The `casino_` prefix is mandatory**:
  `load_casino_settings` reads config with `GLOB 'casino_*'` and strips
  `CASINO_PREFIX` (`casino_service.py:129-136`), so an unprefixed key never
  loads.

A 5% takeout is 95% RTP, squarely inside the casino's 93–97% band, so Pools
needs no separate RTP justification.

**Prod values in this document are guild `1469491362444480666`.** The other live
guild (`1358148226850492618`) runs `jackpot_cut_pct` 25, `max_bet` 100 and
`daily_wager_cap` 500 — different enough that the sizing conclusions here do not
transfer to it.

#### Conservation invariant (replaces exact-EV enumeration)

Parimutuel has no paytable to enumerate, so the pinned property is conservation
instead. Each payout is floored, and the takeout is whatever is left over:

```
payout_i := floor(κ · pool · stake_i / winningPool)
takeout  := pool − Σ payout_i          # a residual, never a rate applied up front
```

Stating `Σ stakes == Σ payouts + takeout` would be vacuous — that is the
definition of `takeout` rearranged. The assertions that carry weight:

1. **Nothing is minted.** The Petals actually credited by `pay_out` over the
   round equal `Σ payout_i`, and no other credit path fires — in particular
   `feed_jackpot` is never called for a Pools round. Total credited ≤ total
   debited by `take_stake`, always.
2. **Dust only ever burns.** `takeout ≥ floor(pool · pct/100)`, because flooring
   each payout can only push the residual up. The house never ends a round
   short, which is the 07-25 draft's "operator loss bound: exactly zero" made
   checkable.
3. **The takeout is in band.** `takeout / pool` lands within one Petal per
   winner of the configured rate — catching a payout formula that silently
   drifts off κ.

Because the takeout is burned rather than routed through `feed_jackpot`, these
hold unconditionally. Note for anyone tempted to reinstate the pot route:
`feed_jackpot` returns 0 when the jackpot is disabled and when the cut floors
below 1 (`casino_service.py:381-389`), so any formulation leaning on its return
value would trip on every round in a jackpot-off guild.

#### Where the takeout goes: burned (decided 2026-07-27)

The 07-25 draft routed the takeout to the jackpot sink. Code review showed the
pot is **not terminal** — `feed_jackpot`'s docstring says winning it "re-mints
this recorded slice", and the award leaves via `pay_out(conn, guild_id, user_id,
payout, "slots", …)` (`casino_service.py:693-701`), a `casino_payout` row that a
Pools-row exclusion would not catch. Pools' takeout would have re-entered the
metric weeks later as `returned`.

**The takeout is therefore burned, not fed to the pot.** `take_stake` has
already debited the stakes; simply not paying the residual out destroys it. This
is terminal by construction, so the conservation assertion holds unconditionally,
the metric needs no jackpot exclusion, a jackpot-off guild needs no fallback,
and "Pools does not inflate" becomes literally true. The cost accepted: the
takeout is invisible — no pot ticks up, 5% of each pool quietly vanishes. If it
ever needs to be visible, show it as a per-round line on the result embed rather
than by re-routing it to the jackpot.

#### Integration with the windowed machine

The existing `RoundTables` / `_settle_round` machinery in `casino_service.py`
carries Pools with two contained changes. Every current game computes payout
per-bet from a fixed paytable (`payout_fn(bet, result)`); parimutuel needs each
payout to depend on all the other bets. `_settle_round` **already reads the
whole round's bets into a list** (`casino_service.py:1306`) before paying, so
the data is in hand:

1. Change `payout_fn` to a whole-round `payouts_fn(bets, result) -> list[int]`,
   index-aligned with the bets. Pools computes the split in one call; the five
   paytable games map their existing per-bet function over the list through a
   one-line `_per_bet` adapter. **One hook with one arity** — an earlier draft
   threaded an optional per-round context as a third argument to `payout_fn`,
   which made the callable's contract depend on whether a *different*
   parameter had been passed.
2. Move `feeds_jackpot` onto `RoundTables` rather than passing it per settle
   call. It is a per-game trait and belongs beside `game`/`result_col`, where
   every other "how does this game differ" fact already lives — a call-site
   kwarg is a second, parallel mechanism a new game's author would not know
   to look for. False for Pools: the losing stakes ARE the winners' payout,
   so skimming them would pay the pot out of money already owed.

`_void_round` also gained a `None`-on-lost-claim return, so a caller can tell
"we voided it, nothing was staked" from "someone else claimed it first"
without reading the row back.

Reused as-is: exactly-once settle via the `status = 'open'` claim, `take_stake` /
`pay_out` / `record_play`, and daily-net stats under game key `pools`.

**Two pieces of shared machinery need a carve-out, both because the round is
24h rather than 45 seconds:**

- **Leaver refunds.** `refund_member_live_stakes` deletes a departing member's
  bets from *any* round with `status = 'open'` (`casino_service.py:1070-1112`).
  A Pools round stays `open` from the 18:00 betting close until it settles after
  midnight, so a member who leaves or is kicked in that 6-hour gap would have
  their stake pulled out of an **already-closed pool**, silently changing every
  remaining bettor's pro-rata payout. Unreachable with the existing games'
  45–60s windows; routine here. Pools must refuse the deletion once betting has
  closed — the stake stays in the pool and settles normally.
- **The floor ticker.** `record_play` writes ticker rows only for
  `TICKER_GAMES = ("coinflip", "slots", "blackjack", "war")`
  (`casino_service.py:432, 495`) — communal round games are deliberately off it.
  Pools follows that convention; it gets daily-net and member stats, **not** a
  ticker row.

**Resolution is recomputable, which no other game's is.** A dice roll's outcome
is lost if the timer is missed; this outcome is derivable from the ledger at any
later time. A missed close, a restart, or hours of downtime all settle correctly
on the next boot sweep by recomputing the completed day.

#### Metric exclusions (load-bearing, not hygiene)

**Pools' own rows.** Market stakes and payouts route through
`take_stake`/`pay_out` like every other game, so without an exclusion the
market's takeout lands as `casino_hold` — a burn — and a bigger pool
mechanically drags net change down. Bet under, inflate the pool, and the takeout
alone moves the number your way.

That is the only Pools-specific exclusion needed. Because the takeout is burned
rather than routed to the pot, no jackpot exclusion is required.

The exclusion needs a matching predicate in `scripts/economy_tuning_report.py` —
its hold query filters on `kind = 'casino_stake' / 'casino_payout'` with no game
predicate (`economy_tuning_report.py:163-171`), so it needs
`json_extract(meta,'$.game') != 'pools'` added to both halves. Otherwise the
line is derived from a different basis than the outcome it settles against.

**Known metric characteristic, deliberately left in: jackpot awards.** A
jackpot hit re-mints the accumulated pot in one lump (currently 7,784 against a
metric stdev of 2,231 — a ~3.5σ shock) and shows up as a `casino_payout` row
that shrinks that day's hold. This is genuine inflation and excluding it would
make the metric lie, so it stays in; the consequence is that a jackpot day is
simply unforecastable. It is unpredictable rather than exploitable — the pot
pays on triple sevens in slots — so it is noise, not an information edge. Flag
if the spikes make the market feel arbitrary.

#### Surface

A **persistent pinned panel** in the casino channel, posted at the day roll and
edited as bets land: the line, the pool split, live implied odds, time to close,
and bet buttons. Both halves already exist — persistent panels
(`casino_panel_message_id`) and the cog's rebuild of an open-round embed on each
new bet. A once-daily market needs ambient visibility or it is forgotten; an
18-hour window is far too long for the ephemeral-only shape the shorter games
use. Settlement posts a separate public result embed.

Dashboard config on the Casino config panel (no slash commands, per CLAUDE.md):
enable toggle, close hour, takeout pct.

#### Charts (decided 2026-07-27: rendered PNGs on both surfaces, throttled)

Both the live panel and the result post carry rendered charts. New module
`src/bot_modules/services/pools_charts.py`, inheriting `activity_graphs.py`'s
conventions exactly — they are load-bearing, not stylistic:

- Set `MPLCONFIGDIR` to the repo-local `.cache/matplotlib` **before importing
  matplotlib** (`activity_graphs.py:15-23`). The unit runs
  `ProtectHome=read-only`, so matplotlib cannot write `~/.config/matplotlib` and
  will warn and rebuild its font cache on every call otherwise.
- `matplotlib.use("Agg")`; Discord dark palette (`_BG = "#2f3136"`, `_TEXT`,
  `_GRID`); `dpi=130`; `savefig` to a `BytesIO`; **return `bytes`**. Cogs wrap
  in `discord.File` and reference `attachment://…png` via `set_image` — the
  pattern at `jail/embeds.py:477`.
- The "Over" series takes the guild accent from `resolve_accent_color`; "Under"
  takes a fixed contrast tone. Green/red stay semantic, used only for candle
  direction.

A Discord embed carries exactly one image, so each surface gets one figure.

**Chart 1 — the live chart (the open panel).** Three stacked panels:
candlesticks of circulation with today's target line, the daily net change with
its rolling median and ±1σ band, and the implied-odds path across the betting
window. Instrument above, market price below — the way a prediction-market page
stacks it. **The candles belong here, not on the result card:** the instrument
matters while you are deciding whether to bet, and by settlement the answer is
already known. Volume is dropped from this figure in favour of the odds path;
while betting is open, how the pool is moving is the live information and
ledger-row counts are not.

The odds panel must **not** share an x-axis with the two above it. The candles
are indexed by day and the path by fraction-of-window, and letting them share
rescales two weeks of history into a smear (observed, not theorised).

**Chart 2 — the instrument chart (result post).** The same candles and net
change, with volume back in place of the odds path, and today's candle now
closed — the payoff shot that answers "how close was it?".

This is honest OHLC, not decoration. The cumulative sum of `econ_ledger.amount`
*is* circulation — verified against prod on 2026-07-28: `SUM(amount)`,
`SUM(econ_wallets.balance)`, the final candle's close and the sum of every day's
net all agree exactly (60,820), with all 11,646 rows counted once. So the day's
net change and the candle body are the same arithmetic, and the settlement and
the chart cannot drift apart.

**Implementation note:** the level must accumulate in *attributed-day* order,
not timestamp order. A payout pulled back across midnight has to land inside
its own day's run of rows, or that day's close and the next day's open stop
agreeing and the series grows a gap the ledger does not have. The shipped
`daily_series` sorts by `(day, created_at, id)` before walking, and a test pins
continuity. The reference query below is the raw shape, *before* attribution:

```sql
WITH r AS (
  SELECT <day_expr> AS d,
         SUM(amount) OVER (ORDER BY created_at, id) AS lvl,
         ROW_NUMBER() OVER (PARTITION BY <day_expr>
                            ORDER BY created_at DESC, id DESC) AS rn_last
  FROM econ_ledger WHERE guild_id = ?)
SELECT d, MIN(lvl) AS low, MAX(lvl) AS high,
       MAX(CASE WHEN rn_last = 1 THEN lvl END) AS close, COUNT(*) AS volume
FROM r GROUP BY d ORDER BY d
```

`open` is the prior day's `close`. **The candle body is exactly the metric** —
prod bodies −1,083 / +8,952 / +5,058 match the net-change series to the Petal —
so a bettor is literally betting on whether today's candle closes above the
line. Wicks are real intraday extremes: they are absent before 2026-07-24 (the
economy only minted, monotonically) and appear the day the casino launched, when
money began moving both directions within a day. `COUNT(*)` per day is genuine
activity volume (38 rows on 07-12 → 2,651 on 07-27).

Note the y-axis must auto-scale to the plotted window: the level (62k) dwarfs a
day's body (~5k), and as the economy matures the ratio worsens.

**Overlays: the settlement line as a horizontal threshold, the 7-day moving
average (which *is* the line), and a ±1σ band.** Deliberately **no** RSI, MACD
or regression trend lines. On 16 observations with a structural break three days
old, those imply a signal that is not in the data; the candles, line, MA and band
give the same trading-desk look without claiming anything false. Revisit once the
series is long enough to mean something.

**Throttling.** The panel redraws on bets, so coalesce: re-render at most once
per ~20s, always render a final frame at close, and swap the image with
`message.edit(attachments=[...])`. At 13–18 bets/day the cost is negligible, but
the throttle keeps a busy day from turning into a render queue.

**And a floor, added 2026-08-23.** A throttle alone leaves the panel stale in the
other direction: the chart's top row is today's *in-progress* candle, which tracks
the economy all day whether or not anyone bets, so a quiet hour was leaving members
reading an old picture of the very thing they are betting on. A live panel now
repaints at least hourly (`pools_logic.refresh_due`), riding the same minute
maintenance tick the day roll uses rather than owning a timer. A stake-driven
repaint stamps the same clock, so a busy market never repaints twice for one
reason, and a restart (no in-memory stamp) re-hydrates the panel on the first tick.

Charts are **never a source of truth**. The settlement number comes from the
metric function; the chart calls that same function. A chart that disagrees with
the result embed is a bug in one caller, not two implementations to reconcile.

#### Degenerate rounds (defaults — flag to change)

At ~13–18 casino bettors/day, thin rounds will be common, not exceptional.

- **Either side empty at close → void, refund all, no takeout.** A one-sided
  pool has no counterparty; taking 5% of it would be a pure tax on the only
  people who showed up. This also covers the zero-bet and single-bettor cases.
- **Cold start:** the line needs ≥7 completed days of metric history; below
  that, don't open a round.
- **Admin void → refund** is kept (cheap, reuses `_void_round`), even though the
  ledger-derived outcome makes it near-unnecessary. **Caveat for a cross-midnight
  void:** `refund()` decrements `casino_daily.wagered` for the *current*
  guild-local day, clamped at 0 (`casino_service.py:345-354`). A void at 18:00 is
  fine, but this spec explicitly plans for a missed close settling on the next
  boot sweep *after* the roll — at which point a Pools refund zeroes the new
  day's counter and grants free cap headroom for a stake never placed that day.
  Cosmetic in the main guild (`daily_wager_cap` 0), real in
  `1358148226850492618` (cap 500). Refund against the round's own day.

#### Why not brackets

Considered and rejected on the numbers: 13–18 casino bettors/day means 3–5
outcome bands routinely produce a lone bettor in the winning band taking the
whole pool, plus empty-winning-band rounds. Binary keeps the pools thick enough
to mean something at this server's size. Revisit if participation grows.

#### Accepted risks (decided by the user 2026-07-27)

**The metric is substantially controlled by two individuals, and the build does
not mitigate it.** Prod evidence: the largest single-member day minted 3,407,
of which 2,550 was `game_host` — lumpy, discretionary, and untouched by
`econ_conversion_daily_cap` (prod: 250). On 2026-07-26 one member was 35,660 of
that day's 55,022 total casino handle, driving most of its 10,628 hold. Since
net change ≈ mint − casino hold, the top host and the top gambler between them
substantially decide the number, while the median member mints 27/day and bets
5 at a time.

This is not manipulation in the 07-25 draft's sense — they would be doing what
they already do while holding a position — but it concentrates the informational
edge. A low per-round cap was offered and **declined in favour of the existing
casino limits**; insider exclusion was offered and declined as punitive toward
the most active members. Watch for pools dominated by a single stake; the
`pools_max_bet` dial above is the response if it becomes a problem.

**Correction and fix (2026-07-27, from code review).** An earlier version of
this spec argued that the "under" side polices itself, because betting under
requires burning and `casino_hold` *is* the players' net loss, so moving the
metric costs 1:1. That was part of the basis for declining a cap, and it is
false: it holds only *within* a day. Stake and payout are written at different
times — blackjack debits at deal (`take_stake`, `casino_service.py:814`) and
credits at settle (`_settle_hand`, `:844`) — so under naive row-timestamp
bucketing a 1,000-Petal hand dealt at 23:59 and stood at 00:00:30 moves day D's
hold by the full 1,000 for an expected cost of ~7. That is ~0.45σ bought for
nothing, and any windowed round joined seconds before the roll does the same
with no timing skill at all.

**This is fixed at the root by session-day attribution** (see the metric
definition above), not by capping what the lever is worth. With stake and payout
attributed to the round's or hand's own day, the straddle moves nothing.

The honest residual position on direction: the **over** side rewards faucet
farming, which pays twice — you keep the Petals *and* win the bet — and is
inflationary; the **under** side requires genuinely burning, at 1:1, now that
the boundary lever is closed. Over remains the asymmetric risk, and nothing in
v1 mitigates it beyond `econ_conversion_daily_cap` (which, as above, does not
touch the `game_host` tail).

#### Economy context at spec time (2026-07-27)

The 16 days of history behind the line: net change mean **+3,724**, median
+3,904, stdev 2,231, range −1,083…+8,952, against a total circulation of
**59,590** across 144 wallets — roughly **+6% of the money supply per day**,
doubling in about a fortnight. Wallets are heavily skewed (median 148, p90
1,088, p99 5,097, max 5,682).

Pools is player-vs-player and its takeout is burned, so it is mildly
**deflationary** — the only new money movement is 5% of each pool leaving
circulation for good. Its incidental value is that it makes that +6%/day legible
to the whole server daily, which is a better thermometer than a script only the
admin runs. Track Petals in vs. out per round; **never** pay participation
bonuses or loans into the market (Manifold's inflation trap).

#### Testing standard for this stage

Exact-EV enumeration does not apply. Instead:

- Pool math as a pure logic module: pro-rata split, κ, residual takeout, the
  conservation assertion, integer dust. Assert conservation **with the jackpot
  both enabled and disabled** — Pools must not touch the pot either way.
- Every degenerate round: empty side, zero bets, one bettor, all-one-side.
- Metric computation, **including that Pools' own rows are excluded**, and that
  `economy_tuning_report.py` computes the identical number.
- **Session-day attribution:** a hand dealt before midnight and settled after it
  contributes its whole stake *and* payout to the earlier day — the straddle
  test, written to fail against naive row-timestamp bucketing. Same for a
  windowed round opened before the roll and settled after it.
- Line derivation: trailing median, insufficient history, step change, and that
  the half-integer offset makes an exact-hit outcome unreachable.
- Day-roll ordering: settling D−1 before opening D, and that D's line uses the
  completed D−1.
- A member leaving between the 18:00 close and settlement **does not** have
  their stake pulled from the pool, and every other bettor's payout is unchanged.
- Exactly-once settle under replay, boot sweep and double-click; void→refund,
  including a void after the day has rolled crediting the round's own day's cap.
- Recomputable resolution: settle a round whose close was missed by hours and
  assert it lands on the same outcome.
- **Chart data derivation** (the OHLC query and probability series are logic,
  and get tested; the matplotlib rendering does not). Pin that each candle's
  `close − open` equals the metric function's value for that day — the chart and
  the settlement must not be able to disagree — and that the cumulative ledger
  reconciles to `SUM(econ_wallets.balance)`. Cover a day with no ledger rows
  (flat candle, not a gap) and the first day of history (no prior close).

#### What the earlier draft got wrong

Recorded so the reasoning is not relitigated. The 07-25 draft assumed
admin-authored questions on server metrics, and therefore needed manipulability
classification, named data sources, submitter/subject exclusion, per-question
exposure caps, a 24h dispute window and an approval queue for member
submissions. Choosing a **bot-computed metric with a bot-set line and
ledger-derived resolution** removes the need for all of it: there is no author
to exclude, no source to name, and no result to dispute. The residual risk is
not manipulation but concentration of information, which is a different problem
with a different (and here, declined) remedy.

#### Considered and deliberately excluded

Everything below was raised during the 2026-07-27 design session and consciously
left out. Recorded with the reasoning and the condition that would justify
revisiting, so none of it gets silently rebuilt — or silently forgotten.

**Market design**

| Excluded | Why | Revisit when |
| --- | --- | --- |
| Brackets / 3–5 outcome bands | At 13–18 bettors/day a band routinely has one bettor who takes the whole pool, plus empty-winning-band rounds | Participation roughly triples |
| Direction-only ("did the economy grow?") | 15 of 16 days were positive — resolves yes almost always, so there is no market | The economy stops being monotonically inflationary |
| Admin-set line | Better lines, but daily toil, and it is the part most likely to quietly stop happening | Only if the median line proves badly calibrated |
| Line = yesterday's value | Day-over-day swings are near-random: a coinflip with no informational content | Never |
| Betting on an already-completed day | Structurally unmanipulable, but rewards having been online rather than forecasting | Manipulation proves worse than expected |
| Locking before the day starts | No within-day information, and the manipulator gets all 24h | Never |

**Metric alternatives not chosen**

| Excluded | Why |
| --- | --- |
| Daily mint alone | Betting "over" rewards faucet farming — straight inflation against the 07-27 retune |
| Casino handle alone | One member was 65% of a day's handle; also reflexive, since market stakes are wagers |
| Total circulation as the *subject* | A stock, not a flow — too slow for a daily over/under. It survives as the **chart's** y-axis, where being a slow level is a virtue |
| "Ordinary economy" (net change excluding `game_host` and casino hold) | **Offered and declined.** Would have removed the two-member concentration and given a smooth, genuinely forecastable series. Rejected because it is no longer literally "the size of the economy" and drops the two most interesting terms. Revisit if the market feels decided by two people |
| Excluding jackpot awards | Deliberately left **in**: a jackpot hit is genuine re-minting, and excluding it would make the metric lie. Cost: a ~3.5σ unforecastable spike on hit days. Revisit if the spikes make rounds feel arbitrary |

**Risk mitigations offered and declined**

Both were declined on 2026-07-27 and both remain live risks — see "Accepted
risks". They are the first things to reach for if the market misbehaves.

- **`pools_max_bet`, a per-round exposure cap (~100).** Would bound both
  whale-domination of the pool and what an informational edge is worth. Declined
  in favour of the casino's existing limits, which in the shipping guild are
  effectively no limit (`daily_wager_cap` 0, `max_bet` 1000 *per bet*, unlimited
  bets over 18h). **Watch for:** a single stake being a large fraction of a pool,
  or the live implied odds visibly being one person's opinion.
- **Insider exclusion** (barring same-day game hosts, or members over a handle
  threshold, from holding a position). Declined as punitive toward the most
  active members, and the gambler side is not knowable until the day ends.

The third option offered — session-day attribution — was *not* declined; it was
adopted, and closes the day-boundary straddle at the root.

**Charts**

RSI, MACD, Bollinger bands and regression trend lines are excluded. On 16
observations with a structural break three days old they imply signal that is not
in the data. The candles, target line, 7-day median, ±1σ band and volume give the
same trading-desk look without claiming anything false. **Revisit once there are
a few months of history** — the objection is sample-size-based and expires on its
own.

**Surface**

Ephemeral-only panel (an 18h market nobody can see is one nobody remembers to bet
in), a long-lived live round message (scrolls out of sight within the hour), and
monospace-only rendering (can't do real candlesticks) were all rejected in favour
of the pinned panel with throttled PNGs.

**Other scope**

Member-submitted questions, external-event questions (sports, releases), a
non-wagering **Forecast** (Brier/log-score) tournament, and additional question
types via the registry. Also: surfacing the burned takeout as a per-round line on
the result embed — cheap, and the answer if anyone asks where the 5% went.

### Explicitly out of scope (per the report's "skip" list)

Full craps, Ultimate Texas Hold'em, Caribbean Stud, native plinko/pachinko
physics, full bingo. **Crash / "Rise"** (cash-out multiplier) is deferred: it
needs genuinely new real-time machinery and the strongest responsible-design
gating (session limits, per-game cooldowns, chasing safeguards) — a separate
initiative if pursued at all.

## Responsible-design notes (apply as we go)

Already have: global daily wager cap, lifetime + daily net visibility. The report
flags fast low-decision games (war, baccarat) as high-*velocity* rather than
high-per-round risk; the mitigations worth adding alongside these games (not all
in v1): an ephemeral **reality-check** after N rounds/M minutes showing daily net,
and a member-initiated **casino self-exclusion / cool-off** (tighten instantly,
loosen with delay). No losses-disguised-as-wins, no manufactured near-misses,
big-win celebrations stay proportional (already gated ≥10×).

## Follow-ups (from the 2026-07-25 /simplify review)

- **Parameterize the windowed-contract test suites.** The generic machinery
  behaviors (one-open-per-channel, debit/close, void-once, boot sweep,
  stale-precheck, jackpot feed, leaver refund) are near-identical across the
  five windowed games (~450 lines of copies in `test_casino_service.py`).
  Fold them into one `pytest.param` table over a per-game spec, keeping only
  genuinely game-specific tests standalone. Standalone change; don't bundle.
- **Third live-hand game rule:** blackjack + war now share `HandTables`
  settle/idle/boot-sweep machinery; a third live-hand game must extend that
  descriptor (and the cog's `_auto_resolve_hand`), never clone.
- **Design note (from the 2026-07-25 correctness review):** if an admin
  closes the casino/war table while tie standoffs are pending, the idle
  sweep's war attempt fails the `take_stake` gates and falls back to
  retreat (member forfeits half), whereas a restart's boot sweep refunds
  those rows in full. Inconsistent outcomes for the same admin action —
  worth a deliberate choice (e.g. void/refund on table-closed errors) if
  it ever matters in practice.

## Testing standard (every stage)

- **Exact-EV enumeration** pins each paytable's RTP into 93–97% (Keno/market by
  construction). This is the source of truth, not published casino figures.
- Every guard/branch: table-closed refusal, bet limits, daily cap, insufficient
  funds, exactly-once settlement under replay/boot-sweep, void→refund.
- Logic in `*_logic.py` (pure math) and `*_service.py` (money/persistence),
  tested there; cogs/views/embeds are glue.
- New logic-layer file ⇒ mapped test (the scoped gate hard-fails otherwise).

## Docs to update per stage

`docs/casino_spec.md` (+ `docs/INDEX.md` classification), the user manual
`src/web_server/static/manual.html` (Casino section), and README's slash-command/
feature reference where member-facing behavior changes. The prediction market
likely warrants its own `docs/prediction_market_spec.md`.

## Open decisions (defaults chosen; flag to change)

1. **Baccarat Tie:** keep at 8:1 as the labeled long shot (default) vs. drop it.
2. **Sic Bo v1:** Big/Small/Odd/Even only (default) vs. include exact-total/triple
   bets from the start.
3. **Keno top-prize splash** vs. flatter in-band curve — tune during the EV pass.
4. ~~**Prediction market v1:** admin-authored non-manipulable questions only.~~
   **Settled 2026-07-27** — superseded entirely. v1 is a single self-running
   daily over/under on the economy's net change; no admin authoring. See
   Stage 2 above, which is now a committed spec rather than thinking.
