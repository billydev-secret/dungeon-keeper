# Meadow Mahjong — build plan

**Status: complete 2026-08-21** — stages 0–8 all landed on this branch, with
adversarial review rounds after stages 1, 3 and 6 (their findings and the
decisions they forced are D15–D18 below and the fix commits). Full gate green
(13,118 tests). What prod still needs: a restart (migration 175 + the cog),
`python scripts/register_tile_emoji.py` once for real tile art, and the
dashboard dials (enable + stakes; Duel wall trim ~60 recommended).

**Spec:** [../meadow_mahjong_spec.md](../meadow_mahjong_spec.md) (Design, v1.0 +
two amendments recorded at the top of that file).
**Branch:** `meadow-mahjong`, one worktree, **one merge at the end** — nothing
half-built reaches a live restart. Every stage below is a commit that references
this doc by stage number; the branch ships through `/dk-ship`, which gathers the
`Testing:` sections into a single QA card.

Card-driven American-style mahjong in Discord: 4-seat and 2-seat Duel, coins
escrow, an original versioned hand card (**Meadow Card — First Light**, 22
hands). Never NMJL content, layout, or selection — the mechanics are
uncopyrightable, the League's compilation is not.

---

## 0. Decisions this plan makes that the spec left open

The spec's §1 decisions are locked and are not revisited here. These are the
gaps it did not cover, resolved so the stages below are unambiguous.

| # | Decision | Why |
|---|---|---|
| D1 | Logic package at `src/bot_modules/games/mahjong/`, thin cog at `src/bot_modules/cogs/mahjong_cog.py` | Spec §5 nests `cog.py` in the package; the house layout puts cogs in `cogs/` (`survivor_cog.py` + `survivor/`). Package path follows the spec, cog placement follows the house. |
| D2 | Card JSON at `src/bot_modules/games/mahjong/cards/meadow_first_light.json`, **not** `data/cards/` | There is no `data/` tree, and the remote gate runner syncs only `src/`, `tests/`, `scripts/` — a card under `data/` would make every linter and matcher test unrunnable in CI. Read with `encoding="utf-8"`. |
| D3 | Tile PNGs generated into `assets/tile_emoji/` and committed; **id map** at `src/bot_modules/games/mahjong/tile_emoji.json` | The renderer and its tests must run on the gate runner, which does not sync `assets/`. Tests that need the PNGs themselves **skip** when absent. Map ships empty, so the text-chip fallback is what actually runs until registration. |
| D4 | Escrow rides `econ_game_wagers` via `economy_wager_service` (`game_type='mahjong'`, `game_id=table_id`), with one new shared primitive `settle_split()` | `hold_stake` / `refund_player` / `refund_game` already do seating, cancel and wall-game exactly-once. Mahjong is the first game whose settlement is not winner-takes-pot, so it needs a split settle rather than a private copy of the escrow machinery. |
| D5 | Five tables, not the spec's four — `mahjong_result_seats` splits per-seat payouts out of `mahjong_results` | Per-seat deltas in a JSON blob would be invisible to `purge_user_data` and to the access export (the documented list-column blind spot in `privacy_service`). A real column is the only way the erasure path can see it. |
| D6 | The public card viewer is **session-gated to any logged-in member**, not anonymous | "Public, read-only, for out-of-Discord study" (§8) is satisfied by the `manual.html` precedent — the closest thing the dashboard has to a member tier. An anonymous route would be a new security precedent and a new `PUBLIC_PATHS` entry; not worth it for card study. |
| D7 | Joining a table consults the no-contact list | Spec never mentions it, CLAUDE.md requires it: mahjong seats members together and the Charleston hands tiles between named players. Refusal is indistinguishable from an ordinary outcome (see `docs/no_contact_spec.md`). |
| D8 | One live table per channel, enforced by a unique index | Spec §6.3 asks for it; it is also what keeps the sticky table message safe — two sticky panels in one channel fight over the bottom slot nondeterministically. |
| D9 | Reachability lives in `match_logic.py` beside the exact matcher, not its own module | Both walk the same binding enumeration (x-bindings x suit maps x D-bindings). One enumerator, two predicates: exact-consume vs can-still-reach. |
| D10 | Last-man-standing generalizes the Duel fallow rule to 4-seat | The spec defines fallow-ends-the-hand only for Duel; a 4-seat table where three seats go fallow has nobody left to play against. When live seats reach one, the hand settles as a fallow end: the survivor collects their lowest-value live line's base payout from **each** fallow escrow, no multipliers. |
| D11 | Rematch is unanimous, on the settle screen's phase timer | A single-click rematch would let one player re-stake an AFK opponent's escrow hand after hand. Every seat must press Rematch within the phase window; anything less closes the table (escrow refunds are per-hand, so nothing is held). |
| D12 | Blind-pass resolution: pass-through with a random-rack cycle break | §2.3.4 says blind slots are "filled from their incoming tiles, unseen" but not how a full circle of blind passes resolves (everyone forwarding means no tile ever originates). Lanes resolve iteratively — a blind slot takes from the front of the sender's already-resolved incoming lane — and any remaining pure-blind cycle falls back to random non-joker rack tiles for those seats, the same fallback the AFK auto-resolve uses. A 13-tile rack always holds ≥5 non-jokers, so the fallback can never come up short. Lanes never carry jokers (pickers exclude them, and both fallbacks do too). |
| D13 | The turn panel gets a Mahjong button | §6.8's screen list omits it, but §2.7 wins by wall draw and by redemption both require declaring on your own turn. Silent-until-valid applies there exactly as in the claim window. |
| D15 | A win declared on a turn that began with a claim scores as a discard win | Found by the stage-3 adversarial review: calling your own winning tile as an exposure and then declaring Mahjong would re-score a discard win as self-pick (3× instead of 2× in Duel; everyone instead of discarder-weighted in 4-seat). The engine records how each turn began; any Mahjong declared on a claim-turn — redemptions in between included — is a discard win off that discarder. §2.6's "redemption that completes the hand is self-pick" applies to draw-turns. |
| D16 | A fallow seat's exposures stay redeemable | Spec-silent; ruled by physical-table intuition — exposed tiles are on the table, and a redemption is self-financed (you give the natural the joker impersonated). The fallow seat already pays; nobody gains anything they didn't trade for. |
| D17 | A claim-window tap does not reset the consecutive-timeout count | Found by the stage-3 review: a free Pass every window would let a player who times out every one of their own turns dodge folding forever, stringing the table along a 45s dead turn per cycle. Presence is proven by own-turn actions and simultaneous-phase submissions; window silence stays a normal pass, and window taps stay costless either way. |
| D18 | Ephemeral panels are summoned, not pushed | §6.4 says racks "arrive" — but Discord cannot send a member an ephemeral message without an interaction from them. The table card's **Open Rack** button is the always-available summons (and doubles as §6.4's Refresh fallback); every phase's private picker rides that panel. |
| D14 | The engine never shuffles and never sleeps | `deal`/`rematch` take an injected pre-shuffled wall, and the transitions that need randomness (auto-resolve picks, blind cycle break, auto-discard with no drawn tile) take an injected `rng`. The service passes `SystemRandom` and real walls; tests pass seeded ones. |

### Open, for the spec author to confirm at go-ahead

- **Stake set.** Proposed allowed coins-per-point `{1, 2, 5}`, default 1.
  Escrow at stake 1 is `card_max(75) x mult x stake` = **300 per seat (4-seat)**
  and **450 per seat (Duel)**. That is a big hold next to a ~36-coin average
  casino stake — though it is returned, and a typical Duel swing at stake 1 is
  only 60–90 coins. The hold, not the swing, is what blocks a seat.
- **Duel pacing.** §1 locks the full 152-tile wall with trim off by default, and
  the `hot_wall` amendment removed the other pacing lever. A default Duel is
  then ~125 live tiles between two players — roughly 60 draws each, an hour-plus
  hand at the 45s turn timer. Recommend setting **Duel wall trim to ~60** on the
  dashboard before the first live Duel; say the word and it ships as the default
  instead.

---

## Stage 0 — spec of record + this plan *(docs only, no `Testing:`)*

Copy the build spec to `docs/meadow_mahjong_spec.md` with its two amendments,
classify it **Design** in `docs/INDEX.md`, and land this plan doc.

---

## Stage 1 — tiles + card + linter

`tiles.py` — the 152-tile deck (§2.1), `Tile` as a comparable value type,
CSPRNG shuffle (`secrets.SystemRandom`), soap↔zero equivalence, flower
interchangeability, deal/wall counters.

`card_logic.py` — load and validate a card (§3.1/§3.2), generate each hand's
`display` string, expose sections in card order.

`scripts/validate_card.py` — the §3.4 linter as a CLI over the same function the
dashboard upload path will call. Hard-fails: groups not summing to 14; an `x`
offset that cannot land in 1–9; more than 4 naturals demanded of one tile across
`count <= 2` groups; flowers > 8; duplicate hand ids; value outside 25–75;
unreachable concealed+exposure combinations. Warns: near-duplicate lines,
sections with fewer than 2 hands.

`cards/meadow_first_light.json` — all 22 hands of §4, transcribed.

**Tests** `tests/test_mahjong_card_logic.py`: linter accepts First Light; every
hand sums to 14; one failure row per linter rule; deck composition; shuffle
returns a permutation; soap-as-zero.

---

## Stage 2 — the matcher (and reachability)

`match_logic.py` — `match_hand(concealed, exposures, card) -> list[Match]`, pure,
per §3.3. Shared binding enumerator over x-bindings (bounded by offsets), suit
letters → physical suits (≤ 6), and `D`. Exposures greedily map to groups first
(exact count, legal joker placement, exactly one group each), then the concealed
multiset is consumed with jokers substituting only into groups of 3+. Rejects a
concealed line with any exposure, a joker in a `count <= 2` group, and leftovers.
Returns **every** matching line; settlement takes the highest value, and
jokerless is computed from the actual tiles, not the line.

`reachable_lines(...)` — amendment 2. Same enumerator; a line is live when the
locked exposures map legally and, for each group, held + still-unseen + legal
joker cover ≥ count. Drives the Duel fallow payout (lowest-value live line, base
payout, no multipliers; card minimum when nothing is live).

**Tests** `tests/test_mahjong_match_logic.py` — table-driven, the heaviest suite
in the build: ≥1 positive per First Light line; x-binding bounds; suit-binding
conflicts (same letter = same suit, distinct letters = distinct suits); D
binding; soap-as-zero on a fixture line; joker-in-pair rejected; joker counting
for jokerless; concealed-with-exposure rejected; exposure→group mapping incl. a
mis-count; multi-line match settling at max value. Reachability: a line dying as
its fourth copy hits the discard pile, exposures killing concealed lines, joker
cover keeping a quint alive.

---

## Stage 3 — the state machine

`game_logic.py` — pure `(state, action) -> state`, no I/O, no sleeping. Seat
count (2|4) is a constructor parameter: pass routing, claim arbitration and
payout tables derive from it, and there is **no forked engine**.

`LOBBY → DEAL → CHARLESTON_1[r,a,l] → CHARLESTON_VOTE → CHARLESTON_2[l,a,r] →
COURTESY_PROPOSE → COURTESY_PICK → PLAY{AWAIT_DISCARD | CLAIM_WINDOW} → SETTLE →
(REMATCH → DEAL | CLOSED)`

Covers §2.3 Charleston (Duel: all three passes to the opponent; jokers excluded
from every picker; blind pass on final passes; unanimous vote with early no;
courtesy minimum-of-two), §2.4 turn loop with the 13-between-turns invariant,
§2.5 collect-then-arbitrate claims (Mahjong > exposure > nearest in turn order
from the discarder; pairs and singles uncallable except for Mahjong; concealed
hands claim only for Mahjong; discarded jokers never claimable; a discard dies
on the next discard; claimant continues their turn then play passes to their
right), §2.6 joker redemption, §2.7 win detection, §2.8 wall game, §2.9 payout
computation, and §6.2 AFK strikes → fallow. Illegal actions return a **typed
rejection**, never a crash and never a public message — that is what makes an
invalid Mahjong fail privately as a Pass (§1).

**Tests** `tests/test_mahjong_game_logic.py`, parameterized at seats ∈ {2, 4}
wherever a rule exists in both: scripted full hands to Mahjong at both seat
counts; Charleston routing and auto-resolve; claim priority and the private
rejections; redemption-as-self-pick; wall game; every payout permutation
including Duel 2x/3x/6x; fallow paying the winner in both modes and
fallow-ends-the-hand in Duel; serialization round-trip at every phase.

---

## Stage 4 — persistence, escrow, timers

Migration **`175_mahjong.sql`** (number re-checked against local `main` at
rebase — collisions have bitten before): `mahjong_cards`, `mahjong_tables`
(unique on live `(guild_id, channel_id)`, D8), `mahjong_results`,
`mahjong_result_seats`, `mahjong_stats`.

`mahjong_service.py` — table lifecycle, state serialize/reload, `asyncio` timers
in the service layer only (turn 45s, claim 8s/6s, phase 60s, 10-min inactivity
dissolve), re-armed on boot for live tables. Escrow through
`economy_wager_service`: `hold_stake` at seating (insufficient balance blocks the
seat with the house ❌ copy), new `settle_split()` at hand end (marks every held
row settled once, credits each seat `escrow + delta`, deltas must sum to zero,
`booster=False` throughout — a transfer between members never mints),
`refund_game` on cancel/dissolve/wall game.

Compliance in the same commit: `docs/data_register.md` rows for the four
per-user tables, all purged (game history has no Art 17(3) ground);
`purge_user_data` wired — dissolve the member's live seat and refund its escrow
before deleting rows. Member-id columns use conventional names already in
`privacy_service.SUBJECT_ID_COLUMNS` (`user_id`, `winner_id`), so no additions.

**Tests** `tests/test_mahjong_service.py`: escrow debit/settle/refund incl.
replayed terminal hooks; balance gate; one-table-per-channel; restart recovery
with a live table; purge dissolving a seat.

---

## Stage 5 — tile art

`scripts/make_tile_emoji.py` (Pillow) generates the 37-emoji set of §7.1 —
128x128 ivory faces, Dots blue / Bams green / Craks red, readable at 22px — so
the set is reproducible and reskinnable. `scripts/register_tile_emoji.py`
uploads them as **application-owned** emoji and writes the id map; it needs live
bot credentials, so it is a **one-command prod step**, not something this branch
can verify. The renderer always falls back to text chips (`2B`, `6D`, `JKR`,
`🌸`, `▢`) when an id fails to resolve — racks never render blank, and with the
map shipping empty that fallback is the launch state.

**Tests**: fallback rendering and id-map parsing run everywhere; anything that
needs the generated PNGs **skips** when `assets/tile_emoji/` is absent.

---

## Stage 6 — Discord surface

`cogs/mahjong_cog.py` (thin) + `views.py` + `embeds.py` (glue only, no rules).
The 13 screens of §6: `/mahjong` panel, create flow with the size picker and
escrow-labelled stake select, the public table card, deal, Charleston picker,
second-Charleston vote, courtesy, turn panel, claim window, exposure render,
joker redemption, Mahjong reveal, settlement. Duel deltas per §6.1.

The table message is a **sticky panel** on `core/sticky.py`; one table per
channel (D8) is what keeps it from fighting another panel for the bottom slot.
Every ephemeral panel carries whose turn it is, the clock, and the tile in
question, so nobody has to scroll. No-contact is consulted on join (D7).
Embeds follow the house style guide: `resolve_accent_color`, semantic
green/red for results only, Title Case, `🀄 Meadow Mahjong • …` footer.

**Tests**: the logic layer is already covered — the cog gets wiring assertions
only (command registration, no-contact gate on join, sticky placement).

---

## Stage 7 — dashboard + user-facing docs

Route id **`mahjong`** (frozen at birth), Games nav → Live Games. Panel:
card management (upload → server-side linter with inline errors → set active /
schedule / archive), the member-visible card viewer (D6), house rules (claim
window per mode, turn timer, phase timer, Duel wall trim; **no `hot_wall`**,
**no `strict_exposures`** — no unenforced toggles), stakes, and a tables report.
Shared escaped `table.js`; config mounts through `mountAsync`; any guild-scoped
cache cleared in `resetMetaCaches()`.

Same commit: `manual.html` player guide + the privacy line ("we store your
mahjong results and aggregates"), routed via `help-sections.js`. README gets a
line — a whole new feature area is exactly the case that earns one.

**Tests**: route authz (the sweep covers a new route automatically), snowflake
precision, linter-rejects-bad-upload, panel load health + responsive layout in
the browser suite.

---

## Stage 8 — green + QA

Full suite, `npx eslint src/web_server/static/js`, `npx stylelint`, scoped
browser checks. QA checklist written for a volunteer tester: Duel first with two
testers, then a 4-seat table (§11). The card ships playable with text chips; the
QA card names emoji registration as the prod step it is.
