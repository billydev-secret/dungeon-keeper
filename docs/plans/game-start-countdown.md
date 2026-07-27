# Game start countdown + host nudge

**Status: built 2026-07-27** (all four stages). Covers todos **#79**
(countdown-to-start on all games, ping the host when it's time) and **#85**
(tell the host it's time to start a game that was set with a start time).
They're one feature — one timer, one nudge — so they ship together.

## What existed before this

- **Clapback alone** has a countdown. `/games play clapback start_in:<1–60>`
  stamps `config["start_epoch"]`, `build_lobby_embed` renders it as a
  `<t:epoch:R>` "⏰ Starting" field (`games_clapback/embeds.py:63`), and
  `ClapbackJoinView` stretches its 600s inactivity timeout past the advertised
  start (`games_clapback_cog.py:137-141`). The host still clicks **Start** —
  the countdown is advertising, not automation.
- **Nothing pings anybody** when the advertised time arrives. The countdown
  just goes red in Discord's client and the lobby sits there.
- The **dashboard scheduler** (`scheduled_games_service.py`) auto-launches a
  game at a set time. For a lobby game it posts the lobby and walks away —
  nobody is told that a human still has to press Start. `_process_due` already
  resolves `row["created_by"]` into `host_id`/`host_name` (service.py:312), so
  the person to nudge is already in hand.

## Decisions (Ben, 2026-07-27)

1. **Channel ping, no DM.** #85 asked for a DM; the call is a message in the
   game's channel pinging the host. Joiners see the nudge too, and it lands
   directly under the lobby card where the button is.
2. **Countdown on lobby games only** — the 6 games that have a join lobby with
   a host-pressed start button: `clapback`, `compliment`, `mfk`, `mlt`,
   `rushmore`, `story`. The other 12 party games post their first prompt the
   instant the command runs; there is no Start button for a countdown to point
   at, and inventing a delayed-launch teaser for them was rejected.
3. **The nudge fires in both flows, still lobby-only:**
   - manual `/games play <game> start_in:N` → ping at T-0;
   - dashboard-scheduled **lobby** game → ping the schedule's creator when the
     lobby lands;
   - scheduled lobby-less game → nothing (it self-runs; there's no action to
     nudge).

## Design

### Where the timer lives

A single **poll loop**, not a per-lobby `asyncio` task. `games_active_games`
holds only live games (a handful of rows), so a 15s poll over
`WHERE state='joining'` is cheap, and — the real reason — it is **restart-safe
for free**. A per-lobby task dies with the process and would need re-arming in
all six recoverers; the loop just finds the row again after a restart and pings
late rather than never.

Cost: the ping lands up to 15s after the advertised second. For "it's time to
start" that's fine.

### State

Two keys, top-level in the game's `payload` (each game's payload shape differs,
so top-level is the only common ground):

- `start_epoch` — UTC epoch to nudge at. Absent ⇒ no countdown, no ping.
- `start_ping_sent` — set once the nudge goes out, so a slow poll can't
  double-ping.

Clapback keeps its existing `config["start_epoch"]` (the view timeout and the
embed both read it); the service's accessor falls back to it rather than
duplicating the value into two places that can drift.

### New service

`src/bot_modules/services/game_start_ping_service.py`

| Function | Kind | Job |
|---|---|---|
| `extract_start_epoch(payload)` | pure | top-level `start_epoch`, else `config.start_epoch`, else `None` |
| `start_ping_due(payload, now)` | pure | epoch present, `now >= epoch`, not already sent |
| `build_start_ping(game_type, host_id)` | pure | the nudge copy, incl. the game's real button label |
| `send_start_ping(channel, game_type, host_id)` | I/O | sends it, allow-listing **only** the host |
| `game_start_ping_loop(bot)` | I/O | 15s poll; registered in `__main__.py` beside `scheduled_games_loop` |

Copy, per the style guide (mention allow-listed to exactly the host, never the
raw text):

> ⏰ <@host> — time to start **Mt. Rushmore Draft**! Hit **Start Draft** when
> everyone's in.

Button labels differ per game (`Start`, `Start Draft`, `Start Story`,
`Close & Generate`, `Close & Assign`), so `constants.py` gains
`LOBBY_GAME_TYPES` and a label map; the nudge names the button the host is
actually looking at.

### Scheduled games

`_process_due` pings **inline after a successful launch** rather than routing
through the loop — the lobby just landed, so a ≤15s lag would be pointless
latency. Ordering is deliberate: the existing optional `announce` line ("🎮
**X** is starting now!") fires *before* launch, then the lobby card, then the
host nudge — so the actionable message sits adjacent to the button.

No new schedule field and no dashboard toggle: the nudge is one mention of one
person, fired only when that person's action is genuinely required.

### Why `start_in` stays a slash param

CLAUDE.md puts *admin/server config* on the dashboard. `start_in` is neither —
it's a per-game choice the host makes at the moment they open a lobby, i.e.
member self-service, which belongs in Discord. It also matches the precedent
clapback already set.

## Stages

**1 — Constants + service.** `LOBBY_GAME_TYPES` + button-label map in
`games/constants.py`; new `game_start_ping_service.py`; loop registered in
`src/dungeonkeeper/__main__.py`. Tests: `tests/test_game_start_ping_service.py`
— due/not-due, missing epoch, already-sent, clapback's nested-config fallback,
copy per game, and the allow-list shape.

**2 — Countdown on the five remaining lobby games.** `start_at: int | None`
param + "⏰ Starting" field on `build_lobby_embed`/`build_join_embed` in
`games_compliment`, `games_mfk`, `games_mlt`, `games_rushmore`, `games_story`
(mirroring clapback's `embeds.py:63-69`); `start_in` slash param (Range 1–60)
on each cog, plumbed through `launch(options=...)` into `payload["start_epoch"]`
so the scheduler path stays identical. Clapback gains nothing but the
top-level mirror it doesn't need. Tests: an embed row per game in each existing
`tests/test_games_<game>_logic.py` (field present with epoch, absent without).

**3 — Scheduler nudge.** `_process_due` sends the nudge after a launch that
returned a game id, for lobby game types only. Tests: extend
`tests/cogs/test_scheduled_games_loop.py` — lobby game pings, lobby-less game
does not, failed launch does not.

**4 — Docs.** `docs/games_system_spec.md` (countdown + nudge behavior),
`src/web_server/static/manual.html` (the `/games play` section documents
`start_in` for the six games — it documents none of it today), and this plan's
status header. `docs/INDEX.md` needs a row for this plan; no classification
changes.

## Edge cases

- **Host never set `start_in`** ⇒ no `start_epoch` ⇒ no ping. The nudge is
  strictly opt-in on the manual path.
- **Game started early**, before T-0: the row leaves `state='joining'`, the
  loop's `WHERE` stops matching, no stale "time to start" for a running game.
- **Lobby cancelled / timed out**: row is gone or no longer `joining`; same.
- **Bot restarted across T-0**: the loop finds the row on the next poll and
  pings late. Late beats never for a lobby that is still open.
- **Channel unreachable / no send perms**: log and mark sent, don't retry every
  15s for the life of the lobby.
- **Clapback's lobby timeout** already stretches past `start_epoch`; the other
  five use persistent (`timeout=None`) views, so there's nothing to extend.

## Follow-up: three more lobby games

A post-ship review found the "six lobby games" premise incomplete. **Two Truths
& a Lie** (`Start Guessing`), **Hot Takes** (`Start Voting`) and **LegitLibs**
(`Start`) also open and wait on a host press, so they'd benefit from both the
countdown and the nudge — a *scheduled* TTL or Hot Takes lobby still sits with
nobody told a press is pending, which is exactly what the scheduler nudge was
added to fix. They were left out because the scope Ben approved was framed
around a six-game list. Adding them is mechanical: a `LOBBY_GAME_TYPES` entry, a
`LOBBY_START_BUTTON` label, `start_in` on the cog, `start_at` on the lobby
embed, and — for TTL and Hot Takes, which never leave `state='joining'` — the
same start-handler state transition clapback/mlt/story needed. Ben's call.
