# Casino expansion — Mines

**Status:** **Built 2026-08-16**, all three stages in one pass. Signed off the
same day; the design below is what shipped, with the four places the build
corrected it recorded under "What the build changed".

Decided with Billy 2026-08-16: expand the casino with **Mines** — a 20-tile
grid hiding a player-chosen number of bombs, each safe tile lifting the
multiplier, cash out any time before you hit one. It is the tenth table.

Mines was considered on 2026-07-24 and lost to Derby "for being the most
social option" ([casino-derby.md](casino-derby.md)). It was never rejected on
merit; it lost a comparison on one axis, and that axis has since been
retired — see below. Conventions, RTP band and responsible-design rules are
inherited from
[casino-classics-and-prediction-market.md](casino-classics-and-prediction-market.md);
this doc only records where Mines needs more than they say.

## Why now, and why it is unapologetically solo

Derby won for being social. That premise did not survive contact with prod:
`c69acb48` measured **73% of 218 windowed rounds with a single bettor and 21
with none at all** — a public board, a countdown and an animation posted for
nobody. All five windowed games were converted to private ephemeral play, and
migration 158 (`d90fd0b8`) made a round belong to a player rather than a
channel. The casino's social layer now lives where it actually works: the hub
floor ticker, daily standings, and big-win broadcasts over
`broadcast_min_payout`.

So the axis Mines lost on no longer exists. **Mines is a solo game and this
design makes no attempt to pretend otherwise** — there is no public board, no
betting window, no spectator moment, and none of that is a gap to fill later.
It is built ephemeral-private from the first commit, following `c69acb48` and
migration 158 as the pattern rather than repeating their conversion. What the
channel sees of a Mines round is exactly what it sees of blackjack: a ticker
line, and a broadcast if the payout clears the bar.

It is also the game the casino is currently missing. Every existing table
resolves in one press (coinflip, slots, baccarat, dice, keno, roulette, derby)
or two (blackjack, war). None of them asks the player to *decide when to
stop*. That is the whole of Mines, and it is why it is worth building — and,
squarely, why it is the most chasing-prone thing on the shelf. See
"Responsible design" below, which is the part of this doc to argue with.

## Shape

Hub → **💣 Mines** → an ephemeral message with four risk buttons
(1 / 3 / 5 / 10 bombs, labelled with their top multiplier, mirroring
`KenoTierButton`'s `Pick {spots} · to {top}×`) → the shared amount modal
(pre-filled with the member's last Mines stake) → the stake is debited via
`take_stake` and the grid appears in the same ephemeral message.

From there the player presses tiles. A safe tile turns 💎, the multiplier
steps up, and the **Cash Out** button relabels with what is actually banked.
A bomb ends it. The round is over when the player cashes out, hits a bomb, or
reaches the top rung (which cashes out for them).

Nothing about this needs a new interaction model: it is blackjack's live-hand
shape with more buttons and a longer decision.

## The grid: 20 tiles, and why not 25

**5 wide × 4 tall = 20 tiles.** The obvious 5×5 is impossible: Discord allows
five action rows of five components, so a 25-tile grid consumes every
component slot and leaves **nowhere to put the Cash Out button**. A game whose
entire point is the voluntary stop cannot ship with the stop button in a
second message.

So: four rows of tile buttons, and the fifth row is the action row (Cash Out,
and the 🔁 Play Again that replaces it once the round is over). Revealed
tiles stay in place as disabled buttons — 💎 for safe, 💣 for the one that
ended it — so the grid never reflows under the player's finger.

## The risk dial and the ladder

The player picks the bomb count. That is the standard Mines shape and it is
the game's actual content: it makes the same grid a slow grind or a
four-press sprint. It also means **the RTP has to hold across every offered
configuration and every rung of every ladder**, not at one number — 43
distinct (bombs, tiles revealed) pairs, all pinned by exact enumeration.

The fair (zero-edge) multiplier after `k` safe reveals on `N` tiles with `M`
bombs is exactly

```
fair(N, M, k) = C(N, k) / C(N−M, k) = Π (N−i) / (N−M−i)   for i in 0..k−1
```

and the paid multiplier is `round(0.95 × fair, 2)` — one constant house edge
applied to the whole surface, then rounded to two decimals for legibility.
Because the multiplier is a ratio of the same two binomials the reach
probability is built from, `P(reach k) × pay(k) = 0.95` identically: **every
cash-out point on every ladder returns exactly 95%**, and the 2-decimal
rounding is the only thing that moves it. Measured across all 43 rungs, that
rounding keeps RTP inside **[0.9480, 0.9540]** (lowest: 1 bomb at 8 tiles,
1.58×; highest: 1 bomb at 2 tiles, 1.06×) — comfortably inside the 93–97%
band, with no rung anywhere near an edge.

| tiles | 1 bomb | 3 bombs | 5 bombs | 10 bombs |
|---:|---:|---:|---:|---:|
| 1 | 1.00× | 1.12× | 1.27× | 1.90× |
| 2 | 1.06× | 1.33× | 1.72× | 4.01× |
| 3 | 1.12× | 1.59× | 2.38× | 9.03× |
| 4 | 1.19× | 1.93× | 3.37× | 21.92× |
| 5 | 1.27× | 2.38× | 4.90× | |
| 6 | 1.36× | 2.98× | 7.36× | |
| 7 | 1.46× | 3.79× | 11.44× | |
| 8 | 1.58× | 4.92× | 18.60× | |
| 9 | 1.73× | 6.56× | | |
| 10 | 1.90× | 9.03× | | |
| 11 | 2.11× | 12.89× | | |
| 12 | 2.38× | 19.34× | | |
| 13 | 2.71× | | | |
| 14 | 3.17× | | | |
| 15 | 3.80× | | | |
| 16 | 4.75× | | | |
| 17 | 6.33× | | | |
| 18 | 9.50× | | | |
| 19 | 19.00× | | | |

This is `MINES_LADDERS` in `casino_logic.py` — **generated by the formula at
import time, not hand-typed**. A hand-typed table of 43 numbers is 43 chances
to fat-finger the house edge, and the enumeration test would then be checking
a typo against itself. The test asserts the generated table against an
independently written `Fraction`-based enumeration.

### The tail is capped, and that is what makes the four options comparable

Uncapped, a 10-bomb full clear pays **175,518×**. That is not a paytable, it
is an unbounded liability against a ~75k float. The ladder therefore **ends at
the last rung paying ≤ 25×**, and reaching it cashes the player out
automatically.

The cap does not bend the RTP, because every rung it keeps is paid at its own
in-band multiplier and the rung it drops is simply not offered. What falls out
is unexpectedly tidy:

| bombs | rungs to the top | top pays | P(reach the top) | bust chance per press, at the start |
|---:|---:|---:|---:|---:|
| 1 | 19 (the whole field) | 19.00× | 5.00% | 5% |
| 3 | 12 | 19.34× | 4.91% | 15% |
| 5 | 8 | 18.60× | 5.11% | 25% |
| 10 | 4 | 21.92× | 4.33% | 50% |

**All four ceilings are ~19–22× and all four have a ~5% chance of being
reached** — which is not a coincidence but the identity above (`P(top) =
0.95 / pay(top)`) showing through. The risk dial therefore changes *the road,
not the destination*: same ceiling, 19 nervous presses or 4 brutal ones. That
is a much better story than the commercial versions' "pick 24 bombs for a
24× coinflip", and it is the reason to keep 10 as the top option rather than
anything spicier.

Max house exposure per round is `max_bet × 21.92`. At the main guild's
`max_bet` of 1,000 that is **21,920** — against a float last measured near
75k, and against a biggest-payout-ever of 3,000. Nothing in this design is
wrong about that, but it is the single number to look at before enabling
Mines; see "Economy impact".

### Payouts round, they do not floor — and this is a deliberate deviation

Every other table floors (`floor(stake × mult)`). Mines must not, and the
reason is arithmetic rather than taste.

Mines is the only game that pays a **ladder of very small multipliers**. At
the guild minimum bet of 5, flooring a 1.19× rung pays 5 coins for a 5.95
expectation — and the enumerated RTP of that cash-out point collapses to
**0.80**, far outside the band, for a paytable that is exactly 95% on paper.
The band is a promise about what the player actually receives, so a rounding
convention that breaks it at the stakes cautious players use is not a
rounding convention we can keep here.

Worst rung RTP across all four ladders, by stake:

| stake | floor | round-half-up |
|---:|---:|---:|
| 5 | **0.800** | 0.900 |
| 10 | 0.880 | 0.931 |
| 25 | 0.928 | 0.943 |
| 36 (prod average) | 0.933 | 0.942 |
| 100 | 0.948 | 0.948 |

`payout = int(stake × mult + 0.5)`. It costs the house a fraction of a coin
per small win, it can round a 1.06× on a 5-coin stake up to 6 (a rung paying
1.02 rather than 0.95 — player-favourable, which is the correct direction to
err), and it keeps every rung within ~2 points of the design at any stake ≥ 10.
The enumeration test pins the multiplier table in exact rationals; a **second**
test pins the integer-payout drift at `min_bet`, so this can never quietly
regress into a sucker rung.

*If you would rather not deviate from floor:* the alternative is a
Mines-specific minimum stake of ~25, which is a new dial, a new refusal
message, and a worse player experience for the same result. Flagged, not
recommended.

## Cash-out mechanics

**When they can stop:** after any reveal, any time, no cooldown between
presses. Cash Out is **disabled at zero reveals** — at k=0 the ladder has no
rung, and a button that pays 0.95× for doing nothing is a trap. A player who
opens a grid and thinks better of it simply walks away and the idle sweep
hands the stake back in full (below).

**What the message shows, at each step:**

- *Choosing:* four risk buttons with their top multiplier and press count
  ("💣 5 bombs · 8 tiles to 18.60×"), so the shape of the choice is visible
  before any money moves.
- *Playing:* the grid; the current multiplier and **what cashing out pays in
  coins right now** on the Cash Out button itself; the next rung's multiplier
  as an ordinary line in the embed. Showing the next rung is information the
  decision genuinely needs — hiding it would make the player press blind — but
  it is stated, not celebrated: no countdown, no "so close", no escalating
  language as the ladder climbs.
- *Cashed out:* the banked amount, the multiplier reached, tiles revealed.
  **The unrevealed tiles are not revealed.** This is the one place the
  standard Mines UI does something we have a rule against: showing a player
  the safe tiles they walked away from manufactures a near-miss out of a
  decision that was correct by construction. A cash-out card shows what was
  won, never what could have been.
- *Bombed:* the full board revealed, bombs included. This is not the same
  thing — the round is over and the reveal is what makes the loss legible
  rather than a shrug. No "one tile away!" line, no near-miss framing, ever,
  including when it happens to be true.
- Either way, a 🔁 Play Again button carrying the same bombs and stake
  (`casino_again:mines:{bombs}:{amount}`, the existing loop-closer shape,
  where the "side" slot carries the bomb count). On the player's own card
  only — public broadcasts carry no buttons (2026-08-15 decision, recorded in
  `casino_spec.md`).

**A 1.00× cash-out is a push, and says so.** With 1 bomb the first rung pays
exactly 1.00×, and rounding can land other small rungs on the stake exactly.
The result card reads "broke even" in that case. Never "you won" for a payout
that equals the stake — that is the losses-disguised-as-wins rule applied to
its mildest case.

**Bombs are placed at deal time** from `random.sample` and never re-drawn.
There is no adaptive placement, no "the next tile is a bomb because you are
ahead", and a test asserts the reveal order cannot influence the outcome. Not
because anyone suspected otherwise, but because the alternative implementation
(roll each tile as it is pressed) is equally natural to write, statistically
identical, and would leave the house holding a lever it should not have.

## Abandonment: idle, boot, and leaving

Mines is the **third live-hand game**, and this is the part with a hard
constraint on it: `casino-classics-and-prediction-market.md`'s follow-ups
state that a third live-hand game must **extend the `HandTables` descriptor,
never clone it**. That is treated here as binding, and the sections below are
mostly the consequences of taking it seriously.

**Idle → auto cash-out.** The maintenance sweep already runs every 60s. A
Mines hand untouched for `blackjack_idle_seconds` (default 180) auto-cashes at
the multiplier the player actually reached, which is exactly what a manual
press would have paid. This is the war precedent (the idle sweep takes the
player-favourable option) and it has a property worth stating: **walking away
from a Mines round costs nothing that staying would not have cost**. The
game's one nasty edge is that stopping is punished; the auto-resolve at least
does not add to it.

- At **zero reveals** the auto-resolve refunds the full stake instead
  (`kind=REFUND_KIND`, no jackpot feed, no `record_play`) rather than paying
  0.95×. There is no rung, so there is nothing to settle.
- The knob is `blackjack_idle_seconds`, reused, not multiplied — the same
  "one table-idle knob" call war made. The name is now a wart covering three
  games; renaming it means a config-key migration on a live prod key, and a
  staged rename that half-lands is exactly the failure mode
  `casino_pools_takeout_pct`'s prefix note warns about. Leave it.

**Boot → refund.** Mines joins `_refund_live_hands`. A restart kills the
webhook token the ephemeral grid is editable through, so resolving would move
money against a result nobody can ever see; the full stake goes back, exactly
as blackjack and war already do.

**Leaving the guild → refund.** `refund_member_live_stakes` currently
hand-codes a blackjack block and then a near-identical war block, then loops
`ALL_ROUND_TABLES` generically. A third copy-paste is the thing the
never-clone rule exists to prevent, so this is where the descriptor earns its
keep: add `ALL_HAND_TABLES = (BLACKJACK_HANDS, WAR_HANDS, MINES_HANDS)` and a
generic `live_hand(conn, t, guild_id, user_id)`, and the function loops hands
the way it already loops rounds. Net line count goes *down*.

The same applies cog-side. The maintenance sweep has two hand-shaped loops
and the boot sweep two hand-shaped calls; a third game must not add a third of
each. Promote the per-game pieces the cog passes into `_auto_resolve_hand`
(the followup map, the idle resolver, the embed builder, the game label) into
a **`_HandUI` NamedTuple mirroring `_WindowUI`**, and iterate `_HAND_UIS` in
both sweeps — for the reason `_WINDOW_UIS` already documents in its own
comment: *"a game absent from a sweep (stuck stakes after a restart) or a
cross-game id mixup cannot happen by omission or reorder."* Hand tables have
independent id spaces too, so the same keyed-not-zipped discipline applies.

This refactor is Stage 1/2 work, not a follow-up. It is small, and doing it
after the third game ships means doing it with three call sites already
diverged.

## Interaction token expiry

An ephemeral message is editable only through its interaction's webhook, and
Discord expires those tokens 15 minutes after the interaction. Mines runs into
this harder than any existing game, because a 1-bomb ladder is 19 presses and
a cautious player can genuinely spend a quarter of an hour on one grid.

Three separate facts, and only one of them is a problem:

1. **Active play never expires.** Each tile press is its own interaction with
   its own fresh token, and the response edits the message the component is
   on. A player can press tiles for an hour; nothing decays.
2. **Money never depends on a token.** The settle path is a guarded
   `settled_at IS NULL` UPDATE inside the write transaction. Whether the
   player ever sees the result is a rendering question, not a money question —
   the same separation blackjack's `_bj_followups` comment already draws
   ("best-effort render handles — never anything money depends on").
3. **The sweep's handle can go stale, and here it actually will.** The
   auto-resolve edits from outside any interaction, using a webhook handle
   stored in a dict, and `_BJ_FOLLOWUP_TTL` (870s) exists because that handle
   dies at ~15 minutes. Blackjack stores it **once, at deal**, and never
   refreshes it — fine for a hand that lasts seconds. For Mines, a player who
   grinds a 1-bomb grid for 16 minutes and *then* wanders off has a stored
   handle older than the token it names: the auto-cash still pays correctly
   and silently, but their message freezes mid-grid and they learn about the
   payout from their balance.

**The fix is one line of discipline: refresh the stored handle on every tile
press**, not just at deal. `self._mines_followups[hand_id] = (interaction.
followup, message_id, time.time())` on each reveal. Since the idle threshold
(180s) is far below the TTL (870s), a refreshed handle is always live when the
sweep fires, and the player always sees their own auto-cash-out. The stale
entry sweep that already prunes `_bj_followups` past the TTL covers Mines
unchanged.

Worth noting as a design property rather than a defect: the 180s idle clock
means the *round* has no 15-minute problem at all. Only a hand that goes
quiet for three minutes is ever resolved by anything other than the player,
and by then the handle is at most three minutes old.

## Storage (migration 164)

`casino_mines_hands`, the blackjack/war hand shape:

```sql
CREATE TABLE IF NOT EXISTS casino_mines_hands (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    message_id     INTEGER NOT NULL DEFAULT 0,   -- backfilled after send
    user_id        INTEGER NOT NULL,
    stake          INTEGER NOT NULL,
    bombs          INTEGER NOT NULL,             -- 1 | 3 | 5 | 10
    state_json     TEXT    NOT NULL,             -- {bombs: [...], revealed: [...]}
    outcome        TEXT,                         -- cashed|pushed|bombed|refunded
    created_at     REAL    NOT NULL,
    last_action_at REAL    NOT NULL,
    settled_at     REAL                          -- exactly-once guard
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_mines_live
    ON casino_mines_hands (guild_id, user_id) WHERE settled_at IS NULL;
```

One live grid per member per guild, enforced by the partial unique index, not
just a pre-check — the same belt-and-braces blackjack has. Reveal count is
derived from `state_json` rather than stored, so the two can never disagree.
`bombs` is a column because the ladder lookup and the stats need it without
parsing JSON.

**164 is the number as of today; check `src/migrations/` again at build
time.** Two casino sessions are live and either may take it.

**Privacy, per CLAUDE.md's per-commit contract:** this is a new table holding
per-user data, so in the same commit it joins
`economy_service._PURGE_USER_ID_TABLES` (hand-maintained — a new per-member
table is invisible to erasure until added) and gets a row in
`docs/data_register.md` alongside the other `casino_*` hand tables, with the
same decision they carry: **purged**, folded into `econ_purge_user`. The
export side needs nothing — discovery is schema-driven and `user_id` is
already in `SUBJECT_ID_COLUMNS`. Wagering history, behavioral, indefinite, no
message content.

## Settings

One knob: `mines_enabled` (default true), joining the per-game enable row on
the existing dashboard Casino panel — checkbox in `config-casino.js`, field on
the `PUT /api/config/casino` model, `CasinoSettings` dataclass, and the
`_CASINO_BOOL_KEYS` list. `"mines"` joins `GAMES` and `TICKER_GAMES`.

Everything else already applies because the money moves through `take_stake` /
`pay_out`: min/max bet, the daily wager cap, the jackpot cut, the big-win
broadcast bar, and the idle clock.

No new dial for the ladder or the 25× cap. Both are paytable, and the casino's
paytables live in `casino_logic.py` where the enumeration test can hold them
(derby's field is fixed for the same reason). An admin dial that reshapes the
ladder would mean the EV test has to hold for every value it can take — it
would, rung by rung, but the exposure ceiling is exactly the thing that should
not be adjustable from a web form at 2am.

## Hub UI pressure

The hub is at nine game buttons (rows 0–2, three per row) plus My Stats and
How It Works on row 3. **Mines is the tenth**, and there is no clean place to
put it under the current hardcoded `row=` assignments: a fourth game row
collides with the stats/help row.

`casino-hub-row-packing` is changing exactly that, right now. **This design
declares a dependency on it and deliberately specifies nothing about row
placement** — Mines contributes one more entry to whatever packing scheme that
session lands, and Stage 2 here should not start until it has merged. Building
against today's hardcoded rows would mean writing a conflict on purpose.

The only hub requirement Mines has: the button obeys `mines_enabled` like the
other nine, and Mines plays appear on the floor ticker (it is a private game
with no public recap, which is precisely the case `TICKER_GAMES` was widened
for in `c69acb48`).

## Economy impact

The relevant context: the float grew **+3,992/day** measured 08-01..08-05
(`2026-08-06-economy-retune-round2-proposal.md`, awaiting sign-off), the
casino's real burn runs ~5% of handle (94.9% blended RTP measured 07-28..30,
on target), and the casino is a sink under active tuning.

- **Mines is a 95% RTP table, which is the blended edge the casino already
  runs.** Handle that moves from slots or blackjack to Mines is float-neutral
  by construction. Handle that is genuinely *new* (novelty traffic) burns 5%
  of itself, which points the same direction as the round-2 retune. Neither
  effect is large: at a plausible 2–4k/day of Mines handle, that is 100–200
  coins/day of burn, against a +3,992/day problem. **Mines is not an economy
  fix and should not be sold as one.**
- **It feeds the progressive jackpot on the lost portion, like every other
  game** — `_settle_hand` already skims `stake − payout` through
  `feed_jackpot`, which handles Mines' partial returns correctly with no new
  code: a bombed grid feeds the whole stake, a cash-out below stake (only
  reachable via rounding) feeds the difference, and a cash-out above stake
  feeds nothing. Worth remembering that the pot **escrows rather than burns**
  (it re-mints to one winner on a triple 7), so a game that feeds it is
  slightly less deflationary than its hold suggests. The pot was 11.4% of the
  float on 07-30.
- **The exposure number is the one to actually think about.** Top rung 21.92×
  against `max_bet` 1,000 in the main guild is a **21,920** single payout —
  ~7× the largest payout in the casino's history, and a visible fraction of a
  float last measured near 75k (07-30), landing with ~4.3% probability *conditional on* a player pressing all
  four 10-bomb tiles at max stake. Nothing about it is unfair (it is paid for
  by the 95.7% of those attempts that bust), but it is a lumpy, one-wallet
  event of the same character as a jackpot hit. Two levers exist if that reads
  too hot: lower the ladder cap from 25× (a one-constant change, no RTP
  consequence), or lower `max_bet`, which is global. **Recommendation: ship at
  25× and watch it** — the same call the jackpot overhang got. Re-read the
  live per-guild limits before enabling, though: the numbers above are the
  07-27 spec's (main guild `max_bet` 1,000 with `daily_wager_cap` 0; the second
  live guild 100 and 500), and a guild running a 100 max bet has no exposure
  question at all.
- Mines plays land in `record_play` → daily net, member stats, and the floor
  ticker, so it is visible to the tuning report and to Pools' metric
  computation the same day it ships. No metric exclusion is needed; unlike
  Pools, Mines is an ordinary house game.

## Responsible design

This is the section to argue with, and the honest framing is: **Mines is the
most chasing-prone game the casino will contain.** The voluntary cash-out is
simultaneously what makes it interesting and the mechanic that punishes a
player for stopping — every cashed round ends with the player having
demonstrably left money on the table, and the game's own arithmetic says
pressing again was neutral-to-good. That is a fundamentally different pressure
from slots, where the round simply ends.

What this design does about it, inside the game:

- **No near-miss manufacturing.** No reveal of what you walked away from on a
  cash-out; no "one tile away" copy on a bust; no escalating language up the
  ladder. Per the standing rules, and Mines is the game where they bite.
- **No losses disguised as wins.** A 1.00× cash-out reads "broke even".
- **A bounded ceiling.** The ladder tops out and cashes you out; there is no
  infinite climb to keep pressing into.
- **Walking away is free.** The idle auto-cash pays what a manual press would
  have; abandoning at zero reveals refunds in full; a restart refunds in full.
- **No autoplay, no auto-pick, no "reveal random tile"** — see the exclusions
  table.
- The existing controls still apply: the global daily wager cap, lifetime and
  daily net on 📊 My Stats, daily standings on the hub.

### Offered and declined (2026-08-16)

`casino-classics-and-prediction-market.md`'s responsible-design notes name two
mitigations "worth adding alongside these games (not all in v1)": an ephemeral
**reality-check** after N rounds or M minutes showing daily net, and a
member-initiated **casino self-exclusion / cool-off** (tighten instantly,
loosen with delay). Both were offered for Mines specifically, on the argument
that Mines is where they matter most.

**Billy's decision: neither ships with Mines. Both are deferred to their own
commits.** Recorded here the way the Pools spec records its declines, so it is
not relitigated and not silently forgotten:

| Deferred | What it would have done | Residual exposure |
| --- | --- | --- |
| Ephemeral reality-check | A line on the grid embed showing today's net across the casino after N rounds / M minutes. Cheap — `casino_daily_net` exists and the message is already re-rendered on every press | A player mid-chase sees their multiplier and their stake, and nothing about the session. The information exists but is behind a different button, and nobody presses 📊 My Stats while chasing |
| Self-exclusion / cool-off | A member-set casino lockout enforced in `take_stake`, covering all ten games at once | There is no way for a member to stop themselves except willpower. The only ceiling is `daily_wager_cap`, which is **0 (uncapped)** in the main guild |

Note that the classics doc's "Considered and deliberately excluded" list
defers **Crash / "Rise"** — the continuous cash-out-multiplier game — for
needing "genuinely new real-time machinery **and the strongest
responsible-design gating** (session limits, per-game cooldowns, chasing
safeguards)". Mines is a discrete-time Crash. The first half of that reason
does not apply (turn-based, no real-time machinery, it is blackjack's shape),
but **the second half does**, and Mines ships without that gating. That is the
decision above, stated plainly rather than buried: the deferral reason for its
closest relative is half-satisfied and half-accepted.

**What to watch, since nothing is being built to catch it:** a member's Mines
handle running well above their handle on every other table combined; long
same-day session lengths on the ticker; and the daily-standings biggest-loser
line being the same name repeatedly. Any of those is the signal to build the
reality-check, which is the cheaper of the two and the one that fits the
message we are already rendering.

## Considered and deliberately excluded

Recorded with reasons so none of it gets silently rebuilt — and checked
against the classics doc's own exclusion list, which Mines does not otherwise
touch (no craps, UTH, Caribbean Stud, plinko physics, or bingo here).

| Excluded | Why | Revisit when |
| --- | --- | --- |
| Free choice of 1–24 bombs | 24 ladders to pin, and the top end (1 tile for ~24×) is a coinflip wearing a grid. The four-option dial gives the whole range of feels with a testable surface | Never for the extremes; more options is cheap if the four prove too coarse |
| A 5×5 grid | No component slot left for Cash Out (25 buttons is Discord's per-message ceiling) | Discord raises the limit, or the grid moves to a select menu |
| Uncapped ladder / full-clear jackpots | 175,518× on a 75k float. The 25× cap is what makes the four risk options comparable at all | Never |
| **Autoplay / "play N rounds"** | The single most chasing-prone feature in commercial Mines: it removes the decision that *is* the game and converts it into a slot machine with extra steps | Never |
| **"Reveal a random tile" button** | Same objection, one press at a time. It also quietly reframes the game as something happening *to* the player | Never |
| Revealing the board on cash-out | Manufactures a near-miss out of a correct decision (see Cash-out mechanics) | Never |
| Per-tile "hint" / insurance / bomb-removal side bets | Every commercial variant of these is a sucker bet; there is no in-band version | Never |
| A public Mines board or spectator mode | The premise it would be built on is the one prod refuted (`c69acb48`) | Participation changes shape entirely |
| A `mines_max_multiplier` dashboard dial | Paytable belongs in logic where the EV test holds it; an exposure ceiling adjustable from a web form is the wrong thing to make easy | Never — change the constant in a commit, with the test |
| Mines-specific min bet | Would fix the flooring drift, but round-half-up fixes it better and without a new refusal path | Only if the rounding deviation is rejected |
| A dedicated Mines config panel | Casino config is one panel; this is one checkbox | Never |

## Stages

Each stage is a standalone commit with its own tests, in the derby/classics
pattern.

1. **Logic + storage + service.** `MINES_LADDERS` (generated), reveal/settle
   pure functions, and the rounding rule in `casino_logic.py`; migration 164;
   `MINES_HANDS` plus the **`ALL_HAND_TABLES` generalization** of
   `refund_member_live_stakes` and the boot sweep in `casino_service.py`;
   `deal_mines_hand` / `reveal_mines_tile` / `cash_out_mines_hand` /
   `resolve_idle_mines_hand` on the blackjack claim-inside-the-write-transaction
   pattern; `mines_enabled` on `CasinoSettings`; `"mines"` into `GAMES` and
   `TICKER_GAMES`; `_PURGE_USER_ID_TABLES`.
2. **Discord glue.** *Gated on `casino-hub-row-packing` merging.* Hub button,
   risk-picker view, `MinesTileButton` / `MinesCashOutButton` DynamicItems,
   grid embeds, the `_HandUI` descriptor + `_HAND_UIS` sweep generalization,
   followup-handle refresh on every press, Play Again.
3. **Dashboard + docs.** `mines_enabled` through `config.py` + `config-casino.js`
   with route tests; `casino_spec.md`; `manual.html` casino section;
   `docs/data_register.md`; `docs/INDEX.md` (this plan's status row already
   exists — flip it from *design* to *built*, and add Mines to the
   `casino_spec.md` line's game list).

## What the build changed

Recorded so the doc and the code do not disagree, and so the reasoning is not
relitigated.

1. **The hub gate cleared before a line was written.**
   `casino-hub-row-packing` merged as `2aa6cecc` while this doc was being
   signed off, and it made the row question disappear rather than answering
   it: `build_hub_view` now packs rows from the *enabled* set, so Mines needed
   no row wiring at all — just a button in decorator order. What the merge
   revealed instead is that **Mines fills the hub exactly**: ten tables pack
   3/3/2/2, and with the utility row that is Discord's five-row ceiling with
   nothing spare. Twelve tables still fit; a thirteenth does not. Recorded in
   `casino_spec.md`, and the packing test's cases were rewritten around ten.

2. **The idle resolver moved *into* the descriptor.** The first cut had
   `_HAND_UIS` (the descriptor tuple) beside a `self._hand_resolvers` dict,
   with a test asserting the two agreed. That is two lists a fourth live-hand
   game has to be added to, and a test for the disagreement rather than a
   design without one. `_HandUI` now carries `resolver` — the cog method's
   name — so there is a single list to extend, and the test just checks each
   named method exists.

3. **`refund_member_live_stakes` and the boot sweep got shorter, not longer.**
   The never-clone rule paid for itself immediately: the leaver refund's two
   hand-coded blocks collapsed into one `ALL_HAND_TABLES` loop beside the
   `ALL_ROUND_TABLES` loop already there, and the boot sweep's
   `refund_live_blackjack_hands(conn) + refund_live_war_hands(conn)` became
   one `refund_all_live_hands(conn)`. Adding the third game removed lines.

4. **The ladder is generated at import, and the numbers landed exactly as
   designed** — 19/12/8/4 rungs, tops 19.00× / 19.34× / 18.60× / 21.92×, all
   43 rungs in [0.9480, 0.9540]. The design doc's tables were computed the
   same way and needed no correction, which is the point of having written
   them before the code.

## Testing standard

Beyond the casino's standing bar (every guard, exactly-once settle under
replay/boot-sweep/double-click, void→refund):

- **Exact-EV enumeration over all 43 rungs**, in `Fraction`, written
  independently of the generator: `P(reach k) × pay(k)` in [0.93, 0.97] for
  every (bombs, k). This is the design contract.
- **Integer-payout drift at `min_bet`** — the worst rung of every ladder stays
  ≥ 0.90 at stake 5 and ≥ 0.93 at stake 25. The test that fails if anyone
  "fixes" the rounding back to floor.
- **The ladder cap:** no rung above 25×, the top rung auto-cashes, and no
  ladder can be pressed past its end.
- Cash-out at every k for every bomb count pays `int(stake × mult + 0.5)`;
  cash-out at k=0 is refused.
- Bomb placement is drawn once at deal; reveal order does not change the
  outcome; a revealed index cannot be revealed twice.
- **Idle auto-cash** pays exactly what the equivalent manual press pays, and
  at k=0 refunds instead; racing a manual cash-out settles once (assert on the
  *balance*, not the return value — `c69acb48`'s lesson).
- Boot sweep refunds a live grid in full and replays free.
- **Leaver refund covers Mines** — the test that would have failed if
  `refund_member_live_stakes` had grown a third hand-coded block instead of a
  loop, and the same for both sweeps missing a game.
- One live grid per member per guild, including with the pre-check bypassed
  (the partial index is the real guard).
- Jackpot feeds on the lost portion for a bust, on the difference for a
  sub-stake cash-out, and not at all for a winning one — with the jackpot
  enabled *and* disabled.
- Migration 164 proved by real INSERTs in both directions, not by reading DDL.
- Cog tests limited to wiring: the hub button honours `mines_enabled`, and the
  followup handle is refreshed on a reveal (the one piece of glue whose bug
  the service layer cannot catch).

## Open decisions (defaults chosen; flag to change)

1. **Round-half-up instead of floor** (default: yes, deviating from the other
   nine games). The alternative is a Mines-specific min bet.
2. **Ladder cap at 25×** → a 21,920 max payout at the main guild's `max_bet`.
   Lower it to ~10× if that reads too hot; costs nothing but ceiling.
3. **Bomb options 1 / 3 / 5 / 10.** Five options with a 2 would add a gentler
   step between 1 and 3 if the 1-bomb grind proves too slow to be interesting.
4. **Reusing `blackjack_idle_seconds`** for a third game rather than renaming
   it to something honest. Renaming is a live-prod config-key migration.
