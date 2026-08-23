# Meadow Card generator — build plan

**Status: stage 0 (this doc) 2026-08-22.** Stages 1–5 unbuilt.

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

## Stage 2 — the simulator

`sim_logic.py` — `simulate(card, *, games, seats, seed, stake) -> SimReport`.
Seats four `bot_logic.decide()` brains at a headless table, plays to settle or
wall, repeats. No service, no DB, no Discord: `deal` takes the wall, `decide`
takes the state.

`SimReport` per hand: pick rate (how often a seat's best prospect was this
line at Charleston's end), completion rate, mean turns-to-win, mean jokers
consumed, times abandoned mid-hand. Card-wide: wall-game rate, mean game
length, per-tile demand entropy, joker-idle rate.

`scripts/mahjong_sim.py` — CLI over it, table output, `--json`, `--seed`.

**Tests** (`tests/test_mahjong_sim_logic.py`): a two-hand toy card where the
outcome is forced; same seed twice ⇒ identical report (G4); a card whose only
hand is unreachable reports zero completions rather than hanging; game count
and seat count are respected. Keep `games` small in tests — the gate runs this.

## Stage 3 — the generator

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

**Tests** (`tests/test_mahjong_card_gen.py`): every generated candidate passes
`lint_card` (the generator can never emit an invalid card); the selector
honours section quotas; the pivot-neighbour floor holds; seeded output is
stable.

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
