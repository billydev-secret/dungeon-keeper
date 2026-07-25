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
  New games feed it on net loss too; the prediction-market takeout routes to the
  same sink, keeping the economy net-neutral/deflationary.
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

### Stage 1b — Sic Bo / "Dice" (windowed)

- **Mechanics:** windowed round; three dice rolled once, settles all bets.
- **v1 bets:** Big (11–17), Small (4–10), Odd, Even — all **1:1, lose on any
  triple** → 2.78% edge → 97.22% RTP, already in band, keep as-is. (Exact-total
  and any-triple higher-pay buttons are a *later* iteration with bespoke in-band
  pays over all 216 outcomes — deferred to keep v1 tight.)
- **EV test:** enumerate all 216 three-dice outcomes → assert each bet's RTP.
- **Machinery:** windowed machine; animated 3-dice reveal like derby.

### Stage 1c — War (per-member live hand)

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

### Stage 1d — Keno (windowed, bespoke paytable)

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

> **PAUSED (2026-07-25):** the four Stage-1 games proceed with the chosen
> defaults; the prediction market is on hold pending more direction from the
> user before any build. The design below is the current thinking, not a
> committed spec.


- **Mechanics:** windowed round on an admin-authored server-metric question;
  stakes pool per outcome; at resolution the operator deducts a **5% takeout**
  (→ jackpot sink), remaining pool splits pro-rata among winning stakers.
  Payout = `κ · TotalPool · (theirStake / winningPool)`, κ = 0.95. **Operator loss
  bound: exactly zero** (winners paid only from losers). Live implied odds shown =
  pool share.
- **Anti-manipulation (the hard part):** classify every question —
  - *Directly manipulable* (message counts, "will member X post", "will the
    jackpot drop") → **not offered as a wagered market** (Forecast-only, or not at
    all).
  - *Partially manipulable* (event attendance, new-member counts) → offered with
    the submitter/subject excluded from betting + a **per-question exposure cap**.
  - *Non-manipulable / external / already-settled periods* → safe to offer freely.
  - Encoded rules: no self-resolution stake, exposure cap per question, commit
    wording + named data source before open, admin **void/refund** path
    (exactly-once), insider exclusion.
- **Resolution:** windowed settle with exactly-once claims; public result embed;
  short **dispute window** (e.g. 24h) with an admin void→refund escape hatch.
- **Machinery:** reuses the windowed machine, but the settle-time multiplier is
  computed from pool shares instead of a fixed paytable; adds question metadata
  (source, resolution time, manipulability class) + dispute/void.
- **Economy safety:** takeout is a **sink**; never pay per-participation bonuses or
  loans into the market (Manifold's inflation trap). Track Petals in vs. out per
  market.
- **v1 scope:** admin-authored, non-manipulable questions only. Member submissions
  (behind admin approval) and a non-wagering **Forecast** (Brier/log-score)
  tournament for manipulable questions are follow-ups, not v1.

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
4. **Prediction market v1:** admin-authored non-manipulable questions only
   (default); Forecast tournament + member submissions as follow-ups.
