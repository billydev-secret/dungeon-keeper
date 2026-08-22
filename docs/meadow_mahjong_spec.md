# Meadow Mahjong — Final Build Spec (v1.0)

> **Classification: Design.** Written to implement; the feature is being built
> against it in `docs/plans/meadow-mahjong.md`. Where this spec and the code
> disagree, the code wins (see `docs/INDEX.md`).

## Amendments since v1.0

Points resolved with the spec author after v1.0 was frozen; they override the
body below. 1–2 predate any code (2026-08-21); 3 records the assist addon.

1. **`hot_wall` is dropped from v1.** §1 and §8 list it as a dashboard lever but
   the spec never defines what it does, and "Duel wall trim" already owns the
   shorten-the-wall job. Shipping an undefined dial would break CLAUDE.md's
   "never ship a preference that isn't enforced" rule — the same call §1 already
   made on `strict_exposures`. Duel wall trim remains as the one pacing lever.
   `hot_wall` can return in a later version once it has a definition.

2. **"Lowest-value live line" (§1, Duel fallow payout) means *still
   reachable*.** A line is live for a player when it is still completable from
   where they stand: their locked exposures map legally onto its groups, and for
   every group the tiles they hold plus the copies still unseen plus legal joker
   cover meet the required count. That needs a partial-match / reachability pass
   alongside the exact-14 matcher of §3.3 — see the plan doc, stage 2. Known
   property of this definition: early in a hand almost every line is still
   reachable, so the fallow payout usually lands on the card's cheapest line; it
   rises as the discard pile kills off cheap lines and as exposures lock a player
   in.

3. **Assistance modes (added 2026-08-22, built as an addon).** Four per-player
   levels — `off`, `target` (closest lines + distance), `gap` (…+ the tiles
   still needed), `coach` (…+ dead weight and a suggested discard) — shown on
   every rack render while the seat has a tile decision. Each member picks
   their own from the `/mahjong` **My Settings** menu, stored per guild
   (`mahjong_prefs`, migration 176); a member who never chose gets the guild
   default (dashboard dial `mahjong_assist_default`, shipped default `gap`).
   Distance ranks; reachability only excludes (dead lines never show).
   Coach never suggests a joker and never suggests a tile that a line
   compatible with another seat's visible exposures still demands — silence
   over harm. Decisions, metric, and rails: `plans/mahjong-assist.md`.

4. **AI seats (added 2026-08-22, built as an addon).** House bots — per-table
   negative synthetic ids, flora names with a 🌱 prefix — fill tables in two
   modes: **practice** (one human + bots, stake-free, nothing recorded but the
   table row; `create_table(practice=True)`, dial `mahjong_practice_bots`,
   default on) and **fill** (host seats a house-staked bot on a short real
   table; the bot's escrow is ledgered house money swept back at settle; dial
   `mahjong_fill_bots`, default off until the brain proves itself). The brain
   is the assistance engine; decisions per phase live in `bot_logic.py`.
   Leavers still fold fallow — they never become bots. Decisions and stages:
   `plans/mahjong-bots.md`.

---

**Handoff target:** Claude Code, working in the Dungeon Keeper repo.
**Scope:** one build — engine + Discord cog + dashboard, both table sizes (4-seat classic, 2-seat Duel), shipping with the starter **Meadow Card — First Light** (22 hands, defined in §4).
**Companion artifact:** `meadow_mahjong_walkthrough.html` — 13-step rendered UX walkthrough. Screens in §7 reference its step numbers.

> **Claude Code: before writing any code**, pull the house rules from the DK MCP (`get_conventions`, and `search_code` for existing game cogs to mirror structure). Write the plan doc in `docs/plans/` first; stage commits against it. Tests land in the same commit as the logic they cover; `gate.py --scoped` must pass. Zero admin configuration in Discord — all config lives on the dashboard. Data-register rows, manual.html sections, and privacy-notice lines ship in the same commit that creates the tables.

---

## 1. What this is

Card-driven American-style mahjong played entirely in Discord. Players win by matching, tile-for-tile, one of the hands on the active **Meadow Card** — an original, seasonal, versioned-data hand set (never NMJL content, layout, or selection/arrangement; the mechanics are uncopyrightable, the League's card compilation is not — do not copy it). Coins economy stakes with escrowed flat payouts. First implementation of American mahjong on Discord anywhere.

**Locked decisions** (do not re-litigate in implementation):
- Two table sizes in v1: **4-seat** and **2-seat Duel**. Duel is the primary live-test vehicle.
- Session unit = **one hand** + Rematch button (dealer rotates/alternates).
- Fallow (AFK) seats fold dead but **still pay the winner**; in Duel a fallow seat ends the hand immediately (survivor collects their lowest-value live line's base payout from fallow escrow, no multipliers).
- Duel payouts: discard 2× / self-pick 3× / jokerless doubles either (max 6×).
- Full 152-tile wall in both modes; `hot_wall` and Duel wall-trim exist as dashboard levers, off by default.
- v1 exposures are **not** validated against live card lines (`strict_exposures` toggle ships only when enforcement ships).
- Invalid Mahjong declarations fail **privately** (convert to Pass); no dead-hand penalty, no table reveal.
- Tile rendering: **custom emoji** (§7.1). Text-chip fallback required.
- Out of scope v1: Siamese mode, spectator betting, AI seats, 3-player, tournaments, cross-server.

---

## 2. Rules of play (complete)

### 2.1 Tiles (152)
Dots/Bams/Craks 1–9 ×4 (108) · Winds N/E/W/S ×4 (16) · Dragons Red/Green/Soap ×4 (12) · Flowers ×8 · Jokers ×8. **Soap doubles as zero** where a card line calls for rank 0. Flowers are suitless and interchangeable with each other.

### 2.2 Deal & turn order
Dealer gets 14, others 13; dealer discards first after the Charleston. 4-seat: dealer rotates one seat per hand on Rematch. Duel: dealer alternates. Turn order = fixed seat order. Shuffle = CSPRNG; wall counter drives wall-game detection.

### 2.3 Charleston
1. **First Charleston (mandatory):** three passes of exactly 3 tiles — right, across, left. (Duel: all three passes go to the opponent; keep all three.)
2. **Second Charleston (optional):** left, across, right — plays only on a **unanimous** yes vote; resolve early on any no.
3. **Courtesy pass:** across-pairs (Duel: the two players) each propose 0–3; exchange the **minimum** of the two proposals; each side then picks which tiles to give.
4. **Blind pass:** on the final pass of each Charleston, a player may mark 0–3 outgoing slots as blind — filled from their incoming tiles, unseen.
5. **Jokers can never be passed** — exclude them from every picker.
6. Wrong counts are impossible by construction; no Charleston penalties exist.

### 2.4 Turn loop
Draw one (or claim the live discard) → any number of joker redemptions → discard one. Hand-size invariant (13 between turns) is engine-enforced. Turn timer default 45s; on expiry auto-discard the drawn tile. Three consecutive timeouts → fallow (§1).

### 2.5 Claims — the window of opportunity
- Each discard opens a claim window for all other seats: **Mahjong** or **Call** (complete an exposure of 3+) or **Pass**. Window: 8s default (4-seat), 6s (Duel); closes early when every eligible seat has responded.
- **Pairs and singles can never be called — except the tile that completes Mahjong.**
- **Collect-then-arbitrate**, never first-come-first-served: Mahjong > exposure > nearest in turn order from the discarder.
- Concealed (`C`) hands may claim only for Mahjong.
- Discarded jokers can never be claimed. A discard dies when the next discard lands.
- Winning exposure claim: claimant reveals the group face-up, then must discard (their turn continues from post-draw state). Play then passes to the claimant's right.
- Buttons stay enabled for everyone; illegal claims (pair-call, concealed-hand call, no matching group) resolve privately as Pass so a tap never leaks hand information.

### 2.6 Jokers
- Stand in **only** inside groups of 3+ (pungs/kongs/quints). Never pairs or singles. Any number of jokers per eligible group.
- **Redemption:** on your own turn, before your discard, swap a natural you hold for the joker it impersonates in **any** exposure (yours or an opponent's). Multiple redemptions per turn allowed. A redemption that completes the hand is Mahjong, scored as self-pick.

### 2.7 Winning
All 14 tiles must exactly match one line on the active card: groups, ranks (with rank-variable binding), suit-variable binding, joker legality, concealed/exposed status. Win by discard claim, wall draw, or redemption. Validation is silent-until-valid (§1).

### 2.8 Wall game
Wall exhausted, no winner → nobody pays, escrow returns, Rematch offered.

### 2.9 Scoring & escrow (coins)
Stake = coins per point, chosen at table creation from the dashboard-configured range.
- **4-seat:** discard win → discarder 2×, others 1×. Self-pick → all 2×. Jokerless (no jokers in the winning 14; Quiet Pairs lines are jokerless by definition and get no extra) doubles everything. Max 4×/loser. Escrow = card max × 4 × stake.
- **Duel:** discard win 2×, self-pick 3×, jokerless doubles either. Max 6×. Escrow = card max × 6 × stake.
Escrow debits at seating (insufficient balance blocks the seat with the house ❌ style), settles at hand end, refunds on cancel/dissolve/wall game.

---

## 3. Pattern grammar & matcher

### 3.1 Hand model
A hand is an ordered list of **groups**; each group = `{count, rank, suit}`.
- `count`: 1–6. Groups with `count >= 3` accept jokers; `count <= 2` never do.
- `rank`: `"1"`–`"9"` (concrete), `"x"`, `"x+1"`…`"x+4"` (rank variable; one `x` binding per hand, all offsets must land in 1–9), `"N" "E" "W" "S"`, `"R" "G" "soap"` (soap satisfies rank `"0"` where used), `"D"` (any single dragon, one binding per hand), `"F"` (flower; suitless).
- `suit`: `"a" | "b" | "c"` for suited ranks (same letter = same suit; distinct letters = pairwise distinct suits; binding chosen by the player's tiles), omitted for honors/flowers.
- Hand-level: `id`, `section`, `name`, `concealed: bool`, `value: int`, `display: str` (generated), `notes`.

### 3.2 Card file
```json
{
  "card_id": "meadow-first-light",
  "display_name": "Meadow Card — First Light",
  "season": "2026-autumn",
  "hands": [
    {"id":"mr-1","section":"Meadow Runs","name":"Meadow Run","concealed":false,"value":30,
     "groups":[{"count":2,"rank":"F"},{"count":3,"rank":"x","suit":"a"},
               {"count":4,"rank":"x+1","suit":"a"},{"count":3,"rank":"x+2","suit":"a"},
               {"count":2,"rank":"x+3","suit":"a"}]}
  ]
}
```

### 3.3 Matcher contract
`match_hand(concealed: list[Tile], exposures: list[Exposure], card: Card) -> list[Match]`
Pure function. For each card line: enumerate `x` bindings (bounded 1–9 minus offsets), `D` bindings, and suit-letter → physical-suit assignments (≤ 3! = 6); greedily assign exposures to groups first (an exposure must map to exactly one group, exact count, joker placement legal), then multiset-match remaining concealed tiles with joker substitution only into groups of 3+. Reject: concealed-line with any exposure; joker in a count ≤ 2 group; leftover tiles. Return every matching line (a 14-tile set can match multiple; settle uses the highest value, jokerless computed per the actual tiles). Brute force is fine: ≤ ~30 lines × ≤ 9 x-bindings × 6 suit maps × 3 dragon bindings is trivial.

### 3.4 Card linter (`scripts/validate_card.py`)
Fails on: group counts not summing to 14; rank offsets out of 1–9 for any x; more than 4 naturals demanded of one tile in `count <= 2` groups (jokerless-impossible pairs); flowers > 8; duplicate hand ids; value outside 25–75; unreachable concealed+exposure combinations. Warns on: near-duplicate lines; sections with < 2 hands.

---

## 4. Meadow Card — First Light (starter card, ships with v1)

22 original hands, 7 sections. `X` = exposures allowed, `C` = concealed. Group notation `count(rank)suit`.

| # | Section | Name | Groups | Val | X/C |
|---|---------|------|--------|-----|-----|
| gh-1 | Golden Hour | Golden Hour | 4(F) 4(2)a 4(6)b 2(8)c | 25 | X |
| gh-2 | Golden Hour | Dawn Chorus | 4(F) 3(x)a 3(x)b 4(x)c | 25 | X |
| gh-3 | Golden Hour | First Light | 6(F) 4(x)a 4(x)b | 30 | X |
| mr-1 | Meadow Runs | Meadow Run | 2(F) 3(x)a 4(x+1)a 3(x+2)a 2(x+3)a | 30 | X |
| mr-2 | Meadow Runs | Terrace Run | 4(x)a 4(x+1)b 4(x+2)c 2(N) | 35 | X |
| mr-3 | Meadow Runs | River Bend | 4(x)a 2(x+1)a 4(x+2)a 4(D) | 30 | X |
| eg-1 | Even Ground | Even Ground | 2(F) 4(2)a 3(4)a 3(6)a 2(8)a | 25 | X |
| eg-2 | Even Ground | Split Rail | 3(2)a 3(4)a 3(6)b 3(8)b 2(2)c | 30 | X |
| eg-3 | Even Ground | Skipping Stones | 4(F) 1(2)a 1(4)a 1(6)a 1(8)a 1(2)b 1(4)b 1(6)b 1(8)b 2(8)c | 40 | C |
| sb-1 | Switchbacks | Trailheads | 2(F) 3(1)a 3(3)a 3(5)a 3(7)a | 25 | X |
| sb-2 | Switchbacks | Scree | 4(1)a 2(3)a 4(5)b 2(7)b 2(9)c | 35 | X |
| sb-3 | Switchbacks | Ridgeline | 3(1)a 4(3)b 3(5)a 4(7)b | 30 | X |
| ws-1 | Windstorm | Four Winds | 4(N) 3(E) 3(W) 4(S) | 30 | X |
| ws-2 | Windstorm | High Lonesome | 2(F) 4(N) 4(S) 4(D) | 25 | X |
| ws-3 | Windstorm | Fire on the Mountain | 2(F) 4(R) 4(G) 4(soap) | 35 | X |
| ws-4 | Windstorm | Weathervane | 2(N) 2(E) 2(W) 2(S) 3(R) 3(G) | 40 | C |
| tt-1 | Tall Timber | Sequoia | 5(x)a 4(x+1)a 5(x+2)a | 45 | X |
| tt-2 | Tall Timber | Lodgepole | 5(N) 5(x)a 4(x)b | 40 | X |
| tt-3 | Tall Timber | Old Growth | 5(F) 5(x)a 4(x)b | 45 | X |
| qp-1 | Quiet Pairs | Quiet Pairs | 2(F) 2(1)a 2(3)a 2(5)a 2(7)a 2(9)a 2(N) | 50 | C |
| qp-2 | Quiet Pairs | The Long Trail | 2(F) 2(x)a 2(x+1)a 2(x+2)a 2(x+3)a 2(x+4)a 2(N) | 50 | C |
| qp-3 | Quiet Pairs | Echo | 2(1)a 2(2)a 2(3)a 2(1)b 2(2)b 2(3)b 2(R) | 75 | C |

Design notes: every group sums to 14 (linter-verified in tests); Tall Timber quints require jokers by construction (only 4 naturals exist — except Old Growth's flower quint, drawable natural from 8); Quiet Pairs lines are joker-free by rule and price it in; walkthrough hands (Meadow Run, Four Winds, Quiet Pairs, Golden Hour) match the published artifact exactly. Encode all 22 in `data/cards/meadow_first_light.json` following §3.2 — transcription is mechanical from this table; run the linter in tests.

---

## 5. Engine architecture

```
src/bot_modules/games/mahjong/
  tiles.py            # Tile enum, deck builder, CSPRNG shuffle
  card_logic.py       # card load, linter, display-string generation
  match_logic.py      # §3.3 matcher — the most-tested unit in the cog
  game_logic.py       # state machine, pure (state, action) -> state
  mahjong_service.py  # persistence, escrow/coins, table lifecycle, timers
  cog.py / views.py / embeds.py   # Discord glue only — no rules logic
```

**State machine:** `LOBBY → DEAL → CHARLESTON_1[r,a,l] → CHARLESTON_VOTE → CHARLESTON_2[l,a,r] → COURTESY_PROPOSE → COURTESY_PICK → PLAY{AWAIT_DISCARD | CLAIM_WINDOW} → SETTLE → (REMATCH → DEAL | CLOSED)`. Seat count (2|4) is a constructor parameter; pass routing, claim arbitration, and payout tables derive from it — no forked engine.

**Actions:** `create, join, cancel, charleston_pick(tiles, blind_n), vote(bool), courtesy_propose(n), courtesy_pick(tiles), discard(tile), claim(pass|call(group)|mahjong), redeem_joker(exposure_id, tile), timeout, rematch`. Every transition validates seat, phase, and legality; illegal → typed rejection (never a crash, never a public message).

**Serialization:** engine state is a dataclass serialized to `mahjong_tables` after every transition; on bot restart, live tables reload and re-arm their timers. Timers (`asyncio` tasks in the service layer) emit `timeout` actions; never sleep inside `game_logic`.

**Simultaneous phases** (Charleston picks, votes, proposals): collect per-seat sub-states, resolve when all seats responded or phase timer (60s) fires; missing seats auto-resolve (pass 3 random non-jokers / vote no / propose 0) and accrue an AFK strike.

---

## 6. Discord UX (screen-by-screen — walkthrough step in brackets)

1. **`/mahjong` panel** [1] — ephemeral: active card, stake range, member balance; buttons `Create Table · Card Viewer · My Stats`.
2. **Create flow** [2] — ephemeral: **size picker (Duel / Full Table)** → stake select showing per-option escrow → `Open Table`. Balance-gated with house ❌ copy.
3. **Table card** [3] — public, in-channel: mode, stake, seats with escrow checks, Join/Cancel buttons; footer counts down to deal when full. Cancel = host or mod only. One table per channel; one seat per member per guild.
4. **Deal** [4] — table card becomes the **persistent table message**; each player's rack arrives as an ephemeral panel (sorted tiles, `Refresh Rack` fallback for expired interactions).
5. **Charleston picker** [5] — ephemeral 3-tile multi-select (jokers excluded), `Blind Pass…` on final passes; table message ticks ✅ per seat.
6. **Second-Charleston vote** [6] — public buttons on the table message; early-resolve on any ❌.
7. **Courtesy** [7] — ephemeral 0–3 proposal buttons → public resolution embed → ephemeral give-picker for the minimum count.
8. **Turn panel** [8] — ephemeral on your turn: rack with drawn tile highlighted, discard select, `Redeem Joker` (enabled only when legal); public table message shows discard pit (latest highlighted), wall count, turn arrow, ⏱ countdown in the footer.
9. **Claim window** [9] — buttons `🀄 Mahjong · ✋ Call · Pass` on the table message; per-seat response ticks; resolution announced in the table render.
10. **Exposure render** [10] — exposures live inline on the owner's seat row of the table message, jokers visibly marked.
11. **Joker redemption** [11] — public "Joker Redeemed" embed (exposure now natural), then the redeemer's refreshed ephemeral panel (joker in rack, discard prompt).
12. **Mahjong** [12] — silent validation; on success a green reveal embed: line name, groups, how won; on failure a private ❌ and the window continues.
13. **Settlement** [13] — green results embed with a monospace payout table, multiplier notes, `Rematch · Close Table`.

**Ordering rule:** ephemeral responses always land at the bottom of the channel; because everything new bottom-anchors, the persistent table message uses the house **sticky panel** pattern (debounced delete+repost on channel activity, per-guild lock, new message id persisted before the DB save). Every ephemeral panel carries enough context to act without scrolling: whose turn, the clock, the tile in question.

Embeds follow the house embed style guide throughout: accent from `resolve_accent_color`, semantic green/red reserved for results, Title Case labels, footer game signature `🀄 Meadow Mahjong • …`, ❌ error style, playful-games voice.

### 6.1 Duel deltas
Create flow labels escrow at 6× max. Table render shows two seats; claim window shows one respondent and a 6s clock; all Charleston passes label the opponent by name ("Pass 3 tiles to Wren"); settlement table shows the 2×/3× line used.

### 6.2 AFK / recovery
Turn timeout auto-discards drawn tile (strike). Simultaneous-phase timeout auto-resolves (strike). 3 strikes → fallow per §1. Full-table inactivity 10 min → dissolve + refund. All timeouts survive bot restart via re-armed timers.

## 7. Tile emoji assets

### 7.1 Asset set (37 emoji)
`mm_1d…mm_9d`, `mm_1b…mm_9b`, `mm_1c…mm_9c` (27 suits), `mm_wn mm_we mm_ww mm_ws` (winds), `mm_dr mm_dg mm_soap` (dragons), `mm_flower`, `mm_joker`, `mm_back`. Source art: 128×128 PNG, ivory tile face, suit-colored glyphs (Dots blue, Bams green, Craks red), readable at 22px — generate programmatically (Pillow) in `scripts/make_tile_emoji.py` so the set is reproducible and reskinnable.

### 7.2 Registration
Prefer **application-owned emoji** (uploaded to the bot application, usable in any guild, no guild-slot cost) via the API/dev portal; store id map in `data/tile_emoji.json`. If application emoji are unavailable at build time, fall back to uploading to the home guild (~37 of the 45 budgeted slots). **Always** implement the text-chip fallback (`2B`, `6D`, `JKR`, `🌸`, `▢`) used automatically when an emoji id fails to resolve — racks must never render blank.

---

## 8. Dashboard (route id `mahjong`, frozen at birth; games nav heading)

- **Card management:** upload card JSON → server-side linter with inline errors → set active / schedule activation / archive. Card viewer (public, read-only page) renders the active card by section for out-of-Discord study.
- **House rules:** claim-window seconds per mode, turn-timer seconds, phase-timer seconds, `hot_wall`, Duel wall trim (0 = off), second-Charleston availability. (`strict_exposures` is **not** shipped in v1 — no unenforced toggles.)
- **Stakes:** allowed coins-per-point set, per-mode escrow preview.
- **Tables report:** live tables, recent results, per-player aggregates. Shared escaped `table.js`; config mounts via `mountAsync`.

## 9. Data model & compliance

| Table | Contents | Per-user | Purge |
|---|---|---|---|
| `mahjong_cards` | card JSON, active flag, schedule | no | — |
| `mahjong_tables` | serialized engine state, mode, sticky message id | yes (seats) | dissolve seat, refund escrow |
| `mahjong_results` | mode, winner, line id, payout, flags | yes | purged |
| `mahjong_stats` | per-member aggregates | yes | purged |

Same commit: data-register rows with the purge decisions above (no preservation ground applies to game history), conventional member-id column names, manual.html player guide + privacy line ("we store your mahjong results and aggregates"), `purge_user_data` wired.

## 10. Testing (logic layer, same-commit, ~80% patch coverage)

Parameterize the full suite at seats ∈ {2, 4} wherever a rule exists in both modes.
- **Card:** linter accepts First Light; every hand sums to 14; linter failure cases for each §3.4 rule.
- **Matcher:** table-driven rows — every First Light line has ≥1 positive case; x-binding bounds; suit-binding conflicts; D-binding; soap-as-zero (add one soap-rank test line in fixtures); joker-in-pair rejection; joker counting for jokerless; concealed-with-exposure rejection; exposure-to-group mapping; multi-line matches settle at max value.
- **Charleston:** pass routing both seat counts (Duel: all → opponent); blind pass; unanimous vote with early no; courtesy minimum; joker exclusion; auto-resolve on phase timeout.
- **Claims:** priority (Mahjong > call > nearest-in-turn); single-respondent Duel path; pair-call rejected privately; concealed gating; discard death; timeout close; claimant-continues-turn.
- **Jokers:** redemption legality (own turn, pre-discard, any exposure), redemption-as-self-pick win.
- **Scoring:** every payout permutation both modes incl. Duel 2×/3×/6×; escrow debit/settle/refund; wall game; fallow payment (both modes, incl. Duel fallow-ends-hand).
- **Serialization:** round-trip state at every phase; restart-with-live-table recovery.
- **Guards:** stake set, balance gate, one-seat rule, host-or-mod cancel — every guard has a failing case.

## 11. Build order & exit criteria

1. **Engine.** tiles → card_logic + linter + First Light JSON → match_logic → game_logic → full scripted hands green at both seat counts (Charleston, arbitration, redemption, payouts).
2. **Assets.** emoji generator + registration + id map + fallback renderer.
3. **Discord.** cog/views/embeds per §6, sticky table message, timers, escrow wiring.
4. **Dashboard + compliance.** §8 panel, data register, manual.html, purge wiring.
5. **Live QA.** Duel first (two testers), then a 4-seat table; QA checklist in the commit's Testing: section.

Ship order within a PR chain is Claude Code's call; every stage lands with its tests and references the plan doc.
