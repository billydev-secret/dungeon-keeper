# Meadow Card generator — build plan

**Status: stages 2–3 and difficulty scoring (4a) built 2026-08-22/23.**
Stage 1 (matched dragons) shipped first from the `meadow-mahjong` branch as
`RankKind.SUIT_DRAGON`, plus a `D2` second-dragon token and `x_parity` — a
superset of what this plan proposed — so this branch's version was dropped
at the rebase. The rest of stage 4 (the measured run, the human naming pass)
and stage 5 unbuilt.

**Spec:** [../meadow_mahjong_spec.md](../meadow_mahjong_spec.md) §3 (card model),
§4 (First Light).
**Builds on:** [meadow-mahjong.md](meadow-mahjong.md) (engine, complete),
[mahjong-bots.md](mahjong-bots.md) (the AI seat this plan drives headlessly).
**Branch:** its own `/dk-feature` session; one merge at the end. Every stage
below is a commit referencing this doc by stage number.

Meadow Mahjong ships one card — **First Light**, 22 hands, hand-authored and
never play-tested at volume. This plan builds the tooling to produce *balanced*
original cards and uses it to produce a full seasonal one (~60 hands).

## Why a generator rather than another hand-authored card

In American mahjong the card **is** the game design: tiles, wall and turn order
are fixed, so every difficulty dial lives on the card. The National Mah Jongg
League tunes theirs with a committee and a year of living-room play by hundreds
of thousands of people. We cannot buy that, and we must not copy its output —
the mechanics are uncopyrightable, the League's *selection and arrangement* of
~60 hands is not (spec §1; `card_logic.py` module docstring). Third-party
"hand tracker" reprints of a League year are the same content in a different
typeface, and every alternative card on the market (Marvelous Mah Jongg, The
Mahjong Line, Oh My Mahjong, The Mahjong Press, Wright-Patterson) is likewise a
priced, unlicensed-to-us compilation. There is no open-licensed American card.

### The public-domain 1920s material is the wrong era, not the wrong licence

Asked and settled 2026-08-22, recorded so it is not re-litigated. Babcock's 1920
rules are public domain and on Wikisource, and the 1924 American standardization
attempt is PD text — but neither contains a card, because the card did not exist
yet. Both score Chinese-classical: any four sets and a pair, basic points for
melds/pairs/flowers, doubles for rarer patterns, against a limit. **The annual
card begins with the League's first one in 1937**, which is outside the PD window
(pre-1931) and would turn on a renewal search nobody here can run.

Two independent reasons the door stays shut even so:

- The engine cannot score that way. We match a rack against a card line and pay
  flat from escrow (spec §2.9); Babcock is points-and-doubles with a limit.
  Adopting it rewrites the win path, the escrow maths and the assist engine,
  which reasons entirely in "distance to a card line".
- The era's named limit hands (Heavenly Twins, Wriggling Snake, Three Great
  Scholars, Thirteen Unique Wonders) are free to use, but there are ~20 of them
  and they are all jackpot-exotic — a card of them is four seats staring at
  unreachable lines.

**Where it is useful:** as flavour at stage 4. A small concealed high-value
section echoing the classic named patterns fits the 75-point top bucket exactly
and needs nobody's permission. Sim decides whether each such line completes
often enough to earn its slot, same as every other hand.

What we *do* have is an engine that can play a candidate card ten thousand times
before a member sees it. Simulation is the substitute for playtest volume, and
it is strictly better than authoring by taste at the things that matter:

| Property a good card needs | How we measure it |
|---|---|
| **Pivot paths** — a blocked player's tiles are already most of the way to another line, so a block is a detour and not a death | Overlap graph over the card; every hand needs ≥2 neighbours sharing ≥8 tiles under some binding. Confirmed in sim by how often a seat switches target and still wins. |
| **Spread demand** — four seats drawing one 152-tile wall should mostly be hunting different tiles, or the discard pool goes dead and tables wall | Per-tile demand entropy across the card; sim's wall-game rate. |
| **Joker economy** — 8 jokers, groups of 3+ only. Kong-heavy cards make jokers king and games fast; pair-heavy cards make them dead weight | Ratio of joker-eligible groups card-wide; sim's mean turns-to-win and joker-idle rate. |
| **Values tracking real difficulty** | Measured completion rate per hand → value bucket. This is the calibration a paper card gets from decades of argument. |
| **Reachability** — a line nobody ever completes is dead ink | Sim's per-hand pick rate and completion rate; anything at zero after N games is cut. |
| **Memorability** | Human, not measurable: the nine-section taxonomy players already know, and Meadow-flavoured hand names. Stage 4 is where a person does this. |

## 0. Decisions this plan makes

| # | Decision | Why |
|---|---|---|
| G1 | **Matched dragons enter the grammar as `D` *with* a suit letter.** `{"rank":"D"}` stays any-one-dragon (unchanged); `{"rank":"D","suit":"a"}` is "the dragon belonging to whatever suit `a` binds to" | It is the one staple American motif the card model cannot currently express, and roughly a fifth of any modern card leans on it (`FFF 2222 DDDD` matching; `33 66 666 999` with the opposite dragon). Reusing `D` + the existing suit-letter machinery is backward compatible — every card that parses today still parses — and it makes "opposite dragon" fall out for free: a `D` on suit letter `b` beside numbers on `a` *is* an opposite dragon, because distinct letters already mean pairwise distinct suits. No new token, no new binding dimension. |
| G2 | The suit→dragon map (`d`→soap, `b`→green, `c`→red) lives in **`tiles.py`**, not the card layer | It is a property of the physical deck, like `Tile.suit`. `card_logic` and `match_logic` both need it, and `tiles.py` is what they already share. |
| G3 | Sim core at `src/bot_modules/games/mahjong/sim_logic.py`; CLI at `scripts/mahjong_sim.py` | The gate runner syncs only `src/`, `tests/`, `scripts/` (D2 of the parent plan), so both paths are visible to CI. `sim_logic.py` is a logic-layer filename, so the scoped gate **hard-fails** without `tests/test_mahjong_sim_logic.py` — which is the right pressure. The CLI/logic split mirrors `scripts/validate_card.py` over `lint_card_data`. |
| G4 | **Every sim run is seeded and reproducible.** The runner takes an explicit seed and an injected wall factory; no `SystemRandom`, no wall-clock | D14 of the parent plan already built the engine for this (`deal` and `rematch` take a pre-shuffled wall; transitions take an injected `rng`). A card's published stats must be re-derivable, or the value calibration is unfalsifiable. |
| G5 | The generator proposes; **a person disposes**. Stage 4 output is a candidate pool with measured stats, and the shipped card is a human selection from it with human-written hand names | Names and section flavour are the creative layer and the reason First Light reads like TGM rather than a spreadsheet. Also our defence that the selection is ours: it is, demonstrably, chosen and named by us from our own generated pool. |
| G6 | Ship the new card **alongside** First Light, not replacing it | `mahjong_cards` is already per-guild with active/scheduled/archived states and a dashboard upload→lint→activate flow (parent plan stage 6). Two cards is the feature working as designed, and First Light stays as the small-card option. |
| G7 | Sim also re-scores **First Light** | 22 hands authored blind. If the tooling says three of them never complete, that is worth knowing and cheap to learn once the harness exists. |
| G8 | No migration, no new table, no `data_register.md` row | The card is rows in the existing `mahjong_cards`; the generator stores nothing per-user. Stage 5's only compliance surface is `manual.html`. |

### The footgun to write down

Card suit letters are `a`/`b`/`c` (abstract, "distinct suits", bound at match
time). Physical tile suits are `d`/`b`/`c` (Dot/Bam/Crak, `tiles.SUITS`).
**`b` and `c` mean different things in the two namespaces.** Every function
touching G1's suit→dragon map straddles both, so name parameters
`card_suit` / `tile_suit` there and never bare `suit`.

---

## Stage 1 — matched dragons in the grammar

`tiles.py` — `SUIT_DRAGON: dict[str, Tile]` and its inverse (G2).

`card_logic.py` — `RankKind.MATCHED_DRAGON`; `_parse_group` accepts a suit
letter on `D` and stops rejecting it as "suitless — drop the suit";
`_demand_key` gains a `("matched_dragon", card_suit)` key. Note this key is
*binding-dependent* in a way the existing keys are not: `2(D)a` and `2(soap)`
collide only when `a` binds to dots. The existing docstring already reasons
about exactly this case for `ANY_DRAGON` and rules it acceptable — the player
picks the bindings, so a coincidental collision never makes a line unwinnable.
Same reasoning, recorded again for the new key.

`match_logic.py` — `_bindings` already enumerates suit maps, so a matched
dragon resolves per suit map with no new binding dimension (G1). Touch
`_group_natural`, `_demand_of`, `_line_reachable` and `_line_prospect`.
`bot_logic` inherits it for free through `closest_lines`/`suggest_discard`.

Spec §3.1's rank grammar table gains the row.

**Tests** (`tests/test_mahjong_card_logic.py`, `tests/test_mahjong_match_logic.py`):
`D`-with-suit parses and `D`-without still means any-dragon (a `pytest.param`
row on the existing grammar table, per CLAUDE.md's prefer-a-param-row rule);
a matched-dragon hand matches only when the dragon agrees with the bound suit;
an opposite-dragon hand (`D` on a different letter) refuses the matching one;
First Light still loads byte-identical.

## Stage 2 — the simulator ✅

`sim_logic.py` — `simulate(card, *, games, seats, seed, stake) -> SimReport`.
Seats four `bot_logic.decide()` brains at a headless table, plays to settle or
wall, repeats. No service, no DB, no Discord: `deal` takes the wall, `decide`
takes the state.

`SimReport` per hand: pick rate (how often a seat's best prospect was this
line at Charleston's end), completion rate, mean turns-to-win, mean jokers
consumed, times abandoned mid-hand. Card-wide: wall-game rate, mean game
length, per-tile demand entropy, joker-idle rate.

`scripts/mahjong_sim.py` — CLI over it, table output, `--json`, `--seed`.

**Built with three changes to what this section assumed.** (a) A game costs
seconds of bot thinking — the assist engine walks every line's bindings at
every decision — so `simulate` seeds **per game** from `(seed, index)` and
takes a `workers` argument; a run's result is independent of the worker
count, and shard-plus-merge is tested directly rather than by spawning a
pool in CI. (b) `Tile.__hash__` is now the identity hash (enum's own hashes
the member *name*); tiles are hashed into Counters millions of times per
game and this took ~20% off a run — it speeds the live matcher too.
(c) Per-hand joker *counts* are not recoverable from `Outcome`, so the joker
economy is read from `jokerless_wins` instead, which is recorded. Note
`wins_per_target` is not a probability and routinely exceeds 1: `targeted`
counts only the opening target, and pivots are the point.

**Tests** (`tests/test_mahjong_sim_logic.py`, 22 cases, ~6s): two-line cards
at two games each, for the reason above. Determinism per seed, distinct
streams per game index, shard+merge equalling the serial run, an unwinnable
card walling out instead of hanging, every game ending in exactly one
outcome at both seat counts, the health flags, and the report ordering.

### What it already said about First Light (G7)

96 games, 4 seats, seed 3: **53% wall games** and **14 of 22 lines never
won**. The worst finding is `qp-2` (The Long Trail) — the *most* popular
opening target at 136 seat-hands, and zero wins. A line that attracts
players at the deal and then strands them is the exact failure the
generator exists to prevent, and First Light has one. Duel is the opposite
extreme: 2 seats, no trim, 100% mahjong rate. Both numbers want a bigger
run before anything is concluded, but the tooling is clearly reading
something real.

## Stage 3 — the generator ✅

`card_gen.py` (logic-layer name, so it needs its own mapped test) —

1. **Enumerate** candidates from a motif grammar over the nine familiar
   sections (year, 2468, like numbers, quints, consecutive run, 13579,
   winds+dragons, 369, singles+pairs). Motifs are mechanics, not anyone's
   selection.
2. **Filter** through the existing `lint_card` — tile total, supply
   feasibility, joker deficit, the uncallable-exposure rule — plus the
   near-duplicate shape signature, which already canonicalizes over suit
   relabeling.
3. **Score and select** ~60 for the objective in the table above: demand
   entropy, ≥2 pivot neighbours per hand, section balance.

`scripts/generate_card.py` — CLI: emit a candidate card JSON + its sim report.

`scripts/generate_card.py` — CLI: emit a candidate card JSON + a pool
breakdown, and report any stranded line rather than hiding it.

**Tests** (`tests/test_mahjong_card_gen.py`, 23 cases, ~8s): every candidate
lints clean, is exactly 14 tiles, and is shape-deduped; the pool and the
selection are both deterministic; section caps hold; no two chosen lines are
clones; no section prints one line with two tails; Singles & Pairs really is
all pairs and concealed; values stay in range; and the metrics
(`overlap`, `stutter_key`, `provisional_value`, `pivot_report`) have their
own cases.

### Three defects the first generated card exposed

Worth recording, because each is a rule the selector now enforces and none
was in the plan as written:

1. **Stutter.** The first card opened with six Year lines identical but for
   their two-tile tail. The linter's shape signature cannot catch it —
   swapping a flower pair for a wind pair genuinely changes the shape. A
   uniform "too much overlap" bar was the wrong fix: *Like Numbers* is
   self-similar by definition and a flat bar starved it to one hand. The
   rule that works is `stutter_key` — a line's identity is its **numeric**
   groups, so two same-section lines with the same numbers and different
   tails are one line; a pure honours line, having no numeric part, is
   keyed by its whole shape instead.
2. **A section that lied.** Padding ran a generic filler onto every core,
   so *Singles & Pairs* was emitting hands containing kongs — and, having a
   callable group, they were not even concealed. That section now pads only
   from pairs.
3. **Stranded lines.** The greedy fills sections in order, so the first
   section chooses against an empty card and can only be judged once the
   rest exists. `_repair_stranded` runs afterwards and swaps a stranded line
   for the best-connected alternative in its own section; what it cannot fix
   it reports. Current output: 56 hands, 1 stranded, lint-clean with no
   warnings, and it passes `scripts/validate_card.py` independently.

Selection also caches each candidate's token counter and stutter key — the
selector compares every remaining candidate against every chosen hand at
every pick, and recomputing them made it the slowest thing in the module by
an order of magnitude (23s → 8s across the tests).

## Review round R1 (2026-08-22, after stage 3)

`/code-review` over stages 0–3 plus the `--remote` work. Eleven findings,
all real, all fixed in the same round. The four worth carrying:

| # | Finding | Fix |
|---|---|---|
| R1 | A suited `D` on a letter **no other group uses** is identical to a suitless `D` — the suit map is free, so both range over all three dragons. Both spellings sat in the pool with different shapes and different tokens, so nothing caught them, and at seeds 19 and 20 the *same hand was printed twice on one card*. | `_normalise` rewrites such a group to a bare `D` before it enters the pool. It also stops the card misleading a reader, who takes a suit letter to mean a constraint. |
| R2 | The year cores paired a repeated digit with an all-`a` suit pattern, so `3(2)a 3(0) 3(2)a 3(6)a` demanded **six copies of a four-copy tile** and burned two jokers by construction. Lint-clean, but it reads as a printing error. | `_is_degenerate` rejects two groups of one physical tile — *unless both are singles*, because writing the year out twice is how a year hand is spelled and four singles of one tile is exactly its natural supply. |
| R3 | Section slugs took the first character of every word including the ampersand, so "Winds & Dragons" became `w&d-1`. These ids are stored on `mahjong_results.line_id` and shown to members in the reveal embed. | Alphanumeric initials only. |
| R4 | Two silent generator dead-ends: the year-written-**once** family produced a 4-tile core with no 10-tile filler to complete it, so every one was discarded; and both `_pairs_cores` loops clamped to the same length, so the longer runs were generated twice and the intended ones never at all. | A `_FILLERS[10]`, and loops that range over what actually exists. |

The rest: `merge_into` now folds `games` too (it is public API, and a caller
merging two independent reports was getting rates over the first one's game
count); `format_report` reports `other_ends`, which was previously visible
only through `--json`, so a run could read as "0% everything" with no clue
where the games went; the INDEX row was still saying stages 1–5 unbuilt; and
the `--remote` findings are recorded in that commit.

Two things the review checked and cleared, worth not re-litigating:
`MATCHED_DRAGON` is handled everywhere `RankKind` is branched on, and
`Tile.__hash__ = object.__hash__` is safe — `Tile` is a plain `Enum`, not a
`str` mixin, so equality is identity, and the engine's only `set[Tile]` is
used for membership and never iterated.

## Stage 4a — difficulty scoring ✅ (2026-08-23)

Stage 4 assumed each line's price would come from a measured completion
rate. That does not survive contact with the dashboard: an admin uploads a
card and wants to see it *now*, and a 56-hand card needs thousands of games
to measure. So the simulator was used to **calibrate a cheap formula**
instead of to price cards directly — the expensive measurement happened
once, offline, and `card_logic.difficulty()` is what ships.

Six terms, all facts about the hand, all individually explainable to a
member (`reasons` names the ones that moved the score):

| term | weight | why |
|---|---|---|
| tiles in groups no joker may fill | ×1.0 | dominant — must arrive as specific naturals |
| tiles in groups too small to call | ×0.8 | strictly worse: cannot even be claimed (§2.5) |
| concealed | +3 | forfeits calling entirely |
| distinct suits | ×1.5 | more separate tiles to chase |
| tiles past the natural supply | ×2.0 | must be jokers, of which 8 exist for the table |
| rank variables | −2.0 | binds where your tiles already sit |
| flowers | −0.5 | eight copies, the cheapest tiles there are |

**`LONG_SHOT_SCORE = 12` is calibrated, not chosen.** Across First Light and
a generated card — ~150 measured games — lines scoring ≥12 took **295
opening picks and won nothing**, while all 63 wins came from below. The
threshold was fitted on First Light and held on the generated card as a
held-out test. The separation is stable anywhere in [12, 14]; at 10 winners
start leaking through. `tests/test_mahjong_card_logic.py` pins the exact set
of First Light lines it flags, so changing a weight fails the suite and
forces a re-run of `scripts/mahjong_sim.py`.

**Two bands, not five.** Only the long-shot boundary is validated; a
finer scale would imply resolution the data does not support.

**Values now derive from the score** (`card_gen.line_value`), replacing an
ad-hoc structural formula that could drift from whatever the card viewer
showed. Note the correction this pass forced: First Light's values are *not*
inverted against difficulty — they correlate at r = 0.64, which is pricing
working roughly as intended. The problem was never the prices.

`select(max_long_shots=N)` budgets the jackpot lines. Unset, the count is
whatever the flat section quotas produce, which is how a pool that is 0.6%
zero-jokerable produced a card that was 12% — the Singles & Pairs quota
dragged every one of them on. A card *wants* some, so this is a budget, not
a ban. The repair pass honours it too (found by its own test).

Surfaced in `scripts/generate_card.py` (per-line score, band and reason) and
`scripts/validate_card.py` (long-shot count for a card author). **Not yet
member-facing** — the card viewer and the assist embed are the next step,
and being member-visible they owe `manual.html` and a QA card.

## Stage 4b — what actually makes a card easy, and how long a hand takes

Measured 2026-08-23/24. This section exists because almost every intuition
in the earlier sections turned out to be wrong in a specific, useful way.

### The external anchor

I Love Mahj publish statistics from millions of games on real NMJL cards:
**7% wall games on the 2024 card, 10.7% on 2022**, and a card's *playable
hands* (concrete instantiations, not printed lines) rose 756 → 1,683 between
those years. They also report that **"2 kongs + 2 pungs" is 57.96% of all
wins**, and that wins concentrate in three sections (Consecutive Runs 28.6%,
Winds/Dragons 21.1%, Any Like Numbers 19.2%).

Against that, our cards were producing dead hands four to six times too
often: First Light 40–45% wall, generated cards 49–64%.

### An easy card closes the whole gap

A deliberately extreme card — 20 lines, **every group 3+** so all fourteen
tiles are joker-eligible, rank variables and any-dragons throughout, nothing
concealed, no pairs — measured **6.4% wall at 500 games**, matching real
play. So the card is the whole story: the bots are not the bottleneck, and
my hypothesis that `_call_tiles` was under-calling is dead.

Ingredients, by measured effect on playable hands per line (pool median 6):
**rank variables ×5** (median 30), **any-dragon ×3** (median 18), suit
letters barely matter at all. But flexibility is not the only route: the
*Winds & Dragons* family has a median of **one** instantiation and still
produces a fifth of real wins, because it needs few distinct tiles and every
group takes a joker. A card wants both kinds.

Two failure modes the easy card exposed: with everything easy every line
prices at 25–30, so there is no jackpot; and **flexibility is competitive** —
put 30-instantiation runs beside 1-instantiation honours and nobody ever
ends up closest to an honours line, so that whole section went dead.

### Hand length is entirely a function of wall rate

| card | wall % | turns (all) | turns \| win | turns \| wall |
|---|---|---|---|---|
| easy-mode | 3.5% | 71.6 | 70.5 | **100.0** |
| First Light | 45.2% | 86.8 | 76.0 | **100.0** |
| generated (57 lines) | 48.5% | 86.9 | 74.6 | **100.0** |

**A wall game is exactly 100 discards.** Not approximately — identically, on
every card. The deal takes 53 of 152, leaving 99, and a dead hand consumes
all of them plus the opening discard. Confirmed on the first real game
played on prod (table 10, 100 discards exactly).

A *winning* hand takes 70–76 turns whatever the card. So:

    mean turns = wall% x 100 + (1 - wall%) x 73

which reproduces every measurement to a tenth. Every 10 points of wall rate
adds ~2.7 discards. There is no speed-versus-difficulty trade-off to
manage: easier **is** shorter, and nothing else about the card moves length.

### The real clock, from prod

Table 10, 2026-08-23: four humans, one hand, **wall game**, 100 discards,
78 minutes from lobby open to settle (68–78 minutes of play). That is
**~45 seconds per discard**, against the 18s I had been assuming — every
wall-clock estimate before this was wrong by 2.5x. Nobody took a strike, so
players were deliberating, not idle; think time is ~37s and the claim window
~8s. Only **two exposures** were made in the entire four-player hand.

At 45s/discard: a wall game is ~75 minutes, a winning hand ~55, First Light
averages ~65. **A four-seat hand is an hour-long activity as built.**

Claim windows are 100 per hand at up to 8s — a sixth of the hand — and
measured over 964 windows, **only 31.5% of responder-slots can legally act
and 28.7% of windows have nobody who can**. Auto-passing seats with no legal
route is the largest single engine-side saving available.

### Shortening the wall: two mechanisms, only one works

| easy card, 4 seats | mahjong | wall | mean turns |
|---|---|---|---|
| 3 suits, no trim | 93.0% | 7.0% | 71.3 |
| 3 suits, trim 15 | 78.0% | 22.0% | 69.4 |
| 3 suits, trim 25 | 58.7% | 41.3% | 66.5 |
| 3 suits, trim 35 | 32.0% | **68.0%** | 61.1 |
| **2 suits (Bams dropped, 116 tiles)** | **90.7%** | **9.3%** | **45.1** |

**`wall_trim` truncates; dropping a suit accelerates.** Trim does not help
anyone finish sooner — winning hands still take ~67 turns — it just removes
the tiles they would have finished with, converting wins into walls. 35
tiles of trim costs 61 points of win rate to buy 10 discards.

Dropping a suit makes winning hands genuinely faster: **66.8 → 43.2 turns to
win**, because a needed tile is 31% denser in every draw. The win rate
barely moves because nobody was using the tail of the wall anyway. It costs
59% of the card's playable hands (571 → 235) and the card may then use at
most two suit letters.

Note this refutes the earlier claim that winning-hand length is invariant:
it is invariant *across cards on one deck*, and moves a great deal when the
deck changes.

**Recommendation:** `wall_trim` should be 0 and the prod checklist's value
of 60 must not be applied — it would make three Duels in four end dead. A
two-suit deck belongs in `TableConfig` as a table mode beside `seat_count`,
not as a global rule change; `SUITS` is currently a module constant read
inside `match_logic._bindings`, so making it per-table is the same plumbing
problem as `RANK_BY_EFFORT`, and a two-suit table needs its card checked for
three-letter lines at activation.

### The 20-minute budget

    2-suit easy card              45.1 discards x 45s = 34 min
    + auto-pass ineligible seats                x 39s = 29 min
    + claim_window 8 -> 4                       x 37s = 28 min
    + think time 37s -> 20s (UI)                x 24s = 18 min

Card and engine work together reach ~28 minutes. The remaining gap is
entirely **think time**, which is a user-interface number and the one thing
nobody has measured. D18 has ephemeral panels summoned rather than pushed,
so every turn costs an Open Rack click, an ephemeral round trip, a read, a
pick and a confirm. Whether 37s is mechanics or deliberation decides whether
20 minutes is a UI job or a game-design one — and **nothing records per-turn
timestamps**, so that must be instrumented first (`started_at` and
`discards` on `mahjong_results`, and practice tables recording results at
all: nine practice games on 08-23 stored nothing).

## Stage 4 — generate, tune, author the card

Run stage 3 at volume. Set each hand's value from its measured completion-rate
quantile, bucketed into the 25–75 range `card_logic.VALUE_MIN/MAX` already
enforces. Cut anything that never completes. Then the human pass (G5): pick the
final line-up, name the hands and sections in Meadow voice, write
`cards/meadow_<season>.json`.

Also run G7 over First Light and record what it says.

**Tests**: the new card loads and lints clean, and — like First Light — has a
committed expected hand count and section list, so an accidental edit is caught.

## Stage 5 — ship

No new surface: the dashboard already does upload → server-side lint → set
active / schedule / archive, and the member card viewer already renders any
loaded card. So this stage is documentation plus the QA card:

- `manual.html` — the mahjong section names the second card and how to switch.
- Spec §4 gains the new card; INDEX.md's mahjong row notes two cards.
- `Testing:` lines for the QA card: activate the new card on the dashboard,
  see it in the card viewer, play a hand against it.

## What this plan is not

- **Not** a route to reproducing any published card. Nothing here reads,
  imports or transcribes third-party card data; the generator's input is a
  motif grammar and the linter, and its output is ours (G5).
- **Not** a change to how cards are stored, uploaded or activated — that all
  shipped with the parent plan.
