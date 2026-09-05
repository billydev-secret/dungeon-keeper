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

Plus the abstract DB hooks (`_db_get_game`, `_db_write_state`, `_db_fetch_active_games`,
`_db_fetch_sweepable`, and — duels — `_db_create_game`/`_db_get_active_game_for_pair`, or —
group — `_db_create_lobby`/`_db_fetch_lobby_games`/`get_lobby_params`).

`_db_set_state` itself is **concrete** on `BaseGame` (a template method): it writes via the
cog's `_db_write_state`, then fires `_on_terminal_state` for every game-ending transition
(`RESOLVED`, `RESOLVED_NO_NICK`, `ABANDONED`, `VOID`, `EXPIRED_PENDING`, `EXPIRED_LOBBY`).
That hook is the single seam where the economy observes a game ending — cogs must route all
state changes through `_db_set_state`, never call their db module's `set_game_state` directly.

---

## 3. Shared lifecycle

```
   DUEL                              GROUP
   ┌─────────────┐                  ┌─────────────┐
   │  CHALLENGE  │                  │    LOBBY    │  /games <game> start [stakes] [wager]
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

**Challenge (duel):** `/games <game> challenge @user [stakes] [wager]` posts an embed pinging the
target with `✅ Accept` / `❌ Decline`. The target **must accept** before the game starts.
A pending challenge is swept to `EXPIRED_PENDING` after
`duels.db.CHALLENGE_RESPONSE_SECONDS` (**5 minutes**), and the card carries a live
`<t:…:R>` countdown to that moment rather than a footer stating a number. One constant
feeds all four places that have to agree — the `ChallengeView` timeout, the countdown,
the late-presser copy, and the `state = 'PENDING'` cutoff in each game's
`fetch_sweepable_games`. It was 60 seconds hard-coded in each of them until 2026-08-30.
A challenge still inside that window when the bot restarts keeps its buttons: `cog_load`
re-attaches a persistent `ChallengeView` carrying the card's original deadline
(`created_at + CHALLENGE_RESPONSE_SECONDS`), and a press after the deadline gets the same
timed-out copy (`views.CHALLENGE_TIMED_OUT_TEXT`) a late presser on the original card gets.
Before 2026-09-04 the view was simply lost, and Accept / Decline answered "interaction
failed" until the sweep flipped the card to Expired.

**Lobby (group):** `/games <game> start [stakes] [wager]` posts a join lobby with `✋ Join`,
`🚪 Leave`, `▶️ Start` (host only), `🚫 Cancel` (host only). The host starts once
`min_players` is met, and the lobby **starts itself** the moment it reaches `max_players`
(the join that fills it runs the same start path the host's press does). A lobby lives
`duels.db.LOBBY_IDLE_SECONDS` (**5 minutes**) from its last join or leave — the card carries a
live `⏱️ Closes <t:…:R>` field, refreshed on every join/leave, and says every join resets the
clock; the sweep pings the host once `LOBBY_WARNING_SECONDS` (60 s) before the close
(`lobby_warned_at`, re-armed by any later action); and an idle lobby is swept to
`EXPIRED_LOBBY` with a card that says what happened — "Nobody pressed ▶️ Start in time" when the
floor was met, "Not enough people joined" when it wasn't. It was 90 seconds hard-coded in each
sweep with nothing on the card until 2026-09-04: a full ten-player Musical Chairs lobby died
under its host on 2026-08-17 while people were reading the rules (duels-party-115).

**Resolve:** the game declares its loser (duel) or final loser (group, = last eliminated);
`BaseGame` posts the result embed. Every result card carries **`🔁 Run It Back`**
(`REMATCH_WINDOW_SECONDS`, 5 minutes, enforced in the button's own callback so the persistent
view survives a restart and a re-attached card past its window simply omits the button). A
duelist's press re-posts the challenge card from them to the other duelist — Accept is still
the other's to press, so nobody's coins or nickname go on the line without them saying so, and
a wager is declared now and taken at accept exactly as a typed challenge is. The lobby host's
press reopens a lobby with the same custom stakes, wager and nickname flag, seats the host
(taking their ante), pings the old roster to press Join, and the lobby starts itself when full.
That public ping leaves off anyone the host holds a no-contact pair with — they are simply not
named, the same shape as a player who has since left the server, so nothing marks them out
(the join gate would have turned them away regardless). Both go through `_base_challenge` /
`_base_lobby`, so they hit every gate the command does: the enabled switch, the channel
allowlist, the no-contact list, the sentence and cooldown preflights, and the wager precheck
(the persisted `stakes_text` gives the custom half back via `filters.custom_stakes_from`, and
the ante via `wager_svc.game_ante(..., live_only=False)` — the finished game's wager rows are
all `settled` / `refunded` by then, so the live-rows read a lobby joiner uses would answer 0
and quietly drop the wager from the rematch). Reaching `RESOLVED` /
`RESOLVED_NO_NICK` also writes the
game's one `games_game_history` row from `_on_terminal_state` (`game_id` `"<GAME_KEY>:<id>"`,
`game_type` = `GAME_KEY`, host = the challenger or lobby host, `player_count` = everyone who
played including the eliminated, `started_at` = `created_at`, guild id set, payload
`players` / `winner_id` / `loser_id` / `state`). The games keep their own tables and never had
a `games_active_games` row for `end_game` to archive, so until 2026-09-04 they were paid by
the economy yet absent from Play Statistics, `/recap` and the game-night session. The write is
idempotent — the hook can fire more than once per game — and an unsettled end (`ABANDONED`,
`VOID`, `EXPIRED_*`, `DECLINED`) records nothing, the same rule as the faucet.

**Auto-revert:** in nickname mode, a background sweep restores the original nickname once the
sentence expires (`sentence_hours`, default 24h). It survives bot restarts (sentences live in
`duel_nicks`, reverted by the recurring `_expire_loop`). If a sentenced member leaves and
rejoins before expiry, `on_member_join` re-applies the nick so they can't dodge it.

---

## 4. The nickname stake mechanic

The signature mechanic: in **nickname mode** the winner replaces the loser's nickname for
`sentence_hours` (default 24h).

Nickname mode is an explicit per-game flag (`nick_stake`, migration 177), **not** an
inference from `stakes_text`. Every challenge/lobby command takes `nickname:`; left unset it
defaults to on when nothing else is staked and off otherwise, so a bare challenge still means
what it always did. Turning it off with nothing else staked is refused — a duel with no stake
is not a duel. `filters.resolve_nick_stake` decides it at creation and
`filters.game_is_nick_stake` reads it back, falling through to the old
`stakes_text is None` inference for rows written before the flag existed.

> Until 2026-08-22 nickname mode *was* `stakes_text is None`, which made the rename mutually
> exclusive with coins and with custom stakes. A Pressure Cooker game staked as "24 hour
> nickname change" **plus** 500 coins (pressure_games 41) therefore offered nobody a rename
> button, and the players spent the aftermath asking where it had gone.

- On resolution, the result embed carries one persistent **`📝 Name the loser`** button
  (`ResultView`), clickable **only by the winner**. Pressing it opens a `NicknameModal`
  (1–32 chars). The submitted name is validated (see §5) and applied to the loser.
- DK snapshots the loser's **original nickname** (`duel_nicks.original_nick`) before renaming.
- The result embed's **"🏷️ Nickname Applied"** line reads *"**{old display name}** is now
  known as **{new nick}**"*. The old name is captured **before** `loser.edit()` and threaded
  into `render_result_state(..., original_name=…)`; the render runs after the rename, so
  reading the loser's live `display_name` there would print the new nick on both sides.
- **If the winner never names the loser**, the result is swept to `NO_NICK_SET` after
  `NAMING_WINDOW_SECONDS` (**30 minutes**) and **nobody is renamed**. (There is no
  auto-applied default/template nickname.) The winner gets **one** in-channel ping at
  `NAMING_REMINDER_SECONDS` (2 minutes) naming the loser and the button, with a live `<t:…:R>`
  to the close (`nick_reminded_at`, so a restart can't ping twice). The **winner** starting a
  new game against the loser ends the window early (`superseded`, written only once the new
  game is actually made — a challenge that bounces off a later gate leaves it open) rather
  than being refused for the rest of it; the **loser** can't end it for them (their challenge,
  typed or via Run It Back, is refused with "The winner of your last game hasn't named you
  yet…"), or a lost nickname duel plus one quick press would wipe the rename. Until
  2026-09-04 the window was 5 minutes with no reminder, and half of all Hot Potato winners
  let it lapse (duels-party-118).
- **Every game that concludes without a rename says why**: `nick_reason` (migration 207) is
  one of `winner_timeout`, `loser_outranks`, `loser_left`, `winner_left`, `already_serving`,
  `superseded` — all written through `BaseGame._conclude_unnamed`, the one path to
  `NO_NICK_SET`. The no-contact gate writes `already_serving`, the same reason the copy it
  borrows would (the pair never leaks into the game row). Rows from before 207 stay NULL.
- A background **auto-revert** (the per-cog `_expire_loop`) restores the original nickname
  when `expires_at` passes, DMs the loser, and logs. On `discord.Forbidden` the row is marked
  `forbidden`; on other HTTP errors it's logged and retried next tick.
- **Server owner:** Discord won't let the bot rename the guild owner, so the sentence is
  announced and the owner is asked to apply it themselves (state `NICKED`, no enforcement).
  The result embed says so plainly ("has to set … themselves") via
  `render_result_state(self_apply_nick=…)` rather than claiming the rename happened, the
  follow-up mentions the loser so it can't be scrolled past, and `_owner_notice` warns at
  challenge/lobby time that this is how it will go. Same treatment for a loser whose role
  outranks the bot: the winner's chosen name is handed over publicly instead of dying in an
  ephemeral only the winner sees.
- **Overlap guard:** a player already serving a sentence can't have a second applied (that
  would snapshot the imposed nick as the "original" and corrupt the revert). The win stands
  but no new nick is applied (`NO_NICK_SET`).

**Custom stakes (optional):** `stakes` free-text at challenge/lobby time is honour-based —
DK announces it and enforces nothing. Validated (≤ `max_stakes_length`, default 200, run
through the denylist).

**Wagers:** a `wager:` escrows an ante from each side at accept (duels) or join (lobbies) and
pays the pot to the winner, minus an optional house rake (`wager_rake_pct`, economy-side,
default 0 — named on the payout when priced). A wagered game that isn't also a nickname game
skips the nickname preflight (Manage Nicknames / active-sentence / group cooldown) and lands
in `RESOLVED_NO_NICK`, which still pays the pot (it's in the settling set).

**All three combine.** `filters.resolve_stakes_text` composes the persisted `stakes_text` from
whichever are live, one line each — custom text, then the wager (already formatted in the
guild's currency vocabulary), then the nickname forfeit. That one string is what every
downstream embed renders as "📋 Stakes", so a two-stake game reads as a two-stake game on the
challenge card, during play *and* at settlement — the coins used to appear only on the
challenge card and again at payout ("Oh there were 2 stakes 👀", game night 2026-08-21). A
plain nickname-only game still persists `stakes_text = NULL` and each cog's own fallback
wording, so the commonest shape of game is untouched. The challenge card adds the
"nothing is charged unless accepted" caveat, which is true only while pending and so is never
persisted; the lobby's own money field shows the **pot** (which grows as people join) rather
than repeating the ante the stakes field already names.

`resolve_nick_stake` is called only **after** `validate_stakes` has normalised the text —
whitespace-only stakes clean away to `None`, and reading the raw string would answer
"something else is staked" for a game that ends up staking nothing, skipping the nickname
preflights and then falling back into nickname mode at settlement anyway.

There is **no per-loser `stake_target` selection** in the current build: duels rename the one
loser; group games rename a single deterministic loser (see per-game specs). The multi-target
variants (`last_eliminated` / `all_eliminated` / etc.) are roadmap — see §13.

---

## 5. Stakes, guardrails & safety

**Stake types**

1. **Nickname (`nickname:`, default on when nothing else is staked):** winner renames the
   loser for `sentence_hours`, bot-enforced with auto-revert.
2. **Custom free-text (`stakes:`):** ≤ `max_stakes_length` chars, cosmetic/honour-based,
   announce-only.
3. **Coin wager (`wager:`):** escrowed ante per player, pot to the winner.

All three are independent and combine freely (§4); the persisted `stakes_text` lists whichever
are live. Only "none of the three" is refused.

**Nickname / stakes validation** (`src/bot_modules/duels/filters.py`, applied to the winner's
chosen nick and to custom stakes)

- Length cap (`max_nick_length`, default 32; `max_stakes_length`, default 200).
- Per-guild `nick_denylist` (JSON array), plus checks against impersonating admins/mods and
  duplicating other members' display names. The configured extras are matched **literally and
  case-insensitively, as whole words**, not as regexes — they are typed into the "Extra Banned
  Words" box on each game's dashboard panel, and admin-typed regex punctuation would either
  raise or silently never match. Each entry is escaped and fenced with word-boundary guards on
  whichever of its ends is a word character, so `c++` still bans that literal text while a
  short entry can't swallow the longer words it merely sits inside ("ass" does not block
  "class"). The built-in `DEFAULT_NICK_DENYLIST` patterns are still regexes.

**Anti-grief**

- **Challenge/start rate limit:** `challenge_limit_per_hour` per user per game, an in-memory
  sliding window over `duel_config`'s per-guild dial (default **30**, `0` = no limit; set from
  each game's dashboard Config panel). It was a hardcoded 3 until 2026-08-22, which is a spam
  brake set at the pace of an idle channel — the most engaged player at a games night hit it
  twice in one evening.
- **Cooldowns:** duels use a **per-pair** cooldown (`duel_cooldowns`, keyed on the sorted
  player pair, written for every settled duel by `BaseDuel._record_rematch_cooldown` from the
  terminal seam — the timer-driven Hot Potato path included — and read in `_base_challenge`);
  group games use a **per-user** cooldown (`duel_group_cooldowns`, written at resolution, read
  at lobby open and join). Both read the same `cooldown_hours` dial and both **default to 0**.
  Until 2026-09-04 the three duel panels offered the dial and nothing read it, while the group
  games enforced it at 48 hours — one nickname Chicken locked its whole roster out of nickname
  Chicken for two days (duels-party-116); 0 is what every duel actually behaved like. The
  cooldown and the preflight below apply in nickname mode only: a wagered or custom-stakes
  rematch is never held back, and the refusal names the time left and says so.
- **No concurrent sentence:** a player wearing a nickname sentence can't stake their
  nickname again until it ends — the refusal says exactly that and points at `nickname: False`
  with a wager or stakes, because that is all that is blocked. (The old line claimed they
  "can't play again until it expires"; the loser of sentence 23 played seven wagered Pressure
  Cooker games while wearing it.)
- **Refusals** on all six games go through `BaseGame._refuse`, which prefixes `❌ ` once and
  answers ephemerally (via the followup when the response is already used). It is deliberately
  **not** in the style contract's `_SEND_WRAPPERS` — that sweep expects a wrapper to forward its
  literal verbatim — and `tests/test_duels_copy.py` pins the prefix instead.
- **One game per pair (duel):** a pair already mid-game can't start a second.

**No-contact list** (`docs/no_contact_spec.md`; `BaseGame._blocked_pair`,
`_blocked_with_any`, `_refuse_rename_across_pair`, so every game on `BaseGame` inherits
all three gates). Each refusal is an ordinary outcome the surface already produced, never a
new "blocked" line, so the blocked party cannot tell:

- **Challenge:** a challenger who holds a pair with the target gets the existing
  "❌ You two already have a game in progress." ephemeral — placed after the guild-wide
  enabled/allowed checks so it reads as believable, and before the rate limit so it costs no
  strike. This is the one duel surface that **records an attempt** (surface
  `duel_challenge`): the challenger typed the other's name on purpose.
- **Lobby join:** a joiner who holds a pair with anyone already seated (host included) gets
  the lobby's own "❌ You're on cooldown for this game — try again later." — a private
  condition nobody else can check, where "full" or "no longer open" would be contradicted by
  the card. A genuine cooldown names the time left ("try again in **1h 30m**"); the gate uses
  the timeless form of the same sentence (`_cooldown_copy(None)`), which a real cooldown could
  also produce. Runs before the nickname preflight.
- **Name the Loser:** when winner and loser hold a pair, both the button press and the modal
  submit (re-checked under the lock, so a modal opened before the pair existed still applies
  nothing) get the "already serving a nickname sentence" line. The win stands and the game
  concludes at `NO_NICK_SET` with no rename; the string is shared with the genuine
  sentence-in-progress path (`_sentence_in_progress_copy`) so the two can never drift.

Tests: `tests/test_duels_no_contact.py`.

**Bot permission preflight** (nickname mode, checked at challenge/lobby/join time so failures
surface before play, not after)

- `Manage Nicknames` — **hard gate**: missing it aborts (the bot can rename no one, so a
  nickname-stake game is pointless).
- Role hierarchy is **non-fatal**: a participant whose top role sits at or above DK's own
  (a staff member, say) no longer blocks the game. The challenge/lobby proceeds with a
  `⚠️`-warning naming who can't be renamed, and if one of them loses the win stands with **no
  nickname applied** (`_unrenameable_members` / `_unrenameable_notice` in `base_game.py`,
  skipped at rename time like the guild-owner and left-server paths). The guild owner is
  excluded from the warning — they self-apply the sentence at rename time.

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
    channel_allowlist TEXT DEFAULT '[]',
    nick_denylist TEXT DEFAULT '[]', max_nick_length INTEGER DEFAULT 32,
    max_stakes_length INTEGER DEFAULT 200,
    challenge_limit_per_hour INTEGER NOT NULL DEFAULT 30,   -- 177; 0 = no limit
    PRIMARY KEY (guild_id, game_type)
);
```

Group games add per-user cooldowns and per-game config knobs on top of this shared base.
`channel_allowlist`, `max_nick_length`, and `max_stakes_length` are enforced generically for
all six games in `duels/base_duel.py` / `base_game.py`, and are configurable per game from
the web dashboard's Games nav section (one "Config" panel per game) — see §8.
`nick_denylist` is set from the same panels as of 2026-08-29 ("Extra Banned Words",
comma-separated; stored lowercased and de-duplicated, capped at 40 entries of 64 characters).
`duel_config` originally also carried an `allow_early_revert` column (migration `032`); nothing
ever read or wrote it, and `194_drop_dead_game_config_columns.sql` (2026-08-30) dropped it
outright — it is not merely absent from `duels/db.py._CONFIG_DEFAULTS`, it no longer exists as
a column at all (see §11).

**Per-game state tables** (one migration each)

| Table | Migration | Notes |
|---|---|---|
| `pressure_games` | `028_pressure_cooker.sql` | gauge, pump log, active player |
| `quickdraw_games` | `033_quickdraw.sql` (+ `037` loser time) | draw delay, fired_at, per-side reaction times |
| `hot_potato_*` (duel) | `034_hot_potato.sql` | holder, timer, pass log, style points |
| duel-group infra | `035_duel_group.sql` | roster/alive/elimination for N-player reuse of `duel_config` |
| hot potato group | `036_hot_potato_group.sql` | rounds, fuse, clockwise passing |
| `musical_chairs_*` | `038_musical_chairs.sql` | rounds, chairs, seated, phase timers |
| `chicken_*` | `039_chicken.sql` (+ `208` hidden `crash_at`) | meter, bail log |

`177_duel_nick_stake.sql` adds `nick_stake INTEGER NOT NULL DEFAULT 0` to all six state
tables (backfilled to `stakes_text IS NULL`, reproducing the old inference exactly) and
`challenge_limit_per_hour` to `duel_config`.

`208_chicken_hidden_crash_hp_min_hold_mc_reruns.sql` (2026-09-04) replaces
`chicken_config.climb_duration` with `min_climb` / `max_climb` (an existing value becomes the
ceiling), adds the hidden `chicken_games.crash_at`, adds `hot_potato_config.min_hold` and
`mc_games.reruns`.

Each per-game row carries the game-specific hidden + visible state as columns/JSON so a game
fully rehydrates after a restart.

---

## 7. Scheduled & background tasks

- **Expiry / auto-revert sweep** — each cog runs `_expire_loop` (`tasks.loop(minutes=1)`).
  It sweeps stale games (below) and reverts any `duel_nicks` row past `expires_at` that isn't
  yet reverted (restore original nick, DM the loser, mark reverted, log).
- **Stale-game reaper** (every threshold is a constant in `duels/db.py`, read by each game's
  `fetch_sweepable` **and** by the card that states the number, so the two can't disagree):
  `PENDING` challenges expire after `CHALLENGE_RESPONSE_SECONDS` (**5 minutes**, see §3 — it
  was 60s until 2026-08-30); `ACTIVE` games with no activity become `ABANDONED` after
  `active_idle_seconds(GAME_KEY)` (**10 min**; Pressure Cooker is the one exception, at
  **5 min**) with no nickname consequences; `RESOLVED` games where the winner never named the
  loser become `NO_NICK_SET` after `NAMING_WINDOW_SECONDS` (**30 min**, reason
  `winner_timeout`); idle lobbies become `EXPIRED_LOBBY` after `LOBBY_IDLE_SECONDS`
  (**5 min**).
- **Two one-shot pings** ride the same loop: the lobby host is warned `LOBBY_WARNING_SECONDS`
  before a lobby closes (`_warn_stale_lobbies`), and an unnamed result's winner is reminded at
  `NAMING_REMINDER_SECONDS` (`_remind_unnamed`). Both are recorded on the row
  (`lobby_warned_at` / `nick_reminded_at`), so a restart never repeats them.
- **A member leaving the server** (`BaseGame.on_member_remove`, duels-party-127): refunded and
  dropped from any lobby of that game (a leaving host closes it, refunding everyone); dropped
  from `alive` in a live group round with a public "left the server" call-out, the game
  resolving to the last player standing when one remains (a nickname game then concludes
  `loser_left`, since there is nobody to rename; an empty `alive` — Chicken, where bailers
  already left it — is left to the game's own timer); a duel they were in is `VOID`ed with a
  "Game Called Off" card and every stake refunded; a pending challenge they were party to
  expires; an unnamed result they won or lost concludes (`winner_left` / `loser_left`). A
  game whose round state pointed at the leaver re-aims through `on_player_left` — Hot Potato
  (Group) hands the bomb to the next player in the circle, fuse still burning. The
  economy cog already refunded a leaver's escrow; what nobody did was take them out of the
  game, so a lobby or round that included them stalled until the abandonment sweep.
- **In-game timers** — hidden fuses (Hot Potato), draw delays (Quickdraw), the meter climb +
  crash (Chicken), and music/scramble windows (Musical Chairs) are per-game `asyncio` tasks
  keyed to the game id; cancelled/rescheduled on the relevant interactions.
- **Restart recovery** — `BaseGame.cog_load` reloads active games (re-attach the game View,
  call `on_game_resume` to re-arm timers), settled games in any of the four settled states
  (`fetch_resolved_games` returns `RESOLVED` / `RESOLVED_NO_NICK` / `NICKED` / `NO_NICK_SET`;
  the result View is re-attached with `📝 Name the Loser` and/or `🔁 Run It Back` as the
  state and the rematch window allow, and skipped once it would carry no button), open
  lobbies (re-attach the lobby View), and — duels only — pending challenges still
  inside their response window (a persistent `ChallengeView` with the card's original
  deadline; see §3). Rows whose message is gone, and challenges already past their deadline,
  are left to the sweep.

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
| `challenge <user> [stakes] [wager]` | Anyone | Challenge a target (accept/decline). |

**Group games** (`/games hotpotatogroup`, `/games chicken`, `/games musicalchairs`)

| Command | Who | Effect |
|---|---|---|
| `start [stakes] [wager]` | Anyone | Open a join lobby. |

**`cancel`/`stats`/`revert`/`config` — all removed, none were ever reachable.** None of
these subcommands (plus `config`, see below) exist in any of the six cogs today — the command
methods and their now-orphaned db-layer stats/revert-shim functions were deleted outright
(`bot.tree.remove_command(...)` in each cog's `setup()` only removes the auto-registered
top-level game-name command so the group can be re-parented under `/games`; it isn't how the
dead subcommands went away). So none of them were ever actually callable in Discord — a
pending challenge could only be cancelled via timeout (5 minutes) or the lobby's `🚫` button,
W/L stats and Hot Potato's style points had no way to be viewed, and the nickname "early
revert" toggle (`allow_early_revert`) had no command to exercise it, on any game, ever. They
weren't needed for these short-lived games. `allow_early_revert` doesn't even exist as a
column any more — migration `194` dropped it (see §11); `nick_denylist` was always enforced
and is now settable from each game's dashboard panel. A pending challenge still self-expires
after 5 minutes (see §7's stale-game reaper) so dropping `cancel` has no user-facing gap.

**Per-game config — web dashboard only.** Settings (cooldowns, sentence duration,
channel allowlist, nickname/stakes length caps, plus each game's own mechanics knobs) live on
the web dashboard's **Games** nav section, one "Config" panel per game
(`config-games-<slug>.js`, using the same no-separator slug as the `/games <slug>` command —
`config-games-hotpotato.js`, `config-games-musicalchairs.js`, etc.). The `PUT` endpoint
underneath is `/api/config/games-<slug>` only for the four single-word games
(`games-pressure`, `games-quickdraw`, `games-chicken`); Hot Potato (duel/group) and Musical
Chairs hit hyphenated multi-word paths instead — `PUT /api/config/games-hot-potato`,
`/api/config/games-hot-potato-group`, `/api/config/games-musical-chairs`:

| Game | Panel fields |
|---|---|
| Pressure Cooker | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `challenge_limit_per_hour` |
| Quickdraw | same shared fields, plus `min_delay`, `max_delay`, `draw_window` |
| Hot Potato (duel) | same shared fields, plus `min_timer`, `max_timer`, `min_hold` |
| Hot Potato (group) | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `challenge_limit_per_hour`, `min_fuse`, `max_fuse`, `min_hold`, `min_players`, `max_players` |
| Chicken | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `challenge_limit_per_hour`, `min_climb`, `max_climb`, `min_players`, `max_players` |
| Musical Chairs | `cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`, `challenge_limit_per_hour`, `min_music`, `max_music`, `scramble_window`, `false_start_elim`, `min_players`, `max_players` |

`channel_allowlist`/`max_nick_length`/`max_stakes_length`/`challenge_limit_per_hour` are exposed
for all six games, not
just Pressure Cooker as the old (dead) commands had it — they were always enforced
generically in the shared base classes, so this closes a real gap rather than adding scope.

**`channel_allowlist` is the *only* channel gate these games have.** The party-game
allowlist (`games_allowed_channels`, set on Games → Global Config) does not apply:
no duel or lobby code path calls `check_allowed_channel`, so an empty per-game
`channel_allowlist` — which is what every guild has, `duel_config` being empty in
production — means the game runs in **every** channel on the server, allowlisted for
party games or not. Until 2026-08-30 all six panels said otherwise, both in a field
hint ("every channel that may host party games") and in a banner that claimed the
game "cannot be played anywhere" while the global list was empty. Both were wrong and
were rewritten; the banner was deleted outright. Wiring these games to the global list
instead is a live option, but it is a behaviour change — duels playable anywhere today
would stop working outside the allowlisted channels — so it is a decision, not a fix.

**In-embed controls** (built): `✅ Accept` / `❌ Decline` (duel challenge); `✋ Join` /
`🚪 Leave` / `▶️ Start` / `🚫 Cancel` (lobby); the game's own button(s) (`💨 PUMP`,
`🔫 FIRE`, `🤲 Pass`, `🐔 BAIL`, `🪑 SIT`); `📝 Name the Loser` (winner-only, on
nickname-mode results); `🔁 Run It Back` (every result, either duelist or the lobby host,
5-minute life — see §3). There is no "How to Play" or "I'll honor this" control (those are
roadmap — see §13).

**Copy that names a duration** comes through `BaseGame`'s helpers (`_span`, `_hours_span`,
`nick_forfeit_copy`, `nick_applied_copy`, `nick_self_apply_copy`, `awaiting_nick_copy`,
`_cooldown_copy`, `_dead_lobby_copy`) so the result cards, the honour-system announcements,
the lobby and challenge fallbacks and the abandonment card all read `sentence_hours` and the
sweep constants rather than a typed "24 hours" / "5 minutes" (duels-party-125). The persisted
`🏷️` stakes line is built from the dial at creation (`filters.nick_stakes_line`), so a game
made under a 48-hour dial says 48 on every card; the `nickname:` slash descriptions no longer
state a number at all (a description is one string per command, not per guild).
`tests/test_duels_copy.py` sweeps the six cogs and the shared modules for the literals.

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
`cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`, `max_stakes_length`,
`challenge_limit_per_hour`.

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
2. Holder presses **🤲 Pass** → the bomb alternates to the other player, once the holder
   has held it for **`min_hold`** (default **2.0s**, the group cog's anti-ping-pong wait,
   adopted 2026-09-04 — without it the duel was a click race decided by a random tick;
   `game.hold_remaining`). An early press is refused ephemerally ("Hold it a moment…").
   The fuse keeps burning.
3. Fuse expires → current holder loses.

**Style points:** time spent holding in the "danger zone" (last 30% of the fuse) earns
cosmetic **style points** (`compute_style_points`), accumulated in `hot_potato_style`. The
result card shows each player's points from this game as the tie flavour and quotes their
running total on the same line ("**X**: +30 pts — now has 174 style points",
`db.get_style_total`). That line is the only reader of the cumulative table.

**Server-authoritative:** the fuse is a hidden scheduled task; passing is locked to the holder
and to the `min_hold` wait; detonation cancels/reschedules on resolution.

**Config knobs:** `min_timer` (10.0), `max_timer` (45.0), `min_hold` (2.0) + shared.

### 9.4 Hot Potato (group)

**Hidden fuse, progressive elimination · 2..N players · lobby · final loser renamed**

The group version re-lights a fresh fuse each round and eliminates the holder at detonation
until one remains. Its display name is **"Hot Potato (Group)"** everywhere it renders —
`GAME_DISPLAY_NAME`, embed titles, the restart notice, the command descriptions, the
`/help` picker and `GAME_NAMES` — because the duel is "Hot Potato" and a refusal, lobby ping
or audit-log reason naming the wrong one was unreadable (duels-party-120). Folding the two
into one command is on the roadmap (§13.4).

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

Everyone is **holding** from the start. A **visible** shared meter climbs toward a **hidden**
crash point. Press **🐔 BAIL** to drop out safely. If the meter blows with players still
holding → **CRASH**.

**States:** `LOBBY → ACTIVE (CLIMBING) → RESOLVED / RESOLVED_NO_NICK`.

**Flow**
1. Lobby (`min_players`/`max_players`, default **2/8**). On start, `CLIMBING`: the crash point
   `crash_at` is rolled uniformly in **[`min_climb`, `max_climb`]** (default **10.0–25.0s**,
   `game.roll_crash_at`, seeded and the seed logged) and stored on the row but never shown.
   The meter is drawn over `max_climb` (`chicken_games.climb_duration`), ticking every ~2s,
   so the bar can blow at 40% and nobody can count it out. Until 2026-09-04 the crash was a
   fixed, public `climb_duration` and the stake fired in 4 prod games of 12
   (duels-party-113).
2. Each player has one **🐔 BAIL** (there is no separate "commit" press — you're committed by
   default and bail once). The bail's meter % is recorded.
3. **Crash** with players still holding → resolve; the result card names the meter reading
   at the blow ("💥 Crash at 60%!"). If everyone bails before the crash, the last to bail wins
   (cosmetic, no rename).

**Loser determination (`resolve_crash`):**
- Crashers + at least one bailer → **winner** = bravest bailer (highest meter % at bail),
  **loser** = **one crasher drawn at random** (`random.Random(seed).choice`, seed logged so
  a disputed draw can be replayed); the card says the loser was "drawn at random from
  everyone who crashed" when more than one crashed. It was the lowest user id until
  2026-09-04, which made the oldest account at the table the permanent scapegoat
  (duels-party-123).
- **Nobody bailed** (total wipeout) → cosmetic, no winner, **no rename**; the card says so
  plainly — "everyone crashed at N%… the pot is refunded" — and the terminal seam refunds any
  wager (a `winner_id` of `None` never pays anybody).

(Only one crasher is renamed — there is no "everyone still holding loses" multi-target stake in
the current build.)

**Server-authoritative:** the meter climb is a server-side scheduled progression; the crash is
a scheduled task at `start + crash_at` (a row from before migration 208 has no `crash_at` and
still crashes at `start + climb_duration`); bail order is authoritative under the per-game
lock.

**Config knobs:** `min_climb` (10.0), `max_climb` (25.0), `min_players` (2), `max_players` (8)
+ shared. A `max_climb` below `min_climb` is lifted to it rather than inverting the range.

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

**A round nobody sat in** (`game.no_sitter_verdict`, 2026-09-04): the round **re-runs** —
same players, same chairs, no elimination, fresh music, "🪑 Nobody sat! … the music starts
again…" — and `mc_games.reruns` counts it. A second no-sitter round in a row (any round with
a sitter resets the count) **voids** the game: state `VOID`, "🏳️ Game Called Off" on the
panel, every stake refunded by the terminal seam, nobody renamed. Resolving instead used to
make the later-listed player both winner and runner-up and pay them the pot for a chair they
never sat in (duels-party-114).

**Server-authoritative:** SIT is always clickable (the false-start trap); the first `chairs`
valid scramble presses seat; one press per player per round; a lock guards the last-chair race.

**Panel placement:** the game panel is edited in place during `MUSIC`, but every
`MUSIC → SCRAMBLE` flip **re-posts it** (`_repost_panel`: send the new one, record its
id, delete the old) so the SIT button is at the bottom of the channel at the moment
it goes hot. Post-before-delete — a send failure keeps the existing panel and falls
back to an in-place edit.

**Elimination call-outs:** every exit is announced publicly with its reason — timeout
("didn't find a chair", including the final round) and false start ("sat before the
music stopped", via `BaseGame._group_eliminate(reason=…)`). Nothing is ephemeral-only.

**Rules discoverability:** `HOW_TO_PLAY` renders as a "📖 How to play" field on the
lobby embed (a `BaseGame` hook any group game can set), and both round embeds spell
out the wait-then-press rule.

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
| Chicken | `chicken` | mutual nerve | 2..N | `BaseGame` | single crasher (random, seed logged) |
| Musical Chairs | `musicalchairs` | reflex + attrition | 3..N | `BaseGame` | runner-up (last eliminated) |

---

## 11. Config reference (defaults)

**Shared `duel_config`** (all games, via `duels/db.py._CONFIG_DEFAULTS`)
- `cooldown_hours` 0 (enforced on all six games since 2026-09-04, nickname games only; the
  column's own SQL DEFAULT is still 48, so both upsert paths seed a fresh row explicitly) ·
  `sentence_hours` 24
- `channel_allowlist` `[]` · `max_nick_length` 32 · `max_stakes_length` 200
- `challenge_limit_per_hour` 30 (0 = no limit)
- `nick_denylist` `[]` — extra banned words, enforced on every nickname and every line of
  stakes text, set from each game's dashboard panel ("Extra Banned Words")
- `allow_early_revert` was a real column, but nothing ever read or wrote it (the games that
  carried a `revert` command never had it wired into the live tree — see §8). Migration `194`
  (`194_drop_dead_game_config_columns.sql`, 2026-08-30) dropped it outright, along with six
  sibling dead columns the same audit found: `quickdraw_config.void_on_double_noshow` (a draw
  nobody answers is always voided), `hp_group_config.shake_threshold` / `pass_mode` (the shake
  threshold is fixed in `game.shake_emoji`; passing is always clockwise), and `lobby_timeout`
  on all three group tables — `hp_group_config`, `chicken_config`, `mc_config` (the stale-lobby
  window is the shared `LOBBY_IDLE_SECONDS` constant, 5 minutes since 2026-09-04). None
  of these seven columns exist in the schema any more, so none of them are in
  `_CONFIG_DEFAULTS` either

**Timing constants** (`duels/db.py`, not dials): `CHALLENGE_RESPONSE_SECONDS` 300 ·
`LOBBY_IDLE_SECONDS` 300 · `LOBBY_WARNING_SECONDS` 60 · `NAMING_WINDOW_SECONDS` 1800 ·
`NAMING_REMINDER_SECONDS` 120 · `REMATCH_WINDOW_SECONDS` 300 · `active_idle_seconds()` 600
(Pressure Cooker 300). Migration 207 added `nick_reason` and `nick_reminded_at` to all six
game tables and `lobby_warned_at` to the three group tables; `GAME_TABLES` maps `GAME_KEY` to
its table so `BaseGame` can run the one-shot-ping queries generically.

**Rate limit:** `challenge_limit_per_hour` challenges/starts per user per hour, per game
(in-memory sliding window, per-guild dial on each game's dashboard Config panel).

**Per-game knobs:** see each §9 entry's "Config knobs" line.

**Enable switch:** each game's panel carries "Available on This Server", stored as a
`games_game_config` row under the cog's `GAME_KEY` — the same store and the same
`check_game_enabled` gate the question-bank games use. No row means enabled. It is checked at
both creation entrypoints (`_base_challenge`, `_base_lobby`). The global
`games_allowed_channels` list is **not** consulted by any duel or lobby path: a game's own
`channel_allowlist` is its only channel rule, and empty means everywhere.

---

## 12. Implementation status

All six games in §9 are built and live under `src/bot_modules/cogs/`, on the shared
`BaseGame`/`BaseDuel` foundation in `src/bot_modules/duels/`. Economy rewards (participation
for everyone, a winner bonus) are paid centrally by `BaseGame._on_terminal_state` when a game
reaches `RESOLVED`/`RESOLVED_NO_NICK` — no cog calls `pay_game_rewards` itself
(economy-sinks round 2, stage 4a).

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

The current build renames a single loser (duel loser, group's last-eliminated, or one of
Chicken's crashers drawn at random). The designed multi-target model — selectable `stake_target` of
`loser` / `first_eliminated` / `last_eliminated` / `all_eliminated`, with non-targeted players
getting cosmetic standings only — is not implemented.

### 13.4 Designed-but-unbuilt UI niceties

- **`❓ How to Play`** — an ephemeral rules button on each game.
- ~~**`🔁 Run Again` / rematch**~~ — built 2026-09-04 as `🔁 Run It Back` (§3).
- **`🫡 I'll honor this`** — a cosmetic accountability button on custom-stakes results.
- **Hot Potato (group) `choose` pass mode** — a player-select for who receives the bomb (today
  it only passes clockwise).
- **Fold the two Hot Potatoes into one command** — keep a single `/games hotpotato` with a
  lobby of 2..N (the challenge form pre-fills a 2-player lobby and pings the target), migrate
  `hot_potato_config` into `hp_group_config`, retire the group cog and its panel (the route id
  can stay as an alias). The group cog has never completed a game in prod (three lobbies, all
  expired) and its duel twin has the same fuse, hold and style mechanics. Until then the two
  are told apart by name only ("Hot Potato" / "Hot Potato (Group)", duels-party-120); folding
  is Billy's call.

### 13.5 Parked / future work

- **Reputation / honor tracker** — cross-game character scores and titles; `elimination_order`
  already gives placement-based signal.
- **Economy layer (payouts by placement)** — games already pay participation/win
  rewards through the terminal-state seam, and wagering is **built** (economy-sinks
  round 2, stage 4b: `econ_game_wagers` escrow, migration 094, optional `wager:` on
  all six games, settled/refunded via the seam's `_on_terminal_state` hook).
  Placement payouts remain parked.
- **Per-game leaderboards & session recap** integration with the Poppy/DK session tracker.
- **More games (cheap once `BaseGame` exists):** Russian Roulette, Higher/Lower, Tug of War,
  Odds Are, Last One Standing, Wheel of Fate, Werewolf/Mafia-lite (bigger build).
