# Meadow Mahjong — assistance modes (addon plan)

**Status: planned 2026-08-22** — not started. Addon to the shipped v1
([meadow-mahjong.md](meadow-mahjong.md), complete 2026-08-21).

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
| A7 | No caching | Measured 4.3 ms for a full 22-hand scan of a 13-tile rack (0.45 ms for the existing reachability pass). Far inside any interaction budget, including the 6–8 s claim window. A cache keyed on rack state would be more code and more staleness risk than it saves. |
| A8 | Default for a member who has never chosen: `gap`, overridable by a guild dial | Assistance-by-default matches the user's stated instinct ("everyone, always on"); the dial exists so a house can make pure play the norm without a code change. One select on the existing dashboard panel, enforced — not a dead toggle. |
| A9 | New table `mahjong_prefs`, PK `(guild_id, user_id)` | The house pattern is a small per-feature prefs table (`econ_notify_prefs` is three columns, same key shape); DK has no member-settings hub to hook into. Purged on erasure — a preference tied to a member id is personal data with no Art 17(3) ground to keep. |
| A10 | The member surface is one **Assistance** button on the existing `/mahjong` panel | `MemberPanelView` already carries Create Table / Card Viewer / My Stats. A personal play setting is member self-service, so it belongs in Discord (CLAUDE.md), while the *house default* of A8 is admin config and belongs on the dashboard. |

---

## Stage 1 — the engine

`closest_lines(concealed, exposures, card, seen_elsewhere, limit=3)` in
`match_logic.py`, returning per line: the `Hand`, the distance, the still-needed
tiles, and the held tiles the line does not consume. Pure; no Discord, no db.

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

Same commit: the `/mahjong` Assistance button and its select; `manual.html`
player-guide section; a new § in the mahjong spec.

**Tests**: embed builder per mode (including `off` rendering nothing); the
A6 safety rail — a suggested discard never names a tile completing a visible
exposure; cog wiring for the new button.

## Stage 4 — gate + QA

Full suite, eslint/stylelint if dashboard assets moved, scoped browser checks
for the dial. QA card written for a volunteer tester.

---

## What this addon does **not** do

- No table-level or host-level control (considered, rejected in favour of A1).
- No seat override of the stored default (considered; costs a precedence rule
  to test for little gain).
- No dashboard panel for the personal setting — that is member self-service.
