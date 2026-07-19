# Dungeon Keeper — PvP Games Suite Spec

> Authoritative reference for the duel/group nickname-stake game system on The Golden Meadow (TGM).
> Module roots: `src/bot_modules/cogs/<game>/cog.py` (per game) · `src/bot_modules/duels/` (shared base)
> Stack: Python · discord.py · aiosqlite
> Status: **Current** — this document describes the system as built. Designed-but-unbuilt games and
> features live in [§13 Not Yet Built / Roadmap](#13-not-yet-built--roadmap).

---

## Table of contents

1. Overview & design philosophy
2. Architecture: `BaseGame` / `BaseDuel`
3. Shared lifecycle (challenge/lobby → game → resolution → auto-revert)
4. The nickname stake mechanic
5. Stakes, guardrails & safety
6. Database schema
7. Scheduled & background tasks
8. Command surface
9. Per-game specs (built)
   - 9.1 Pressure Cooker
   - 9.2 Quickdraw
   - 9.3 Hot Potato (duel)
   - 9.4 Hot Potato (group)
   - 9.5 Chicken
   - 9.6 Musical Chairs
10. Cross-game summary table
11. Config reference
12. Implementation status
13. Not Yet Built / Roadmap

---

## 1. Overview & design philosophy

A suite of interactive, button-driven games hosted by Dungeon Keeper for TGM. Every game
resolves to a **loser**, whom the winner may rename for a configurable window (default 24h,
auto-reverting), or — if custom stakes were set — who owes an honor-based cosmetic forfeit.
The built games range from pure-reflex (Quickdraw) to nerve/brinkmanship (Chicken), and they
all share one spine so new games are cheap to add.

**Design principles**

- **One spine, many middles.** The shared lifecycle (challenge/lobby, stakes, resolution,
  nickname application, auto-revert, cooldowns, guardrails, result embed) is written once in
  `BaseGame` / `BaseDuel`. Each game implements only its own "middle."
- **Server-authoritative.** All hidden state (timers, fuses, draw delays, the pressure roll)
  lives server-side. The client never learns anything that would let a player cheat.
- **Consent-gated stakes.** In a duel, the target must **accept** before anything locks in; in
  a group game, players opt in via the lobby. Custom stakes are cosmetic and honor-based.
- **Hearth > Highlight.** Games are playful and teasing, not humiliating. Nickname content is
  guardrailed; cooldowns prevent griefing wars.

---

## 2. Architecture: `BaseGame` / `BaseDuel`

The shared machinery lives in `src/bot_modules/duels/`:

- `base_game.py` — `BaseGame` (2..N players): lifecycle, background expiry/auto-revert sweep,
  the nickname-stake flow, lobby handling for N-player games, rate limiting, group resolution,
  and the abstract DB/game hooks.
- `base_duel.py` — `BaseDuel(BaseGame)`: the fixed 2-player special case, adding the
  single-opponent challenge/accept/decline flow and pairwise winner resolution.

```
BaseGame  (2..N players)                         src/bot_modules/duels/base_game.py
   owns: roster, lobby (join/leave/start/cancel), stakes, elimination tracking,
         nickname-stake flow (winner "Name the loser" → modal → apply → auto-revert),
         result embed, cooldowns, rate limiting, permission preflight, expiry sweep
   │
   ├── BaseDuel(BaseGame)             ← fixed 2-player      src/bot_modules/duels/base_duel.py
   │       Pressure Cooker · Quickdraw · Hot Potato (duel)
   │
   └── BaseGame (used directly)       ← N-player, lobby-based
           Chicken · Hot Potato (group) · Musical Chairs
```

Each game is a cog under `src/bot_modules/cogs/<game>/`:

| Game | Cog module | Cog class | Base | `GAME_KEY` |
|---|---|---|---|---|
| Pressure Cooker | `cogs/pressure_cooker/cog.py` | `PressureCookerDuel` | `BaseDuel` | `pressure` |
| Quickdraw | `cogs/quickdraw/cog.py` | `QuickdrawDuel` | `BaseDuel` | `quickdraw` |
| Hot Potato (duel) | `cogs/hot_potato/cog.py` | `HotPotatoDuel` | `BaseDuel` | `hot_potato` |
| Hot Potato (group) | `cogs/hot_potato_group/cog.py` | `HotPotatoGroupGameCog` | `BaseGame` | `hot_potato_group` |
| Chicken | `cogs/chicken/cog.py` | `ChickenCog` | `BaseGame` | `chicken` |
| Musical Chairs | `cogs/musical_chairs/cog.py` | `MusicalChairsCog` | `BaseGame` | `musical_chairs` |

Each cog folder also carries a `game.py` (pure dataclass/logic, no Discord), a `db.py`
(per-game SQL, config shimmed to the shared `duels/db.py`), and a `views.py` (its buttons).

### Hooks each game implements

| Hook | Purpose |
|---|---|
| `render_game_state(game, guild)` | Build the current live game embed. |
| `render_result_state(game, guild, *, imposed_nick=None)` | Build the post-game result embed. |
| `build_game_view(game_id)` | Return the interactive View for the game (its buttons). |
| `handle_interaction(interaction, game)` | Process a press; mutate state; return `("continue"/"rejected"/"eliminate"/"done", id)`. |
| `on_game_start(game)` *(optional)* | Roll initial hidden state (first player, fuse, draw delay) and arm timers. |
| `on_game_resume(game)` *(optional)* | Re-arm timers on restart (cog_load). |
| `on_game_resolved(game_id)` *(optional)* | Cancel any running timers. |

Plus the abstract DB hooks (`_db_get_game`, `_db_set_state`, `_db_fetch_active_games`,
`_db_fetch_sweepable`, and — duels — `_db_create_game`/`_db_get_active_game_for_pair`, or —
group — `_db_create_lobby`/`_db_fetch_lobby_games`/`get_lobby_params`).

---

## 3. Shared lifecycle

```
   DUEL                              GROUP
   ┌─────────────┐                  ┌─────────────┐
   │  CHALLENGE  │                  │    LOBBY    │  /games <game> start [stakes]
   │  accept/    │                  │  join/leave/│
   │  decline    │                  │  start/     │
   └──────┬──────┘                  │  cancel     │
          │ accept                  └──────┬──────┘
          │                                │ host Start (≥ min_players)
   ┌──────▼───────────────────────────────▼──────┐
   │                 [MINIGAME]                    │  ← the only per-game part
   └──────────────────────┬───────────────────────┘
                          │ loser (or final loser) determined
   ┌──────────────────────▼───────────────────────┐
   │                   RESOLVE                      │  post result embed
   └──────────────────────┬───────────────────────┘
        nickname mode      │      custom-stakes mode
   ┌──────────────────────▼───────┐   ┌───────────▼────────────┐
   │ winner presses "Name the      │   │ announce only — no bot  │
   │ loser" → modal → apply nick    │   │ enforcement, no rename  │
   └──────────────────────┬────────┘   └────────────────────────┘
                          │ +sentence_hours (default 24h)
   ┌──────────────────────▼────────┐
   │          AUTO-REVERT           │  background sweep restores original nick
   └────────────────────────────────┘
```

**Challenge (duel):** `/games <game> challenge @user [stakes]` posts an embed pinging the
target with `✅ Accept` / `❌ Decline`. The target **must accept** before the game starts.
A pending challenge is swept to `EXPIRED_PENDING` **60 seconds** after it was created (the
challenge embed footer says "60 seconds to respond").

**Lobby (group):** `/games <game> start [stakes]` posts a join lobby with `✋ Join`,
`🚪 Leave`, `▶️ Start` (host only), `🚫 Cancel` (host only). The host starts once
`min_players` is met. An idle lobby is swept to `EXPIRED_LOBBY`.

**Resolve:** the game declares its loser (duel) or final loser (group, = last eliminated);
`BaseGame` posts the result embed.

**Auto-revert:** in nickname mode, a background sweep restores the original nickname once the
sentence expires (`sentence_hours`, default 24h). It survives bot restarts (sentences live in
`duel_nicks`, reverted by the recurring `_expire_loop`). If a sentenced member leaves and
rejoins before expiry, `on_member_join` re-applies the nick so they can't dodge it.

---

## 4. The nickname stake mechanic

The signature mechanic: in **nickname mode** (no custom stakes given) the winner replaces the
loser's nickname for `sentence_hours` (default 24h).

- On resolution, the result embed carries one persistent **`📝 Name the loser`** button
  (`ResultView`), clickable **only by the winner**. Pressing it opens a `NicknameModal`
  (1–32 chars). The submitted name is validated (see §5) and applied to the loser.
- DK snapshots the loser's **original nickname** (`duel_nicks.original_nick`) before renaming.
- The result embed's **"🏷️ Nickname Applied"** line reads *"**{old display name}** is now
  known as **{new nick}**"*. The old name is captured **before** `loser.edit()` and threaded
  into `render_result_state(..., original_name=…)`; the render runs after the rename, so
  reading the loser's live `display_name` there would print the new nick on both sides.
- **If the winner never names the loser**, the result is swept to `NO_NICK_SET` after 5
  minutes and **nobody is renamed**. (There is no auto-applied default/template nickname.)
- A background **auto-revert** (the per-cog `_expire_loop`) restores the original nickname
  when `expires_at` passes, DMs the loser, and logs. On `discord.Forbidden` the row is marked
  `forbidden`; on other HTTP errors it's logged and retried next tick.
- **Server owner:** Discord won't let the bot rename the guild owner, so the sentence is
  announced and the owner is asked to apply it themselves (state `NICKED`, no enforcement).
- **Overlap guard:** a player already serving a sentence can't have a second applied (that
  would snapshot the imposed nick as the "original" and corrupt the revert). The win stands
  but no new nick is applied (`NO_NICK_SET`).

**Custom stakes (optional):** if `stakes` free-text is provided at challenge/lobby time, the
game runs in **custom-stakes mode** — the result is announced only, no `📝 Name the loser`
button, no rename, no expiry sweep. Enforcement is honor-based. Custom stakes are validated
(≤ `max_stakes_length`, default 200, run through the denylist).

There is **no per-loser `stake_target` selection** in the current build: duels rename the one
loser; group games rename a single deterministic loser (see per-game specs). The multi-target
variants (`last_eliminated` / `all_eliminated` / etc.) are roadmap — see §13.

---

## 5. Stakes, guardrails & safety

**Stake types**

1. **Nickname (default):** winner renames the loser for `sentence_hours`, bot-enforced with
   auto-revert.
2. **Custom free-text (optional):** ≤ `max_stakes_length` chars, cosmetic/honor-based,
   announce-only. No custom stakes → nickname mode.

**Nickname / stakes validation** (`src/bot_modules/duels/filters.py`, applied to the winner's
chosen nick and to custom stakes)

- Length cap (`max_nick_length`, default 32; `max_stakes_length`, default 200).
- Per-guild `nick_denylist` (JSON array), plus checks against impersonating admins/mods and
  duplicating other members' display names.

**Anti-grief**

- **Challenge/start rate limit:** at most **3 per hour** per user (`_RATE_LIMIT_MAX`, in-memory
  sliding window).
- **Cooldowns:** duels use a **per-pair** cooldown (`duel_cooldowns`, keyed on the sorted
  player pair); group games use a **per-user** cooldown. Both default to `cooldown_hours` = 48.
  Cooldowns and the preflight below apply in nickname mode only (custom-stakes games skip them).
- **No concurrent sentence:** a player currently serving a nickname sentence can't start or
  join another nickname-mode game until it expires.
- **One game per pair (duel):** a pair already mid-game can't start a second.

**Bot permission preflight** (nickname mode, checked at challenge/lobby/join time so failures
surface before play, not after)

- `Manage Nicknames`.
- DK's top role must be **above** every participant's top role (except the guild owner, who is
  handled specially at rename time).

---

## 6. Database schema

Schema lives in `src/migrations/`. Each game has its own state table; nickname sentences,
cooldowns, and config are **shared** across all games via the `duels/*` tables.

**Shared** (`032_duels.sql`)

```sql
CREATE TABLE duel_nicks (            -- one row per applied nickname sentence
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL, game_type TEXT NOT NULL,
    guild_id INTEGER NOT NULL, loser_id INTEGER NOT NULL, winner_id INTEGER NOT NULL,
    original_nick TEXT, imposed_nick TEXT NOT NULL,
    applied_at REAL DEFAULT (unixepoch()), expires_at REAL NOT NULL,
    reverted_at REAL, revert_reason TEXT
);
CREATE TABLE duel_cooldowns (        -- per-pair (duel) cooldowns
    guild_id INTEGER NOT NULL, game_type TEXT NOT NULL,
    player_a INTEGER NOT NULL, player_b INTEGER NOT NULL, last_game_at REAL NOT NULL,
    PRIMARY KEY (guild_id, game_type, player_a, player_b)
);
CREATE TABLE duel_config (           -- per-guild, per-game config
    guild_id INTEGER NOT NULL, game_type TEXT NOT NULL,
    cooldown_hours INTEGER DEFAULT 48, sentence_hours INTEGER DEFAULT 24,
    allow_early_revert INTEGER DEFAULT 0, channel_allowlist TEXT DEFAULT '[]',
    nick_denylist TEXT DEFAULT '[]', max_nick_length INTEGER DEFAULT 32,
    max_stakes_length INTEGER DEFAULT 200,
    PRIMARY KEY (guild_id, game_type)
);
```

Group games add per-user cooldowns and per-game config knobs on top of this shared base.
`channel_allowlist`, `max_nick_length`, and `max_stakes_length` are enforced generically for
all six games in `duels/base_duel.py` / `base_game.py`, and are configurable per game from
the web dashboard's Games nav section (one "Config" panel per game) — see §8.
`nick_denylist` remains unexposed anywhere; it's a future feature.

**Per-game state tables** (one migration each)

| Table | Migration | Notes |
|---|---|---|
| `pressure_games` | `028_pressure_cooker.sql` | gauge, pump log, active player |
| `quickdraw_games` | `033_quickdraw.sql` (+ `037` loser time) | draw delay, fired_at, per-side reaction times |
| `hot_potato_*` (duel) | `034_hot_potato.sql` | holder, timer, pass log, style points |
| duel-group infra | `035_duel_group.sql` | roster/alive/elimination for N-player reuse of `duel_config` |
| hot potato group | `036_hot_potato_group.sql` | rounds, fuse, clockwise passing |
| `musical_chairs_*` | `038_musical_chairs.sql` | rounds, chairs, seated, phase timers |
| `chicken_*` | `039_chicken.sql` | meter, bail log |

Each per-game row carries the game-specific hidden + visible state as columns/JSON so a game
fully rehydrates after a restart.

---

## 7. Scheduled & background tasks

- **Expiry / auto-revert sweep** — each cog runs `_expire_loop` (`tasks.loop(minutes=1)`).
  It sweeps stale games (below) and reverts any `duel_nicks` row past `expires_at` that isn't
  yet reverted (restore original nick, DM the loser, mark reverted, log).
- **Stale-game reaper** (thresholds from each game's `fetch_sweepable`): `PENDING` challenges
  expire **60s** after creation; `ACTIVE` games with no activity for **5 min** become
  `ABANDONED` (no nickname consequences); `RESOLVED` games where the winner never named the
  loser become `NO_NICK_SET` after **5 min**; idle lobbies become `EXPIRED_LOBBY`.
- **In-game timers** — hidden fuses (Hot Potato), draw delays (Quickdraw), the meter climb +
  crash (Chicken), and music/scramble windows (Musical Chairs) are per-game `asyncio` tasks
  keyed to the game id; cancelled/rescheduled on the relevant interactions.
- **Restart recovery** — `BaseGame.cog_load` reloads active games (re-attach the game View,
  call `on_game_resume` to re-arm timers), resolved games (re-attach the `📝 Name the loser`
  View), and open lobbies (re-attach the lobby View). Rows whose message is gone are handled
  silently.

---

## 8. Command surface

Every game hangs off the shared **`/games`** group: `games.add_command(cog.<group>)` in each
cog's `setup()`. So the real invocation is **`/games <slug> <subcommand>`**, never `/<slug> …`.
Slugs: `pressure`, `quickdraw`, `hotpotato`, `hotpotatogroup`, `chicken`, `musicalchairs`.

> Note: Hot Potato ships as **two separate commands** — `/games hotpotato` (2-player duel) and
> `/games hotpotatogroup` (N-player group) — backed by separate cogs, `GAME_KEY`s, and stats.

**Duels** (`/games pressure`, `/games quickdraw`, `/games hotpotato`)

| Command | Who | Effect |
|---|---|---|
| `challenge <user> [stakes]` | Anyone | Challenge a target (accept/decline). |

**Group games** (`/games hotpotatogroup`, `/games chicken`, `/games musicalchairs`)

| Command | Who | Effect |
|---|---|---|
| `start [stakes]` | Anyone | Open a join lobby. |

**`cancel`/`stats`/`revert`/`config` — all removed, none were ever reachable.** Every one of
these subcommands (plus `config`, see below) was stripped from each cog's command tree in
`setup()` (`cog.<group>.remove_command(...)`) before it ever registered under `/games`, so
none of them were ever actually callable in Discord — a pending challenge could only be
cancelled via timeout (60s) or the lobby's `🚫` button, W/L stats and Hot Potato's style points
had no way to be viewed, and the nickname "early revert" toggle (`allow_early_revert`) had no
command to exercise it, on any game, ever. Rather than wire these up, the dead command methods
and their now-orphaned db-layer stats/revert-shim functions were deleted outright — they
weren't needed for these short-lived games. `allow_early_revert` and `nick_denylist` remain
unused columns on the shared `duel_config` table (see §10) but are no longer surfaced
anywhere. A pending challenge still self-expires after 60s (see §7's stale-game reaper) so
dropping `cancel` has no user-facing gap.

**Per-game config — web dashboard only.** Settings (cooldowns, sentence duration,
channel allowlist, nickname/stakes length caps, plus each game's own mechanics knobs) live on
the web dashboard's **Games** nav section, one "Config" panel per game
(`config-games-<slug>.js` / `PUT /api/config/games-<slug>`):

| Game | Panel fields |
|---|---|
| Pressure Cooker | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length` |
| Quickdraw | same shared fields, plus `min_delay`, `max_delay`, `draw_window` |
| Hot Potato (duel) | same shared fields, plus `min_timer`, `max_timer` |
| Hot Potato (group) | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `min_fuse`, `max_fuse`, `min_hold`, `min_players`, `max_players` |
| Chicken | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `climb_duration`, `min_players`, `max_players` |
| Musical Chairs | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `min_music`, `max_music`, `scramble_window`, `false_start_elim`, `min_players`, `max_players` |

`channel_allowlist`/`max_nick_length`/`max_stakes_length` are exposed for all six games, not
just Pressure Cooker as the old (dead) commands had it — they were always enforced
generically in the shared base classes, so this closes a real gap rather than adding scope.

**In-embed controls** (built): `✅ Accept` / `❌ Decline` (duel challenge); `✋ Join` /
`🚪 Leave` / `▶️ Start` / `🚫 Cancel` (lobby); the game's own button(s) (`💨 PUMP`,
`🔫 FIRE`, `🤲 Pass`, `🐔 BAIL`, `🪑 SIT`); `📝 Name the loser` (winner-only, on
nickname-mode results). There is no "How to Play", "Run Again", or "I'll honor this" control
(those are roadmap — see §13).

---

## 9. Per-game specs (built)

> Each game specs only its "middle": states, flow, loser determination, server-authoritative
> bits, config knobs, and embed shape. Everything in §3–§7 is inherited.

### 9.1 Pressure Cooker

**Escalating gauge · 2 players · duel · winner renames loser**

Two players alternate pressing **PUMP** on a shared gauge that rises by a random **1–15** per
press (`ROLL_MIN`/`ROLL_MAX` constants in `game.py`). When it reaches/exceeds **100**
(`GAUGE_CEILING`), whoever pumped last **loses**. The first pump can never bust (max roll 15 <
100).

**States:** `PENDING → ACTIVE → RESOLVED → NICKED` (or `RESOLVED_NO_NICK` for custom stakes).

**Flow**
1. On accept, gauge = 0, random first player, state `ACTIVE`.
2. Active player presses **💨 PUMP** → gauge += `randint(1, 15)`; turn passes; off-turn presses
   are rejected.
3. Gauge ≥ 100 → last presser loses.

**Server-authoritative:** the roll is server-side per press; a per-game lock prevents a
double-press race; turn ownership is enforced.

**Config knobs:** none game-specific (gauge ceiling and roll are hardcoded constants). Shared:
`cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`.

### 9.2 Quickdraw

**Reflex + nerve · 2 players · duel · winner renames loser**

Wait for the hidden **DRAW!** then slap **FIRE**. Fastest wins; firing early is an instant loss.

**States (`qd_state`):** `WAITING → DRAW → WINNER_FIRED → COMPLETE`.

**Flow**
1. On accept, `WAITING`; the FIRE button is live (the trap needs it). Roll a hidden delay,
   `min_delay`–`max_delay` (default **3.0–8.0s**).
2. Press during `WAITING` = **false start** → presser loses immediately.
3. Delay elapses → `DRAW` (record `fired_at`).
4. First valid FIRE wins and moves to `WINNER_FIRED`; the opponent stays blind (the button is
   still live) so their own reaction time is timed for the delta, then `COMPLETE`.
5. If nobody fires within `draw_window` (default **5.0s**) → **VOID**, no penalty. If the winner
   fired but the opponent never did, resolve winner-only ("didn't draw").

**Server-authoritative:** the draw delay + draw window are hidden server-side timers; reaction
= `press_ts − fired_at`; a lock guards against double-resolution.

**Config knobs:** `min_delay` (3.0), `max_delay` (8.0), `draw_window` (5.0) + shared.

### 9.3 Hot Potato (duel)

**Hidden fuse · 2 players · duel · winner renames loser**

A bomb passes between two players on a hidden fuse; the holder at detonation loses.

**States:** `PENDING → ACTIVE → RESOLVED → NICKED` (or `RESOLVED_NO_NICK`).

**Flow**
1. On accept, the challenger holds; roll a hidden fuse, `min_timer`–`max_timer` (default
   **10.0–45.0s**).
2. Holder presses **🤲 Pass** → the bomb alternates to the other player (no min-hold in the
   duel). The fuse keeps burning.
3. Fuse expires → current holder loses.

**Style points:** passes made deep in the "danger zone" earn cosmetic **style points**
(`compute_style_points`), accumulated in `hot_potato_style` — write-only; there's no surface
that displays them.

**Server-authoritative:** the fuse is a hidden scheduled task; passing is locked to the holder;
detonation cancels/reschedules on resolution.

**Config knobs:** `min_timer` (10.0), `max_timer` (45.0) + shared.

### 9.4 Hot Potato (group)

**Hidden fuse, progressive elimination · 2..N players · lobby · final loser renamed**

The group version re-lights a fresh fuse each round and eliminates the holder at detonation
until one remains.

**States:** `LOBBY → ACTIVE (rounds) → RESOLVED → NICKED` (or `RESOLVED_NO_NICK`).

**Flow**
1. Lobby (`min_players`/`max_players`, default **2/10**). On start, a random player holds; roll
   a fuse `min_fuse`–`max_fuse` (default **20.0–60.0s**).
2. Holder presses **🤲 Pass** after a **`min_hold`** wait (default **2.0s**, anti-ping-pong).
   The bomb passes **clockwise** through the alive players (fixed order — there is no
   choose/select target mode).
3. Fuse expires → holder eliminated. ≥2 remain → re-roll a fresh fuse, next player holds,
   continue; else the last survivor wins.
4. **Final loser** = the last player eliminated; the winner renames them.

**Creeping-dread tell:** the embed's emoji escalates as the current fuse burns down
(`shake_emoji`), since the fuse itself is hidden.

**Server-authoritative:** fuse hidden; `min_hold` server-enforced; pass locked to the holder;
detonation is a scheduled task cancelled/rescheduled on each elimination.

**Config knobs:** `min_fuse` (20.0), `max_fuse` (60.0), `min_hold` (2.0), `min_players` (2),
`max_players` (10) + shared.

### 9.5 Chicken

**Mutual nerve / brinkmanship · 2..N players · lobby · single crasher renamed**

Everyone is **holding** from the start. A **visible** shared meter climbs 0→100. Press
**🐔 BAIL** to drop out safely. If the meter hits 100 with players still holding → **CRASH**.

**States:** `LOBBY → ACTIVE (CLIMBING) → RESOLVED / RESOLVED_NO_NICK`.

**Flow**
1. Lobby (`min_players`/`max_players`, default **2/8**). On start, `CLIMBING`; the meter climbs
   over `climb_duration` (default **25.0s**), the embed ticking every ~2s.
2. Each player has one **🐔 BAIL** (there is no separate "commit" press — you're committed by
   default and bail once). The bail's meter % is recorded.
3. **Crash at 100** with players still holding → resolve. If everyone bails before the crash,
   the last to bail wins (cosmetic, no rename).

**Loser determination (`resolve_crash`):**
- Crashers + at least one bailer → **winner** = bravest bailer (highest meter % at bail),
  **loser** = a single deterministic crasher (lowest user id) who takes the nick.
- **Nobody bailed** (total wipeout) → cosmetic, no winner, **no rename**.

(Only one crasher is renamed — there is no "everyone still holding loses" multi-target stake in
the current build.)

**Server-authoritative:** the meter climb is a server-side scheduled progression; the crash is
a scheduled task at `start + climb_duration`; bail order is authoritative under the per-game lock.

**Config knobs:** `climb_duration` (25.0), `min_players` (2), `max_players` (8) + shared.

### 9.6 Musical Chairs

**Reflex + attrition · 3..N players · lobby · runner-up renamed**

`chairs = players − 1` each round. Music plays for a hidden duration; on stop, race to **SIT**.
The unseated player(s) are out. Remove a chair, repeat, until one remains.

**States (`phase`):** `LOBBY → ACTIVE (MUSIC → SCRAMBLE → …) → RESOLVED / RESOLVED_NO_NICK`.

**Flow**
1. Lobby (`min_players`/`max_players`, default **3/10**). On start, `MUSIC`; the SIT button is
   live; hidden music duration `min_music`–`max_music` (default **5.0–15.0s**).
2. Sitting during `MUSIC` = false start → if `false_start_elim` (default **on**) the presser is
   eliminated this round; otherwise it's rejected.
3. `SCRAMBLE`: music stops, SIT goes hot; the first `chairs` valid presses claim seats
   (press-order authoritative), capped by `scramble_window` (default **8.0s**).
4. Unseated players are eliminated; loop with one fewer chair until one survivor.
5. **Final loser** = last eliminated (the runner-up); the winner renames them.

**Server-authoritative:** SIT is always clickable (the false-start trap); the first `chairs`
valid scramble presses seat; one press per player per round; a lock guards the last-chair race.

**Config knobs:** `min_music` (5.0), `max_music` (15.0), `scramble_window` (8.0),
`false_start_elim` (1), `min_players` (3), `max_players` (10) + shared.

---

## 10. Cross-game summary (built)

| Game | Slug | Tension | Players | Base | Loser renamed |
|---|---|---|---|---|---|
| Pressure Cooker | `pressure` | escalating gauge | 2 | `BaseDuel` | last pumper |
| Quickdraw | `quickdraw` | reflex + nerve | 2 | `BaseDuel` | false-starter / slower draw |
| Hot Potato (duel) | `hotpotato` | hidden fuse | 2 | `BaseDuel` | holder at detonation |
| Hot Potato (group) | `hotpotatogroup` | hidden fuse | 2..N | `BaseGame` | last eliminated |
| Chicken | `chicken` | mutual nerve | 2..N | `BaseGame` | single crasher (lowest id) |
| Musical Chairs | `musicalchairs` | reflex + attrition | 3..N | `BaseGame` | runner-up (last eliminated) |

---

## 11. Config reference (defaults)

**Shared `duel_config`** (all games, via `duels/db.py._CONFIG_DEFAULTS`)
- `cooldown_hours` 48 · `sentence_hours` 24
- `channel_allowlist` `[]` · `max_nick_length` 32 · `max_stakes_length` 200
- `allow_early_revert` 0 · `nick_denylist` `[]` — real columns, but unused: no command or
  web panel reads or writes either one (the games that carried a `revert` command never had
  it wired into the live tree — see §8)

**Rate limit:** 3 challenges/starts per user per hour (in-memory, not configurable).

**Per-game knobs:** see each §9 entry's "Config knobs" line. Group games also default
`lobby_timeout` to 60.0s.

---

## 12. Implementation status

All six games in §9 are built and live under `src/bot_modules/cogs/`, on the shared
`BaseGame`/`BaseDuel` foundation in `src/bot_modules/duels/`. Games also pay economy rewards on
completion via `bot_modules/economy/game_rewards.pay_game_rewards` (XP/economy for
participants, with a winner bonus).

The suite was built roughly in this order: `BaseGame`/`BaseDuel` → Pressure Cooker → Quickdraw
→ Hot Potato (duel → group) → Musical Chairs → Chicken.

---

## 13. Not Yet Built / Roadmap

Everything below is **designed but not implemented** — no code exists for it in `src/`. Kept
here so the intent isn't lost.

### 13.1 Minesweeper Duel (unbuilt)

**Climbing odds · 30–60s · 2 players · winner renames loser**

A 4×4 grid, one hidden mine. Players alternate clicking tiles. Hit the mine, you lose. No
adjacency hints — only "tiles remaining," so the odds visibly climb each safe pick.

- **States:** `PLAYING → COMPLETE`.
- **Flow:** place one mine at random 0–15; coin-flip first player. Active clicks an unrevealed
  tile — safe → ✅, turn passes; mine → 💥, clicker loses. Active may instead press
  🏳️ **Forfeit** to concede. With 15 revealed, the last tile is the mine.
- **Loser:** mine-revealer, or forfeiter.
- **Climbing-odds display:** show `1 / tiles_remaining` for the player about to move.
- **Server-authoritative:** mine rolled once, hidden; turn ownership enforced; 16 buttons
  across 4 rows (Discord cap).
- **Config (proposed):** `grid_size` (4), `mine_count` (1), `turn_timeout` (off), `show_odds`
  (true).

### 13.2 Liar's Dice (unbuilt)

**Bluff / deduction · 2–6 min · 2..N players · final loser renamed**

Each player has a cup of hidden dice. Bid up how many of a face exist across *all* cups, or
**call Liar**. On a call, all cups reveal; the wrong party loses a die. Lose all dice = out.
Last player with dice wins.

- **States:** `ROLLING → BIDDING → REVEAL → (ROLLING | COMPLETE)`.
- **Flow:** each starts with `starting_dice` (5). All roll privately (ephemeral cup). Active
  player **raises** (quantity-up always legal; face-up legal at same-or-higher quantity) or
  **calls Liar**. Reveal counts matches (`aces_wild`, default true); bid met → caller loses a
  die, else bidder loses a die. Die-loser starts next round; 0 dice = eliminated.
- **UI:** 🎲 **My Cup** re-shows dice ephemerally; ⬆️ **Raise** and 🗣️ **Call Liar** for the
  active player; others see the live bid.
- **Server-authoritative:** all dice server-side; ephemeral reveals never leak; raises
  validated; rotating turn pointer skips eliminated; `turn_timeout` (60s) auto-calls Liar on
  stall.
- **Config (proposed):** `starting_dice` (5), `aces_wild` (true), `min_players`/`max_players`
  (2/6), `turn_timeout` (60s).

### 13.3 Per-loser `stake_target` variants (unbuilt)

The current build renames a single loser (duel loser, group's last-eliminated, or Chicken's
lowest-id crasher). The designed multi-target model — selectable `stake_target` of
`loser` / `first_eliminated` / `last_eliminated` / `all_eliminated`, with non-targeted players
getting cosmetic standings only — is not implemented.

### 13.4 Designed-but-unbuilt UI niceties

- **`❓ How to Play`** — an ephemeral rules button on each game.
- **`🔁 Run Again` / rematch** — a one-press rematch path on the result embed.
- **`🫡 I'll honor this`** — a cosmetic accountability button on custom-stakes results.
- **Hot Potato (group) `choose` pass mode** — a player-select for who receives the bomb (today
  it only passes clockwise).

### 13.5 Parked / future work

- **Reputation / honor tracker** — cross-game character scores and titles; `elimination_order`
  already gives placement-based signal.
- **Economy layer (betting/payouts by placement)** — games already pay participation/win
  rewards via `pay_game_rewards`; wagering and placement payouts are not built.
- **Per-game leaderboards & session recap** integration with the Poppy/DK session tracker.
- **More games (cheap once `BaseGame` exists):** Russian Roulette, Higher/Lower, Tug of War,
  Odds Are, Last One Standing, Wheel of Fate, Werewolf/Mafia-lite (bigger build).
