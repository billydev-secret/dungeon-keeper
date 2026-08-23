# Meadow Mahjong — assistance modes (addon plan)

**Status: complete 2026-08-22** — stages 1–4 landed (engine 43e7040b, prefs
d431fe2b, surface 260b0b25, review fixes with the stage-4 commit; the review
round's three confirmed findings are R1–R3 below). Addon to the shipped v1
([meadow-mahjong.md](meadow-mahjong.md), complete 2026-08-21). Still
merge-gated on the base game passing live QA.

**Spec:** [../meadow_mahjong_spec.md](../meadow_mahjong_spec.md) — v1 is silent
on hints of any kind, so this breaks no existing decision. Stage 3 adds a §
to that spec rather than leaving the behavior undocumented.
**Branch:** continues on `meadow-mahjong`. **Do not merge before the base game
has passed live QA** — assistance is unobservable until the dashboard panel
loads, a card is active and a table can actually be seated.

Whenever a player is choosing tiles, show them how close their rack sits to
each line on the active card. Four levels, each player picks their own.

| Mode | Shows |
|---|---|
| `off` | nothing — pure card-reading |
| `target` | the closest lines and how many tiles away each is |
| `gap` | ...plus which tiles are still needed |
| `coach` | ...plus dead weight and a suggested discard |

---

## 0. Decisions

| # | Decision | Why |
|---|---|---|
| A1 | Four levels, **per player**, remembered across games; guild-scoped | The user's call, taken twice with the cost in front of them: first over an always-on build, then over a cheaper seat-scoped one. Consequence accepted: two players at one coin table can hold different information. |
| A2 | Distance = `14 − matched`, minimised over every binding **and** exposure assignment | Reuses `_bindings` / `_group_natural` / `_exposure_assignments` wholesale. Naturals serve pairs/singles first (jokers are barred there by §2.6), then joker-eligible groups; held jokers cover what naturals could not. One new pure function in `match_logic.py`, beside the two predicates that already share that enumerator (D9 of the v1 plan). |
| A3 | Rank by distance; exclude unreachable lines rather than filter by reachability | Measured on First Light: **all 22 lines stay reachable well into a hand**, because unseen copies plus jokers keep nearly anything alive. Reachability is therefore a near-useless *ranking* signal and only worth using as an exclusion. If every line is dead the readout says so — it never invents a target. |
| A4 | Show the top 3; tie-break distance → value descending → card order | Distances cluster hard (a sampled 13-tile rack scored 8, 9, 10, 10, 10), so ties are the common case, not the edge case. Without a deterministic tie-break the readout would reshuffle between renders of an unchanged rack. |
| A5 | Jokers are never dead weight and are never a suggested discard | A joker is always redeemable or tradeable, so it is never the right tile to throw. Easy to get wrong from a naive "tiles not consumed by the best line" set. Named test. |
| A6 | `coach`'s suggested discard must skip any tile that visibly completes another seat's exposure | Suggesting a tile that hands someone else the hand loses a player real coins on the bot's advice. The rail is cheap — exposures are already public state. Remaining heuristic: of the dead weight, the tile appearing in the fewest live lines. |
| A7 | No caching | Measured 4.3 ms for a full 22-hand scan of a 13-tile rack (0.45 ms for the existing reachability pass); re-measured 2026-08-22 on the 71-hand Harvest card at ~37 ms per scan, worst bot decide ~186 ms — still far inside any interaction budget, including the 6–8 s claim window. A cache keyed on rack state would be more code and more staleness risk than it saves. |
| A8 | Default for a member who has never chosen: `gap`, overridable by a guild dial | Assistance-by-default matches the user's stated instinct ("everyone, always on"); the dial exists so a house can make pure play the norm without a code change. One select on the existing dashboard panel, enforced — not a dead toggle. |
| A9 | New table `mahjong_prefs`, PK `(guild_id, user_id)` | The house pattern is a small per-feature prefs table (`econ_notify_prefs` is three columns, same key shape); DK has no member-settings hub to hook into. Purged on erasure — a preference tied to a member id is personal data with no Art 17(3) ground to keep. |
| A11 | `needed` may honestly name a tile with zero unseen copies | Found by stage-1 testing: a 3+ gap stays fillable by *drawn jokers* after the last natural is seen, so the line is live and the distance real — hiding the tile would misstate the gap, and pinning this beat guessing. Named test: `test_a_drawn_joker_keeps_a_binding_alive_at_zero_copies`. |
| A10 | The member surface is a **My Settings** button on the existing `/mahjong` panel, opening an ephemeral menu; assistance is its first tenant | `MemberPanelView` already carries Create Table / Card Viewer / My Stats. A single-purpose Assistance button works for one setting and sprawls at the second — a container costs nothing now and means no future per-player preference has to touch the play panel again (CLAUDE.md: collapse controls; one ephemeral panel over a sprawl). Scoped to mahjong, not a bot-wide member hub — DK has none, and inventing one is not this addon's job. A personal play setting is member self-service and so belongs in Discord, while the *house default* of A8 is admin config and belongs on the dashboard. |

---

## Stage 1 — the engine

`closest_lines(concealed, exposures, card, seen_elsewhere, limit=3)` in
`match_logic.py`, returning per line: the `Hand`, the distance, the still-needed
tiles, and the held tiles the line does not consume. Pure; no Discord, no db.
`suggest_discard` and `dangerous_tiles` land here too (pure engine — stage 3
stays glue), plus a 20-seed invariant sweep pinning closest_lines' line set to
exactly reachable_lines' and distance 0 to exactly match_hand's verdict.

Per binding and exposure assignment, demand is aggregated **by tile** rather
than by group — two groups in one line can resolve to the same natural, and a
per-group walk would double-count the naturals covering them.

**Tests** (`tests/test_mahjong_match_logic.py`): a known rack against a known
card at every distance; a complete hand scores 0; exposures lock their groups
and an unabsorbable exposure kills the line; jokers cover 3+ groups but never
pairs/singles; jokers never appear in dead weight (A5); dead lines excluded and
the all-dead case; tie ordering is deterministic (A4).

## Stage 2 — the preference

Migration **176** — `mahjong_prefs (guild_id, user_id, mode, updated_at)`,
PK `(guild_id, user_id)`. *Re-check the number at rebase:* no branch holds 176
today, but 141 and 145 both collided this way, and differently-named files
produce no textual conflict.

Service read/write in `mahjong_service.py`; `purge_user_data` deletes the row;
`data_register.md` gains a row (purged, no Art 17(3) ground); `manual.html`
§Your Data & Privacy gains a line. `user_id` is already a conventional name in
`privacy_service.SUBJECT_ID_COLUMNS`, so the export sees the table for free.
Guild default dial (A8) is a `mahjong_*` config KV key with a writer on the
existing dashboard panel — a key without a reader is a silent no-op.

**Tests** (`tests/test_mahjong_service.py`): round-trip; default when unset;
guild default respected and overridden; unknown/corrupt stored mode falls back
rather than raising; purge clears the row.

## Stage 3 — the surface

Readout threaded into the six decision points: turn/discard (primary), claim
window, Charleston pick, courtesy, joker redemption, rack panel. One builder in
`embeds.py` renders a mode's block so the six callers cannot drift apart.

Honest limit to note in the manual: at deal time every line sits 8–10 away and
the ranking is close to noise. It earns its keep mid-hand.

Same commit: the `/mahjong` **My Settings** button and the ephemeral menu behind
it (one select today — the mode); `manual.html` player-guide section; a new § in
the mahjong spec.

**Tests**: embed builder per mode (including `off` rendering nothing); My Settings
reflects the stored mode and writes the chosen one back; the A6 safety rail — a suggested discard never names a tile completing a visible
exposure; cog wiring for the new button.

## Stage 4 — gate + QA

Full suite, eslint/stylelint if dashboard assets moved, scoped browser checks
for the dial. QA card written for a volunteer tester.

### Review round (2026-08-22)

An adversarial multi-agent review over the three addon commits (4 lenses,
2 isolated skeptics per finding, every verdict backed by an executed
reproduction) confirmed three defects — all four lenses independently
converged on the first:

| # | Finding | Fix |
|---|---|---|
| R1 | **The claim-window readout counted the live claimable discard as gone.** `_place_discard` appends to `state.discards` before the window opens, so the one tile the phase exists to acquire read as extinct — a member one 8c short of Golden Hour, with the winning 8c live, was shown a sister-binding lie ("distance 6, need 8b×2") or even "play for the wall" while the winning claim sat in front of them. | `assist_readout` discounts the live discard from `seen` for every seat that may claim it; the discarder keeps the unadjusted view (their tile can never come back). Named test: `test_claim_window_live_discard_counts_as_obtainable_for_claimants`. |
| R2 | **"Dead weight" printed the top hand's list unqualified** while the copy promises "tiles none of your closest hands can use" — the same tile could print as *needed* by hand #2 and *dead* in one field. | The embed renders the intersection across the hands actually shown (order preserved from the top hand); the suggestion machinery is untouched — it already scored across all live lines. |
| R3 | **The 1024 bound was a blind slice.** With the prod emoji map registered (~28 chars/tile), ~10% of fresh-deal coach readouts overflow; the slice cut mid-`<:mm_…:id>` token and silently dropped the "Consider discarding" line. Invisible in dev because the map ships empty. | `_assist_field` guarantees the bound itself: emoji at full width, then text chips, then fewer hands — content over glitter, never a cut token. The test renders under a fake 19-digit-id map. |

### Review round 2 (/code-review, 2026-08-22)

A second, independent review over the finished addon confirmed 11 findings
(3 more candidates refuted). The three commits working the queue:

| # | Finding | Fix |
|---|---|---|
| F1 | **The fallow-settle payout had R1's bug in the money path**: `_settle_fallow_end` valued the survivor's hand with unadjusted seen, so a leaver folding mid-claim-window could change real coin payouts when the live discard was the survivor's last needed copy. | `obtainable_seen()` — one shared view for the readout AND the settlement; they can never again disagree. Regression test pays 25, not 50. |
| F2 | **Coach contradicted its own readout** (~16% of coach racks): the suggestion came from the closest hand's dead weight alone and could name a tile a shown sister hand still needed. | Candidates now come from the dead-weight *intersection* of the shown hands — provably disjoint from every shown need list (excess ⇒ zero deficit). Sweep-pinned. |
| F3 | A **fallow seat's exposures** gated the coach although a folded seat threatens nothing. | The danger set skips fallow seats. |
| F4 | The claim-window discount applied to a seat that had already **passed** on the tile. | `obtainable_seen` checks the recorded claim response; a pending call/mahjong keeps the discount (that tile is incoming). |
| F7 | The mode vocabulary was enumerated in five places, three unpinned. | `ASSIST_MODES` lives in game_logic; service re-exports, route derives its pattern; gate + route pins added. |
| F8 | The equivalence sweep never exercised **exposures** — the liveness duplication's hardest branch. | Odd seeds now carry a locked exposure (some with a joker) through both functions. |
| F5/F6 | Three DB round-trips per rack render; pure gates ran after the paid work. | `assist_relevant()` gates before any I/O; `get_assist_mode` reads one dial key instead of nine; parsed cards cache by row id + json hash. |
| F9–F12 | `dangerous_tiles` rescanned the card per opponent; `_TILE_ORDER` duplicated `tiles._ORDER`; the mode clamp existed twice; a redundant `conn.commit()`. | Enumeration hoisted; `TILE_ORDER` public in tiles.py and shared; `_assist(raw, fallback)`; commit removed (open_db commits on exit). |

A verify pass over the three fix commits (3 isolated skeptics, executed
attacks) then caught round 2's own bug and one toothless test:

| # | Finding | Fix |
|---|---|---|
| V1 | **`obtainable_seen`'s discount was unconditional** for any non-passed claimant, but §2.5 gives a seat only two routes to a live discard: an instant Mahjong, or a call stood up by two matching rack tiles. A pair-mate two tiles out has no route (pairs can't be called), and a discarded **joker** is unclaimable by anyone — the naive discount resurrected dead lines and, since fallow pays the *minimum* live line, **underpaid** survivors. | The discount now requires a legal route: `match_hand(rack + tile)` or pool (matching naturals + jokers) ≥ 2; jokers never discount. Route table + both underpay repros are named tests. Fixture lesson pinned alongside: sister-suit bindings must be exhausted too, or reachability just re-binds the line. |
| V2 | The F6 wiring test was **toothless**: its exploding stub's `AssertionError` was swallowed by `_assist_for`'s never-raise contract, so it passed on pre-gate code. | Counter-based stub asserting zero service calls; verified to fail with the gate removed. |

Observation left standing (pre-existing v1 behavior, out of scope): a seat
that records a claim and *then* goes fallow can still win that one in-flight
window — `_resolve_claim_window` doesn't filter fallow. Self-healing (the
turn timeout path recovers), touched by no addon commit, noted here so a
future reader doesn't rediscover it as an assist bug.

One process note stood: the stage-2 dial commit broke the same-commit
manual/Testing letter (mitigated — stage 3 closed both 11 minutes later,
and the QA card assembles per branch). Refuted and dropped: pydantic
trailing-newline, CHARLESTON_VOTE-in-_ASSIST_PHASES-as-bug, TableError log
spam (the triggering state is unreachable without hand-written SQL).

---

## What this addon does **not** do

- No table-level or host-level control (considered, rejected in favour of A1).
- No seat override of the stored default (considered; costs a precedence rule
  to test for little gain).
- No dashboard panel for the personal setting — that is member self-service.
