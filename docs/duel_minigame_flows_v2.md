# Dungeon Keeper — Duel Minigame Flows v2

> **Status: historical design doc.** Chicken, Musical Chairs, and Hot Potato (group)
> shipped, but with materially different rules than sketched here — see
> `docs/dk_pvp_games_suite_spec.md` §9 for the as-built behavior. **Liar's Dice is the
> only game from this doc that remains unbuilt**, and **Minesweeper was never built**
> (in any version). Kept for the design intent, not as a description of the code.

**New this version:** Chicken · Liar's Dice · Musical Chairs · Hot Potato (group)

---

## Framework change: `BaseDuel` → `BaseGame`

v1 assumed exactly two players. Three of the four games below want **2..N**, so the
inheritance gets one more layer:

```
BaseGame  (2..N players)
   owns: roster, join/accept lifecycle, stakes, elimination tracking,
         loser→nickname stake + auto-revert, result embed, cooldowns,
         guardrails, "🫡 I'll honor this"
   │
   ├── BaseDuel(BaseGame, n=2)   ← v1 games unchanged; just a fixed-roster special case
   │       Pressure Cooker, Quickdraw (Minesweeper was planned in v1 but never built)
   │
   └── BaseGame(n=2..N)
           Chicken (2..N), Hot Potato (2..N), Musical Chairs (3..N),
           Liar's Dice (2..N)
```

**What moves up from `BaseDuel` into `BaseGame`:**

- **Roster** — ordered list of `player_ids` instead of `(p1, p2)`. Duels just cap it at 2.
- **Lobby/accept** — for N>2, the challenge embed becomes a **join lobby** with a
  `✋ Join` button + host `▶️ Start`, min/max player gates, and a join window
  (`lobby_timeout`, default 60s). Duels keep the v1 single-opponent accept flow.
- **Elimination model** — `BaseGame` tracks `alive[]` / `eliminated[]` and an
  `elimination_order`. The *loser* of a multi-player game is the last one standing's
  *opposite* — i.e. games declare losers progressively, and the **nickname stake applies
  to whoever the game marks as the final loser** (configurable: `stake_target` =
  `last_eliminated` | `first_eliminated` | `all_eliminated`).
- **Per-game hooks unchanged:** `render_game_state()`, `get_buttons()`,
  `handle_interaction()`, plus a new optional `on_eliminate(player_id)`.

**Stakes in N-player games.** The 24h-nickname mechanic is built for one loser. For
group games, default `stake_target = last_eliminated` (the final loser eats the stake);
the rest just get cosmetic standings. Custom free-text stakes still work and stay
cosmetic. This keeps the nickname-surrender rail meaningful without 6 people getting
renamed at once.

---

## Game 4 — Chicken

**Tension type:** mutual nerve / brinkmanship · **Length:** 10–40s · **Players:** 2..N

Everyone holds a button. A shared meter climbs while held. **First to release loses** —
but if the meter maxes out while people are still holding, **everyone still holding
loses together** (mutual destruction). The whole game is "who blinks first, and is the
glory worth the risk of the crash?"

### States

```
COUNTDOWN → CLIMBING → COMPLETE
```

### Flow

1. Lobby fills (2..N) → `▶️ Start` → 3-2-1 `COUNTDOWN`, then `CLIMBING`.
2. Each player presses & **holds** 🐔 **HOLD**. Releasing = letting go of the button
   interaction (see implementation note — Discord can't truly detect hold, so this is
   modeled as press-to-commit / press-again-to-bail).
3. A shared meter climbs from 0→100 over `climb_duration` (default 25s).
4. The **first** player to bail (release) is safe-ish but **loses the round** in the
   2-player case. In N-player, bailing **eliminates you from contention for glory** but
   saves you from the crash.
5. If the meter hits 100 with players still holding → **CRASH**: everyone still holding
   loses simultaneously. state = `COMPLETE`.

### Loser determination

- **2 players:** first to release loses. If both ride to 100 → both lose (double KO).
- **N players:** everyone holding at crash loses (`stake_target = all_eliminated`
  override recommended for Chicken specifically, since "everyone who didn't blink"
  is the dramatic point). The single bravest-but-not-dead player (last to bail *before*
  crash) is the moral winner; flavored on the embed.
- Edge case: everyone bails before crash → **last to bail wins**, earlier bailers ranked
  by bail order (no nickname penalty; cosmetic chicken-ranking).

### The "hold" modeling problem (important)

Discord buttons fire a single interaction — there's no real press-and-hold. Two clean
options:

- **Option A (recommended): commit/bail toggle.** First press = "I'm holding" (you're in
  and locked climbing). A second press = "BAIL." This is honest, simple, and server-
  authoritative. The tension is psychological — once you're in, you watch the meter and
  decide *when* to spend your one bail.
- **Option B: heartbeat.** Require re-pressing every N seconds to "stay holding"; miss a
  beat = auto-bail. More fiddly, more spammy, not worth it. Use A.

### Critical implementation notes

- Meter climb is a **server-side scheduled progression**; the embed is edited on an
  interval (e.g. every 2–3s) to show the bar. Don't trust client timing.
- Record `bail_ts` per player; bail order is authoritative for ranking.
- Per-game lock around bail/crash resolution so a bail landing in the same tick as the
  crash resolves deterministically (bail timestamp < crash timestamp → bailed in time).
- The crash is a scheduled task at `start + climb_duration`; on fire, read who's still
  holding.

### Config knobs

- `climb_duration` (default 25s)
- `min_players` / `max_players` (default 2 / 8)
- `stake_target` (default `all_eliminated` for Chicken)
- `show_meter` (default true — unlike hidden-fuse games, Chicken's meter is **visible**;
  the tension is watching it climb toward a known cliff)

### Embed

**CLIMBING:**
```
🐔 CHICKEN
Still holding: {p1}, {p3}, {p4}
Bailed: {p2} (at 38%)

⚡ METER: ████████░░░░░░░░ 52%
       ↑ crash at 100%. blink first or ride it out.

[🐔 HOLD / BAIL]
```

**COMPLETE (crash):**
```
💥 CRASH at 100%.
😵 Still holding when it blew: {losers}
🐔 {bravest_bailer} bailed last at {n}% — nerves of steel.
```

**COMPLETE (all bailed):**
```
🐔 Everyone blinked.
🏆 {winner} held longest ({n}%). {ranked list follows}
```

---

## Game 5 — Liar's Dice

**Tension type:** bluff / deduction · **Length:** 2–6 min · **Players:** 2..N

Each player has a cup of hidden dice. Players take turns raising a **bid** about how many
of a given face exist across *all* cups. You can raise the bid or **call "Liar."** Calling
reveals all dice: if the bid was true, the caller loses a die; if false, the bidder loses
a die. Lose all your dice and you're out. Last player with dice wins.

This is the most decision-rich game in the set and the only one with real depth.

### States

```
ROLLING → BIDDING → REVEAL → (ROLLING | COMPLETE)
```

### Flow

1. Lobby fills (2..N). Each player starts with **5 dice** (`starting_dice`).
2. **ROLLING:** all players roll privately. Each player sees **only their own** dice via
   an ephemeral message ("Your cup: ⚄⚂⚀⚅⚂").
3. **BIDDING:** active player either:
   - **Raises** the bid — must increase either the *quantity* or the *face value*
     (standard Liar's Dice raising rules; quantity-up always legal, face-up legal at
     same-or-higher quantity).
   - **Calls "Liar"** on the previous bid.
4. **REVEAL** (on a call): all cups open. Count actual dice matching the bid face
   (1s/aces wild is a config option, `aces_wild`, default true).
   - Bid was **met or exceeded** → the **caller** was wrong → caller loses one die.
   - Bid was **not met** → the **bidder** was bluffing → bidder loses one die.
5. Player who lost a die starts the next `ROLLING` round. A player at **0 dice is
   eliminated** (`on_eliminate`).
6. Repeat until one player has dice → `COMPLETE`, that player wins; the **last eliminated**
   eats the nickname stake.

### Loser determination

- Progressive elimination by losing all dice. Final loser for stake purposes =
  `last_eliminated` (the runner-up who busts out last), or configurable.

### UI design (the fiddly part)

- **Private dice:** each player presses 🎲 **My Cup** for an ephemeral re-display anytime
  (in case they lost the original ephemeral). Never post dice publicly until reveal.
- **Bidding controls** for the active player: a **Raise** button opens a modal/select for
  (quantity, face), and a **Call Liar** button. Non-active players see disabled controls
  + a live "current bid" line.
- **Current bid** always shown in the public embed: "Current bid: **four ⚄s** by {player}".
- Reveal posts every cup publicly with the tally.

### Critical implementation notes

- **All dice values server-side.** Ephemeral reveals are per-user; never leak another
  player's cup.
- Validate raises server-side against the standard ordering; reject illegal raises with
  an ephemeral.
- Turn order is a rotating pointer over `alive[]`; skip eliminated players.
- `aces_wild`: if true, 1s count as the bid face during tally (common variant).
- Per-game lock around reveal resolution.
- **Turn timeout** recommended here (`turn_timeout`, default 60s) since bidding can stall;
  on timeout, auto-call Liar or auto-minimum-raise (configurable, default auto-call).

### Config knobs

- `starting_dice` (default 5)
- `aces_wild` (default true)
- `min_players` / `max_players` (default 2 / 6)
- `turn_timeout` (default 60s)
- `stake_target` (default `last_eliminated`)

### Embed

**BIDDING:**
```
🎲 LIAR'S DICE
Players: {p1}(5) {p2}(4) {p3}(5)   ← dice counts
Total dice in play: 14

📣 Current bid: four ⚄s — by {p2}
🎯 {p3}'s turn.

[🎲 My Cup]  [⬆️ Raise]  [🗣️ Call Liar]
```

**REVEAL:**
```
🔍 {caller} called LIAR on "four ⚄s"!

Cups: {p1} ⚄⚄⚂⚀⚅ · {p2} ⚄⚂⚂⚀ · {p3} ⚀⚅⚄⚂⚄
Actual ⚄s (aces wild): 5

✅ Bid was true → {caller} loses a die. ({caller}: 5→4)
```

**COMPLETE:**
```
🎲 {winner} is the last liar standing.
💀 {last_out} busted out last — they take the stake.
```

---

## Game 6 — Musical Chairs

**Tension type:** reflex + attrition · **Length:** 1–3 min · **Players:** 3..N

Classic elimination. There are **N−1 chairs** for N players. "Music" plays for a hidden
duration; when it **stops**, everyone races to claim a chair. The player who doesn't get
one is **out**. Remove a chair, repeat, until two players fight over one chair. Last
seated wins.

Needs **3+ players** to be meaningful (2-player degenerates to a single reflex race —
just use Quickdraw).

### States

```
MUSIC → SCRAMBLE → ELIMINATE → (MUSIC | COMPLETE)
```

### Flow

1. Lobby fills (3..N). `chairs = players − 1`.
2. **MUSIC:** embed shows "🎵 the music is playing…" with a 🪑 **(disabled-looking, but
   live)** sit button. Hidden duration rolled **random 5–15s**.
3. Sitting **during MUSIC** = jumped the gun → **that player is eliminated this round**
   (false-start trap, same trick as Quickdraw). Optional via `false_start_elim`
   (default true).
4. **SCRAMBLE:** music stops, embed flips to "🪍 SIT!", the sit button goes hot. The
   **first `chairs` players** to press claim seats. Server records press order.
5. **ELIMINATE:** the player(s) who didn't claim a seat are out. Normally exactly one
   per round (N players, N−1 chairs). state cycles back to `MUSIC` with `chairs−1`.
6. When 2 players remain over 1 chair → final scramble → winner. `COMPLETE`.

### Loser determination

- Per round: the slowest / unseated player is eliminated (`on_eliminate`).
- Final loser for stake = `last_eliminated` (runner-up in the 2-player final).
- False-start during MUSIC eliminates that player for the round even if they'd have been
  fast enough.

### Critical implementation notes

- The sit button is **always live** (the false-start trap needs it clickable during
  MUSIC) — same pattern as Quickdraw's FIRE button. Check state on press.
- **Seat claiming is press-order, server-authoritative.** First `chairs` valid presses
  after SCRAMBLE get seats; record monotonic press timestamps.
- Per-game lock so the "did I get the last chair?" race resolves deterministically.
- A player can only press once per round; ignore repeats.
- **No-show handling:** if a player never presses during SCRAMBLE, they're simply not
  among the seated and get eliminated — no special timeout needed (the next MUSIC won't
  start until the round resolves, so cap SCRAMBLE with `scramble_window`, default 8s,
  after which unseated players are all eliminated).
- Edge: if multiple players are unseated in a malformed round (shouldn't happen with
  chairs = players−1), eliminate all unseated.

### Config knobs

- `min_music` / `max_music` (default 5s / 15s)
- `scramble_window` (default 8s)
- `false_start_elim` (default true)
- `min_players` (default 3) / `max_players` (default 10)
- `stake_target` (default `last_eliminated`)

### Embed

**MUSIC:**
```
🎵 MUSICAL CHAIRS — Round {r}
🪑 Chairs: {chairs}   👥 Still in: {n}

🎶 …the music is playing… don't sit yet…
(sit too early and you're out)

[🪑 SIT]
```

**SCRAMBLE:**
```
🪑 SIT!!! — grab a chair!
Chairs left: {chairs}

[🪑 SIT]
```

**ELIMINATE:**
```
❌ {loser} didn't find a chair. Out!
Remaining: {survivors}
```

**COMPLETE:**
```
🪑 {winner} takes the last chair.
🥈 {runner_up} was left standing — they take the stake.
```

---

## Game 3b — Hot Potato (group, 2..N)

Generalizes v1 Hot Potato from the 2-player special case to the full `BaseGame`. The
v1 duel version becomes just `n=2`.

**Tension type:** hidden fuse timing · **Length:** 30–90s · **Players:** 2..N

A bomb is passed around the circle on a hidden fuse. Whoever holds it when it blows is
**out**. Remove them, **re-light a new hidden fuse**, keep going until one player remains.

### States

```
TICKING → DETONATE → (TICKING | COMPLETE)
```

### Flow

1. Lobby fills (2..N). Bomb lands on a **random** player. Roll hidden fuse
   (random 20–60s).
2. Holder has 🤲 **Pass**. **Min-hold 2s** before Pass enables (anti-ping-pong),
   unchanged from v1.
3. Pass → bomb moves to the **next alive player** (configurable direction:
   `pass_mode` = `choose` | `clockwise`, default `choose` — holder picks the target via
   a user-select among alive players; clockwise is simpler/faster for big groups).
4. **DETONATE:** fuse expires → current holder is **eliminated** (`on_eliminate`).
5. If ≥2 players remain → re-roll a **fresh hidden fuse**, bomb lands on a random
   surviving player (or the player after the eliminated one in clockwise mode), back to
   `TICKING`.
6. One player left → `COMPLETE`, they win. **Last eliminated** takes the nickname stake.

### Loser determination

- Progressive: each detonation eliminates the holder. Final loser for stake =
  `last_eliminated`.

### Style points (carried from v1, now cross-round)

- Track **cumulative hold time** per player across the *entire game*, not just one round.
- "Bravest" = highest cumulative hold among survivors AND the dead; flavored on the final
  embed. Still purely cosmetic.

### Creeping-dread tell (carried from v1)

- Past ~70% of the current fuse, escalate the bomb emoji `💣` → `💣💥` → `💣💥💥` via
  embed edits. Re-applies each round with the new hidden fuse.

### Critical implementation notes

- **Fuse server-side & authoritative**, per round. Never leak the value.
- **Min-hold enforced server-side**; ignore Pass < 2s after receiving.
- **Pass target validity:** in `choose` mode, the user-select must list only **alive**
  players excluding the current holder; reject stale selections (target eliminated mid-
  pass). In `clockwise` mode, skip eliminated players when advancing.
- Detonation is a scheduled task per round; cancel/reschedule on elimination, NOT on pass
  (fuse keeps burning across passes — only resets when a new round starts).
- Per-game lock around pass + detonation resolution.
- If the holder goes unresponsive, fine — they're gambling. No extra timeout beyond fuse.

### Config knobs

- `min_fuse` / `max_fuse` (default 20s / 60s — re-rolled each round)
- `min_hold` (default 2s)
- `shake_threshold` (default 0.70)
- `pass_mode` (default `choose`; `clockwise` recommended for `players > 6`)
- `min_players` / `max_players` (default 2 / 10)
- `stake_target` (default `last_eliminated`)

### Embed

**TICKING (group):**
```
🥔💣 HOT POTATO
Still in: {p1}, {p3}, {p4}, {p5}
Out: {p2}

🤲 {holder} is holding the bomb…
Pass it before it blows.

[🤲 Pass]  ← {holder} only, enabled after 2s
   (choose mode: opens a select of alive players)
```

**Near detonation:**
```
🥔💣💥💥 …it's getting hot in {holder}'s hands…
```

**DETONATE (not final):**
```
💥 BOOM. {loser} is out. ({n} left)
🔁 New fuse lit. {next_holder} is holding now.
```

**COMPLETE:**
```
💥 Last blast — {last_out} is out.
🏆 {winner} survives the whole game.
🫡 Bravest hands: {bravest} held {n}s total across the game.
💀 {last_out} takes the stake.
```

---

## Updated cross-game summary

| Game | Tension type | Length | Decision | Players | Stake target |
|---|---|---|---|---|---|
| Pressure Cooker | escalating gauge | 60–90s | pump (forced) | 2 | loser |
| Quickdraw | reflex + nerve | ~10s | when to fire | 2 | loser |
| Minesweeper *(never built)* | climbing odds | 30–60s | which tile / forfeit | 2 | loser |
| Hot Potato (duel) | hidden fuse | 20–60s | when to pass | 2 | loser |
| **Chicken** | mutual nerve | 10–40s | when to bail | 2..N | all holding at crash |
| **Liar's Dice** | bluff / deduction | 2–6 min | raise or call | 2..N | last eliminated |
| **Musical Chairs** | reflex + attrition | 1–3 min | when to sit | 3..N | last eliminated |
| **Hot Potato (group)** | hidden fuse | 30–90s | when/who to pass | 2..N | last eliminated |

## Build order recommendation (v2)

1. **Generalize `BaseDuel` → `BaseGame`** first — add roster/lobby/elimination. Retrofit
   the v1 trio as `BaseDuel(n=2)` subclasses to prove nothing broke.
2. **Hot Potato (group)** — smallest delta from existing v1 code; validates the
   elimination + re-round loop on familiar mechanics.
3. **Musical Chairs** — reuses Quickdraw's false-start trap + the new elimination loop.
   Mostly assembled from parts you'll already have.
4. **Chicken** — introduces the visible shared meter + the commit/bail interaction model.
5. **Liar's Dice** — most complex (private state, bid validation, reveal). Do last when
   the framework is solid.

When the **reputation tracker** and **economy** layers land (parked), attach them to
`BaseGame` once. Elimination order gives them rich signal for free — placement-based
standing and payouts across any player count.
