# Dungeon Keeper — Games Suite Functional Spec

> Authoritative long-term reference for the duel/group game system on The Golden Meadow (TGM).
> Module root: `dk/cogs/games/`
> Stack: Python · discord.py · aiosqlite
> Status: design spec — hand sections to Claude Code for implementation.

---

## Table of contents

1. Overview & design philosophy
2. Architecture: `BaseGame` / `BaseGame`
3. Shared lifecycle (challenge → stakes → resolution → revert)
4. The nickname stake mechanic
5. Stakes, guardrails & safety
6. Database schema
7. Scheduled tasks
8. Command surface
9. Per-game specs
   - 9.1 Pressure Cooker
   - 9.2 Quickdraw
   - 9.3 Minesweeper Duel
   - 9.4 Hot Potato
   - 9.5 Chicken
   - 9.6 Liar's Dice
   - 9.7 Musical Chairs
10. Cross-game summary table
11. Config reference
12. Build order
13. Parked / future work

---

## 1. Overview & design philosophy

A suite of interactive, button-driven games hosted by Dungeon Keeper for TGM. Every game
resolves to a **loser**, who surrenders nickname control for 24 hours (auto-reverting),
optionally under a custom cosmetic stake. The games range from pure-reflex (Quickdraw) to
deep bluffing (Liar's Dice), but they all share one spine so new games are cheap to add.

**Design principles**

- **One spine, many middles.** The shared lifecycle (challenge, stakes, resolution,
  nickname application, auto-revert, cooldowns, guardrails, result embed) is written once.
  Each game implements only its own "middle."
- **Server-authoritative.** All hidden state (timers, mine positions, dice, fuses) lives
  server-side. The client never learns anything that would let a player cheat.
- **Consent-gated stakes.** No one's nickname changes without them accepting the challenge.
  Custom stakes are cosmetic and honor-based.
- **Hearth > Highlight.** Games are playful and teasing, not humiliating. Guardrails on
  nickname content; cooldowns prevent griefing wars.
- **Replay is half the value.** Every result embed has a rematch / run-again path.

---

## 2. Architecture: `BaseGame` / `BaseDuel`

```
BaseGame  (2..N players)
   owns: roster, join/accept lifecycle, stakes, elimination tracking,
         loser → nickname stake + auto-revert, result embed, cooldowns,
         permission checks, guardrails, "🫡 I'll honor this", stats
   │
   ├── BaseDuel(BaseGame, n=2)        ← fixed 2-player special case
   │       Pressure Cooker · Quickdraw · Minesweeper · Hot Potato (duel)
   │
   └── BaseGame(n=2..N)               ← multiplayer
           Chicken · Hot Potato (group) · Musical Chairs · Liar's Dice
```

### Hooks each game implements

| Hook | Purpose |
|---|---|
| `render_game_state()` | Build the current game embed. |
| `get_buttons()` | Return the interactive components for the current state. |
| `handle_interaction(interaction)` | Process a press; mutate state; decide if the game is over and who lost. |
| `on_eliminate(player_id)` *(optional)* | Multiplayer only — react to a player being knocked out. |
| `setup_game()` *(optional)* | Roll initial hidden state (mine position, fuse, dice). |

### What `BaseGame` owns (games never reimplement these)

- **Roster** — ordered `player_ids` list. `BaseDuel` caps at 2.
- **Lobby / accept**
  - *Duel:* `/<game> challenge @user` → target gets an accept/decline embed → on accept,
    the game window spawns pinging both players.
  - *Multiplayer:* challenge spawns a **join lobby** (`✋ Join` + host `▶️ Start`), with
    `min_players`/`max_players` gates and a `lobby_timeout` (default 60s).
- **Elimination tracking** — `alive[]`, `eliminated[]`, `elimination_order[]`.
- **Stake resolution** — applies the nickname stake to the game's declared loser per
  `stake_target` (see §4).
- **Result embed** — winner/loser, stake summary, the cosmetic "🫡 I'll honor this"
  button, and a rematch/run-again control.
- **Cooldowns, permission checks, guardrails, audit logging, stats.**

---

## 3. Shared lifecycle

```
        ┌─────────────┐
        │  CHALLENGE  │  /<game> challenge @user [stakes]   (duel)
        │  / LOBBY    │  /<game> start [stakes]             (multiplayer)
        └──────┬──────┘
               │ accept / join + start
        ┌──────▼──────┐
        │  [MINIGAME] │  ← the only per-game part
        └──────┬──────┘
               │ loser(s) determined
        ┌──────▼──────┐
        │   RESOLVE   │  apply nickname stake to loser(s)
        └──────┬──────┘
               │ +24h (scheduled task)
        ┌──────▼──────┐
        │ AUTO-REVERT │  restore original nickname
        └─────────────┘
```

**Challenge (duel):** `/<game> challenge @user [stakes]` posts an embed pinging the target
with `✅ Accept` / `❌ Decline`. The target **must accept** before anything locks in.
Challenge expires after `challenge_timeout` (default 120s).

**Lobby (multiplayer):** `/<game> start [stakes]` posts a join lobby. Players press
`✋ Join`; host presses `▶️ Start` once `min_players` is met (or auto-start at
`lobby_timeout`).

**Resolve:** game declares loser(s); `BaseGame` applies the stake.

**Auto-revert:** a scheduled task restores the original nickname at +24h. Survives bot
restarts (rehydrated from DB on startup).

---

## 4. The nickname stake mechanic

The signature mechanic: the loser's nickname is replaced by the winner's choice for 24h.

- On loss, the **winner** is prompted (modal) for the new nickname. If they decline /
  time out, default to a configured template (e.g. `"🫡 {game} loser"`).
- DK records the loser's **original nickname** before changing it.
- A scheduled **auto-revert** task fires at +24h. Always logged; retries with exponential
  backoff on Discord API error; alerts the admin channel after repeated failure. **No one
  stays renamed past their revert window** — this is a hard guarantee.

### `stake_target` (multiplayer)

The 24h-nickname mechanic targets one loser. For N-player games:

| Value | Meaning | Default for |
|---|---|---|
| `loser` | The single loser. | All duels |
| `last_eliminated` | Final player knocked out (the runner-up bust). | Liar's Dice, Musical Chairs, Hot Potato (group) |
| `first_eliminated` | First player out. | — |
| `all_eliminated` | Everyone who lost (use sparingly). | Chicken |

Non-targeted players in multiplayer games get **cosmetic standings only** (placement,
flavor), never a forced rename.

---

## 5. Stakes, guardrails & safety

**Stake types**

1. **Mechanical (default):** 24h nickname surrender, bot-enforced.
2. **Custom free-text (optional):** ≤200 chars, cosmetic/honor-based. Shown on the result
   embed with the "🫡 I'll honor this" accountability button (gates nothing).
   No stakes specified → defaults to the mechanical 24h nickname surrender.

**Nickname guardrails** (applied to the winner's chosen name)

- Length cap (Discord max 32 chars; enforce a tighter cap if desired).
- Deny list: no slurs, no impersonating mods/admins, no `@everyone`/`@here` trickery,
  no zero-width / markup exploits.
- Config-level server deny list.

**Anti-grief**

- **Per-pair cooldown** (duels): default 48–72h so the same two people can't lock each
  other in a perpetual nickname war (opt-out if that's the vibe).
- **Per-user concurrency cap:** a player can be in at most one active game at a time.
- **Audit log:** every rename (who changed whom, to what, when, which game) is logged for
  a paper trail.

**Bot permission requirements**

- `Manage Nicknames`.
- DK's top role **higher** than the loser's top role (else the rename silently fails —
  detect and surface this at challenge time, not after the game).

---

## 6. Database schema

```sql
-- One row per active or recently-completed game.
CREATE TABLE games (
    game_id       TEXT PRIMARY KEY,         -- uuid
    game_type     TEXT NOT NULL,            -- 'pressure_cooker' | 'quickdraw' | ...
    guild_id      TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    message_id    TEXT,                      -- the game embed message (for restart re-attach)
    host_id       TEXT NOT NULL,
    state         TEXT NOT NULL,            -- per-game state machine value
    players       TEXT NOT NULL,            -- JSON array of player ids (ordered roster)
    alive         TEXT,                     -- JSON array (multiplayer)
    elimination_order TEXT,                 -- JSON array (multiplayer)
    payload       TEXT,                     -- JSON: per-game hidden + visible state
    stakes_text   TEXT,                     -- custom stake (nullable)
    stake_target  TEXT DEFAULT 'loser',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

-- Nickname stakes pending auto-revert.
CREATE TABLE nickname_stakes (
    stake_id      TEXT PRIMARY KEY,
    game_id       TEXT NOT NULL,
    guild_id      TEXT NOT NULL,
    loser_id      TEXT NOT NULL,
    winner_id     TEXT NOT NULL,
    original_nick TEXT,                      -- null = had no nickname (reset to none)
    applied_nick  TEXT NOT NULL,
    revert_at     INTEGER NOT NULL,          -- epoch; scheduled auto-revert
    reverted      INTEGER DEFAULT 0,
    created_at    INTEGER NOT NULL
);

-- Per-pair / per-user cooldowns.
CREATE TABLE game_cooldowns (
    guild_id      TEXT NOT NULL,
    game_type     TEXT NOT NULL,
    pair_key      TEXT NOT NULL,             -- sorted "id1:id2" for duels, user id for solo limits
    available_at  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, game_type, pair_key)
);

-- Lifetime stats per player per game.
CREATE TABLE game_stats (
    guild_id      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    game_type     TEXT NOT NULL,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    games_played  INTEGER DEFAULT 0,
    extra         TEXT,                      -- JSON: per-game flavor stats (longest hold, etc.)
    PRIMARY KEY (guild_id, user_id, game_type)
);

-- Audit trail of every rename.
CREATE TABLE game_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       TEXT,
    guild_id      TEXT NOT NULL,
    actor_id      TEXT,                      -- winner who chose the name
    target_id     TEXT NOT NULL,             -- loser renamed
    old_nick      TEXT,
    new_nick      TEXT,
    action        TEXT NOT NULL,             -- 'apply' | 'revert' | 'revert_failed'
    ts            INTEGER NOT NULL
);
```

`payload` holds per-game state as JSON (gauge value, mine index, fuse start/length, dice
cups, meter %, etc.) so a game fully rehydrates after a restart.

---

## 7. Scheduled tasks

- **Nickname auto-revert** — at startup and on a recurring tick, scan `nickname_stakes`
  for `revert_at <= now AND reverted = 0`; restore `original_nick`; mark reverted; log.
  Retry with exponential backoff; alert admins after 3 failures.
- **Game timers** — hidden fuses (Hot Potato), draw delays (Quickdraw), meter climb
  (Chicken), music duration (Musical Chairs), turn timeouts (Liar's Dice) are
  server-side scheduled tasks tied to `game_id`; cancelled/rescheduled on relevant
  interactions.
- **Stale game reaper** — games stuck past a max lifetime (`game_max_age`, default 30 min)
  are auto-closed and cleaned up.
- **Restart recovery** — on startup, load active `games`; re-fetch each message and
  re-attach view listeners; clean up rows whose message is gone.

---

## 8. Command surface

Every game shares this command shape (`<game>` = the game's slug):

| Command | Who | Effect |
|---|---|---|
| `/<game> challenge @user [stakes]` | Anyone | (Duel) Challenge a target. |
| `/<game> start [stakes]` | Anyone | (Multiplayer) Open a join lobby. |
| `/<game> cancel` | Challenger/host | Cancel a pending challenge/lobby. |
| `/<game> stats [@user]` | Anyone | W/L and per-game flavor stats. |

Shared in-embed controls: `✅ Accept` / `❌ Decline` (duel), `✋ Join` / `▶️ Start`
(lobby), `❓ How to Play` (everyone, ephemeral rules), `🔁 Run Again` (on result),
`🫡 I'll honor this` (cosmetic, on custom-stake results).

---

## 9. Per-game specs

> Each game below specs only its "middle": states, flow, loser determination, the
> server-authoritative bits, config knobs, and embeds. Everything in §3–§7 is inherited.

### 9.1 Pressure Cooker

**Tension:** escalating gauge · **~60–90s** · **2 players** · `stake_target: loser`

Two players alternate clicking **PUMP** on a shared gauge that rises by a random **1–15**
per press. When it reaches/exceeds **100**, **whoever pumped last loses.** The dread of
pumping at 87 is the whole game.

**States:** `PLAYING → COMPLETE`

**Flow**
1. Both in → gauge = 0, random first player, state = `PLAYING`.
2. Active player presses **PUMP** → gauge += random(1,15); turn passes.
3. Gauge ≥ 100 → last presser loses → `COMPLETE`.

**Loser determination:** the player whose pump took the gauge to/over 100.

**Server-authoritative:** the RNG increment is rolled server-side per press. Per-game lock
prevents a double-press race. Turn ownership enforced (ignore the off-turn player).

**Config:** `max_pressure` (100), `min_increment`/`max_increment` (1/15).

**Embed (PLAYING):**
```
🔥 PRESSURE COOKER
{p1} vs {p2}

GAUGE: ██████████████░░░░░ 78 / 100
🎯 {active}'s turn. Pump… if you dare.

[💨 PUMP]
```
**Embed (COMPLETE):**
```
💥 BLEW AT {final}! {loser} pumped last.
🏆 {winner} keeps their cool — and renames {loser} for 24h.
```

---

### 9.2 Quickdraw

**Tension:** reflex + nerve · **~10s** · **2 players** · `stake_target: loser`

Wait for the hidden "DRAW!" then slap **FIRE**. Fastest wins. Firing early = instant loss.

**States:** `WAITING → DRAW → COMPLETE`

**Flow**
1. Both in → `WAITING`; FIRE button mounted and **live**. Roll hidden delay random 3–8s.
2. Press during `WAITING` = **false start** → presser loses → `COMPLETE`.
3. Delay elapses → `DRAW`; record `draw_at`.
4. First valid FIRE after `draw_at` wins; other loses → `COMPLETE`.

**Loser determination:** false-starter (priority), else slower valid draw. Neither fires
within `draw_window` (5s) → void, no penalty (`void_on_double_noshow`, default true).

**Server-authoritative:** delay timer hidden & authoritative; FIRE always clickable (the
trap needs it); reaction = `press_ts − draw_at`; lock against double-resolution.

**Config:** `min_delay`/`max_delay` (3/8s), `draw_window` (5s), `void_on_double_noshow`.

**Embeds:**
```
🤠 QUICKDRAW — Hands on your holsters… fire early and you lose.   [🔫 FIRE]
🔫 DRAW!!!  ← fire NOW                                            [🔫 FIRE]
💨 {winner} drew in {ms}ms. 🐌 {loser} was {delta}ms too slow.
😬 {loser} jumped the gun. {winner} wins by default.
```

---

### 9.3 Minesweeper Duel

**Tension:** climbing odds · **30–60s** · **2 players** · `stake_target: loser`

A 4×4 grid, one hidden mine. Alternate clicking tiles. Hit the mine, you lose. No
adjacency hints — only "tiles remaining," so the odds visibly climb each safe pick.

**States:** `PLAYING → COMPLETE`

**Flow**
1. Both in → place one mine at random 0–15; coin-flip first player; `PLAYING`.
2. Active clicks an unrevealed tile: safe → ✅, turn passes; mine → 💥, clicker loses.
3. Active may instead press 🏳️ **Forfeit** to concede (counts as a loss).
4. Grid can't be cleared — with 15 revealed, the last tile is the mine.

**Loser determination:** mine-revealer, or forfeiter.

**Climbing-odds display:** show `1 / tiles_remaining` for the player about to move. Each
safe pick worsens the *next* pick, which you hand to your opponent — or eat yourself.

**Server-authoritative:** mine position hidden, rolled once; turn ownership enforced;
lock against double-click race. 16 buttons across 4 rows (Discord cap).

**Config:** `grid_size` (4), `mine_count` (1), `turn_timeout` (off), `show_odds` (true).

**Embeds:**
```
💣 MINESWEEPER DUEL — one mine, no hints.
🎯 {active}'s turn — odds this pick: 1 in {N}.  Tiles left: {N}
[4×4 grid: ✅ revealed, ⬜ hidden]   [🏳️ Forfeit]
💥 {loser} hit the mine ({k} safe picks in). 🧨 {winner} survives.
```

---

### 9.4 Hot Potato

**Tension:** hidden fuse timing · **2 players: 20–60s / group: 30–90s** · **2..N**
· `stake_target: loser` (duel) / `last_eliminated` (group)

A bomb passes on a hidden fuse; holder at detonation is out. Group version re-lights a
fresh fuse and continues until one remains.

**States:** `TICKING → DETONATE → (TICKING | COMPLETE)`

**Flow**
1. Bomb lands on a random player; roll hidden fuse random 20–60s.
2. Holder has 🤲 **Pass**, enabled after a **2s min-hold** (anti-ping-pong).
3. Pass → bomb moves to next player (`pass_mode`: `choose` via select | `clockwise`;
   clockwise recommended for >6). Fuse keeps burning; min-hold resets.
4. Fuse expires → holder eliminated. ≥2 left → re-roll fresh fuse, continue; else
   `COMPLETE`.

**Loser determination:** holder at fuse-zero (each round). Group final loser =
`last_eliminated`.

**Style points:** track **cumulative hold time** per player (across the whole game in
group mode); "bravest" flavored on the result, cosmetic only.

**Creeping-dread tell:** past ~70% of the current fuse, escalate `💣 → 💣💥 → 💣💥💥`
via embed edits (since the fuse is hidden).

**Server-authoritative:** fuse hidden & authoritative; min-hold server-enforced; pass
locked to holder; in `choose` mode the select lists only alive players (reject stale
targets); detonation is a scheduled task — cancel/reschedule on elimination, **not** on
pass.

**Config:** `min_fuse`/`max_fuse` (20/60s, re-rolled each round), `min_hold` (2s),
`shake_threshold` (0.70), `pass_mode` (`choose`), `min_players`/`max_players` (2/10).

**Embeds:**
```
🥔💣 HOT POTATO — the fuse is lit, nobody knows how long.
🤲 {holder} is holding…  [🤲 Pass]  (≥2s; choose mode opens a player select)
🥔💣💥💥 …it's getting hot…
💥 BOOM. {loser} was holding. 🔁 New fuse — {next} holds now.  (group)
🏆 {winner} survives. 🫡 Bravest hands: {bravest} held {n}s total.
```

---

### 9.5 Chicken

**Tension:** mutual nerve / brinkmanship · **10–40s** · **2..N**
· `stake_target: all_eliminated`

Everyone commits to **HOLD**. A **visible** shared meter climbs. First to **bail** loses
(duel) / drops out of glory contention (group). If the meter hits 100 with players still
holding → **CRASH**: all still holding lose together.

**States:** `COUNTDOWN → CLIMBING → COMPLETE`

**Flow**
1. Lobby (2..N) → `▶️ Start` → 3-2-1 → `CLIMBING`.
2. Meter climbs 0→100 over `climb_duration` (25s).
3. **Hold modeling = commit/bail toggle** (Discord can't detect true press-and-hold):
   first press = committed (locked in, climbing); second press = **BAIL** (safe, out of
   contention). Record `bail_ts`.
4. Crash at 100 → everyone still holding loses.

**Loser determination:** 2P → first to bail loses; both ride to 100 → double KO.
NP → all holding at crash lose; if everyone bails first, last-to-bail wins, others ranked
by bail order (no penalty).

**Server-authoritative:** meter climb is a server-side scheduled progression (embed edited
every 2–3s); bail order authoritative; lock so a bail in the crash tick resolves
deterministically (bail_ts < crash_ts = safe). Crash is a scheduled task at
`start + climb_duration`.

**Config:** `climb_duration` (25s), `min_players`/`max_players` (2/8), `show_meter` (true).

**Embeds:**
```
🐔 CHICKEN — crash at 100%. blink first or ride it out.
⚡ METER: ████████░░░░░░░ 52%   Holding: {…}  Bailed: {…}
[🐔 HOLD / BAIL]
💥 CRASH! Still holding when it blew: {losers}. 🐔 {bravest} bailed last at {n}%.
```

---

### 9.6 Liar's Dice

**Tension:** bluff / deduction · **2–6 min** · **2..N** · `stake_target: last_eliminated`

Each player has a cup of hidden dice. Bid up how many of a face exist across *all* cups,
or **call Liar**. On a call, all cups reveal; the wrong party loses a die. Lose all dice
= out. Last player with dice wins.

**States:** `ROLLING → BIDDING → REVEAL → (ROLLING | COMPLETE)`

**Flow**
1. Lobby (2..N). Each starts with **5 dice** (`starting_dice`).
2. **ROLLING:** all roll privately; each sees only their own cup via ephemeral.
3. **BIDDING:** active player **raises** (quantity-up always legal; face-up legal at
   same-or-higher quantity) or **calls Liar**.
4. **REVEAL:** count actual matching dice (`aces_wild`, default true). Bid met → caller
   loses a die; bid not met → bidder loses a die.
5. Die-loser starts next round; 0 dice = eliminated. Repeat → `COMPLETE`.

**Loser determination:** progressive elimination; stake to `last_eliminated`.

**UI:** 🎲 **My Cup** re-shows dice ephemerally anytime; never post a cup publicly until
reveal. Active player gets ⬆️ **Raise** (modal/select for quantity+face) and 🗣️ **Call
Liar**; others see disabled controls + the live current bid.

**Server-authoritative:** all dice server-side; per-user ephemeral reveals never leak;
raises validated against standard ordering; rotating turn pointer skips eliminated;
`turn_timeout` (60s) auto-calls Liar on stall; lock around reveal.

**Config:** `starting_dice` (5), `aces_wild` (true), `min_players`/`max_players` (2/6),
`turn_timeout` (60s).

**Embeds:**
```
🎲 LIAR'S DICE — players & dice: {p1}(5) {p2}(4) {p3}(5). Total: 14.
📣 Current bid: four ⚄s by {p2}.  🎯 {p3}'s turn.
[🎲 My Cup] [⬆️ Raise] [🗣️ Call Liar]
🔍 {caller} called LIAR! Actual ⚄s: 5 → bid true → {caller} loses a die.
🎲 {winner} is the last liar standing. 💀 {last_out} takes the stake.
```

---

### 9.7 Musical Chairs

**Tension:** reflex + attrition · **1–3 min** · **3..N** · `stake_target: last_eliminated`

`chairs = players − 1`. Music plays for a hidden duration; on stop, race to **SIT**. The
unseated player is out. Remove a chair, repeat, until two fight over one. Last seated wins.

**States:** `MUSIC → SCRAMBLE → ELIMINATE → (MUSIC | COMPLETE)`

**Flow**
1. Lobby (3..N). `chairs = players − 1`.
2. **MUSIC:** SIT button mounted and **live**; hidden duration random 5–15s.
3. Sitting during MUSIC = false start → eliminated this round (`false_start_elim`, true).
4. **SCRAMBLE:** music stops → SIT goes hot; first `chairs` presses claim seats (press
   order authoritative).
5. **ELIMINATE:** unseated player out; `chairs −= 1`; loop. 2-over-1 final → winner.

**Loser determination:** per-round unseated player; stake to `last_eliminated` (the
2-player final's loser). False-starter eliminated for the round regardless of speed.

**Server-authoritative:** SIT always clickable (false-start trap); first `chairs` valid
presses after SCRAMBLE seat; one press per player per round; lock around the last-chair
race; `scramble_window` (8s) caps the scramble.

**Config:** `min_music`/`max_music` (5/15s), `scramble_window` (8s), `false_start_elim`
(true), `min_players` (3)/`max_players` (10).

**Embeds:**
```
🎵 MUSICAL CHAIRS R{r} — chairs: {c}, still in: {n}. …don't sit yet…  [🪑 SIT]
🪑 SIT!!! grab a chair! chairs left: {c}                              [🪑 SIT]
❌ {loser} didn't find a chair. Out!
🪑 {winner} takes the last chair. 🥈 {runner_up} takes the stake.
```

---

## 10. Cross-game summary

| Game | Tension | Length | Decision | Players | Stake target | Complexity |
|---|---|---|---|---|---|---|
| Pressure Cooker | escalating gauge | 60–90s | pump (forced) | 2 | loser | low |
| Quickdraw | reflex + nerve | ~10s | when to fire | 2 | loser | low |
| Minesweeper | climbing odds | 30–60s | which tile / forfeit | 2 | loser | med |
| Hot Potato | hidden fuse | 20–90s | when/who to pass | 2..N | loser / last out | med |
| Chicken | mutual nerve | 10–40s | when to bail | 2..N | all holding | med |
| Liar's Dice | bluff / deduction | 2–6 min | raise or call | 2..N | last out | high |
| Musical Chairs | reflex + attrition | 1–3 min | when to sit | 3..N | last out | med |

---

## 11. Config reference (defaults)

**Shared / `BaseGame`**
- `challenge_timeout` 120s · `lobby_timeout` 60s · `game_max_age` 30 min
- `nickname_duration` 24h · `nickname_max_len` 32 · `per_pair_cooldown` 48–72h
- `stake_target` per game (see table) · `custom_stake_max_chars` 200

**Per game:** see each §9 entry's Config line.

---

## 12. Build order

1. **`BaseGame` (+ `BaseDuel`)** — lifecycle, roster, lobby, elimination, stake
   resolution, schema, scheduled tasks, guardrails. Foundation for everything.
2. **Pressure Cooker** — simplest duel; proves the duel path end-to-end. *(May already be
   built — fold it onto `BaseGame` if so.)*
3. **Quickdraw** — smallest middle; validates the hook boundary.
4. **Hot Potato (duel → group)** — introduces hidden-fuse scheduled tasks, then the
   eliminate-and-re-round loop for the multiplayer path.
5. **Musical Chairs** — reuses Quickdraw's false-start trap + the elimination loop.
6. **Minesweeper** — grid/turn-ownership fiddliness.
7. **Chicken** — visible shared meter + commit/bail interaction model.
8. **Liar's Dice** — most complex (private state, bid validation, reveal). Last.

---

## 13. Parked / future work

- **Reputation / honor tracker** — cross-game character scores and titles. Attach to
  `BaseGame` once; `elimination_order` gives placement-based signal for free.
- **Economy layer (Petals)** — betting on games, payouts by placement. Hooks into the same
  resolution point.
- **More games (free once `BaseGame` exists):** Russian Roulette, Higher/Lower, Tug of
  War, Odds Are, Last One Standing, Wheel of Fate, Werewolf/Mafia-lite (bigger build).
- **Per-game leaderboards & session recap** integration with the existing Poppy/DK session
  tracker.
