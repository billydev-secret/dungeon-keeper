# Risky Rolls — Feature Spec

A channel-scoped dice game. Anyone in the channel presses **Roll** to roll 1–100; highest unique roll asks a question, lowest answers. Ties for the top auto-reroll until one player wins. Special rolls trigger variants: a **69** lets the winner ask the whole room in a thread, a **100** lets the winner pick the bottom two players, and a **1** triggers a two-questioner mode where the top two each fire a question at the loser. Persistent state — an in-progress round survives a bot restart.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/risky start` | Slash | Everyone (server only) | Open a new round; pings the configured role (if set) and applies the min-game-time floor |
| `/risky start_no_ping` | Slash | Everyone (server only) | Same as `/risky start` but skips the ping and the min-game-time wait |
| `/risky reset_state` | Slash | Administrator | Wipe every active round, pending question, and posted question in **this channel** |
| **Roll** button | Persistent | Round participant | Roll 1–100 once (rerolls in tie state are restricted to tied players) |
| **How to Play** button | Persistent | Everyone | Show the rules in an ephemeral message |
| **Close Round** button | Persistent | Round opener or admin | Resolve the round (blocked until min-game-time elapses unless start_no_ping was used) |
| **Ask Question** button | Persistent | Eligible questioner | Open the question modal |
| **Reply** button | Persistent | Allowed replier | Open the reply modal; first valid reply locks the question |
| Risky panel | Web (dashboard) | Admin | Configure the ping role and the min-game-time floor |

## Behavior

### Starting a round

`/risky start` opens a new round. The bot checks Send Messages + Embed Links in the channel, refuses if the channel already has 10 active games, then posts the round embed with the **Roll / How to Play / Close Round** buttons. If a ping role is configured, the bot also posts a one-line ping ("A new Risky Rolls round has begun!"). `/risky start_no_ping` is the same except it skips the role ping and bypasses the min-game-time floor.

An auto-close is scheduled at start: by default the round auto-closes 120 minutes after start, or sooner once 25 distinct players have rolled (whichever comes first, never before the min-game-time floor).

### Rolling

Pressing **Roll** rolls 1–100 once per player. The roll is appended to the round embed with a decoration (🔥 for 69, ⭐/🥇 for current winner, 💀/☠️ for current loser, 🎲 otherwise). A player can't roll twice; in a reroll state, only tied players can roll and only once each.

### Closing and resolving

**Close Round** (opener or admin) checks two things: that the min-game-time has elapsed, and that at least two players have rolled. If a tie for the top is detected, the bot runs a hidden re-roll-off among the tied players (recursively if the re-roll also ties) until a single winner emerges; same for the bottom if needed.

Special-roll outcomes:
- **Anyone rolled 69** — that roller wins; the prompt becomes a "room" question that asks every participant. The bot creates a thread off the prompt message (`auto_archive_duration = 1440`) for the conversation, falling back to a channel followup if thread creation fails.
- **Winner rolled 100** — the winner picks both the lowest and second-lowest players as recipients of their question.
- **Loser rolled 1** — a "two questioners" sub-game spawns: both the top and second-top each get to ask the loser one question.

After resolution, the **Roll / Close** view is disabled and replaced with an **Ask Question** prompt aimed at the eligible questioner(s).

### Asking and replying

**Ask Question** opens a 300-character modal. On submit, the bot posts the question (in a thread for room/69 questions, in the channel for direct questions) with a **Reply** button. **Reply** opens a 300-character reply modal; the first valid reply edits the original question message in place to embed the reply text, and closes the reply window.

### Cooldown / minimum game time

A configurable min-game-time floor (default 30 minutes) prevents premature closes. `/risky start_no_ping` bypasses it.

### Persistence and restarts

Active rounds, pending questions, and posted questions are all stored in SQLite. On bot restart the cog re-attaches all persistent views to the original messages, re-schedules auto-close timers from the remaining elapsed time, and sweeps posted questions older than 7 days.

## Permissions

- **User-side**:
  - `/risky start`, `/risky start_no_ping`: everyone, server only.
  - `/risky reset_state`: Administrator.
  - Buttons gate themselves at click time (opener-or-admin for Close; eligible-questioner for Ask; allowed-replier for Reply).
- **Web**: admin only.
- **Bot-side**: **Send Messages**, **Embed Links**, plus **Create Threads** + **Send Messages in Threads** for the 69-rule path.

## User-visible errors

| When | The user sees |
|---|---|
| `/risky start` in a DM | "This command can only be used in a server channel." |
| `/risky start` missing Send Messages / Embed Links | The explicit missing-perm list |
| `/risky start` with 10 active games in channel | "This channel already has 10 active games. Close one before starting another." |
| `/risky start` fails after setup | "Risky Rolls could not finish setup. Start a new round." |
| `/risky reset_state` with nothing to wipe | "No active or pending Risky Rolls state was found in this channel." |
| Non-admin `/risky reset_state` | "You do not have permission to use that command." |
| **Roll** with no open round | "No open round to roll in." |
| **Roll** when already rolled | "You already rolled this round." |
| **Roll** when not eligible to reroll | "You cannot reroll right now." |
| Non-opener / non-admin presses **Close Round** | "Only the round opener can close this round." |
| **Close Round** before min-game-time elapsed | "This round cannot be closed yet. Please wait N more second(s)." |
| **Close Round** while waiting on rerolls | "Still waiting for {mentions} to reroll." |
| **Close Round** edit fails | "Round closed, but the message could not be updated. Start a new round." |
| **Ask Question** with no pending question | "There is no pending winner question for this round." |
| **Ask Question** from non-questioner | "Only the eligible players can send a question." |
| **Ask Question** when already asked | "You already asked your question." |
| Empty question | "Enter a question before sending it." |
| **Reply** when window has closed | "This reply window has closed." or "Someone already replied to this question." |
| **Reply** from non-recipient | "Only the question's recipient can reply." |
| **Reply** when question message was deleted | "The question message no longer exists." |
| Dashboard sends negative min-game-seconds | HTTP 400 |

## Economy integration

Pressing **Roll** fires the `risky_roll` economy quest trigger (once per member
per round, keyed on the game id — `bot_modules/services/risky_roll/views.py:337-341`,
via `fire_member_trigger`). The roll itself is the qualifying act, so it fires at
roll time, not round close. Best-effort: an economy failure never blocks the roll.

## Non-goals

- **No leaderboards.** Wins / losses aren't aggregated; closed rounds delete their state.
- **No DM mode.** Server-only.
- **No multi-channel rounds.** A round lives in one channel; the 10-active-games cap is per channel.
- **No editing / cancelling an already-asked question.** Once submitted, the question is locked.
- **No multi-reply chains.** First valid reply finalises the question.
- **No spectator participation.** Only members who clicked Roll appear in the round.
- **No XP.** Round outcomes don't feed [[xp-spec]]; the economy quest trigger above fires on Roll instead.

## Configuration

| Key | Default | Purpose |
|---|---|---|
| Ping role | unset | Optional role to ping on `/risky start` (not on start_no_ping). Setting it to "no role" clears the row |
| Min game seconds | 1800 (30 min) | Floor on round duration; blocks early **Close Round**. `/risky start_no_ping` bypasses |

Per-round only (not persisted as config):
- **Auto-close after N players** — default 25 (must be ≥ 2).
- **Auto-close after N minutes** — default 120 (must be > 0).

## Stored data

Four per-guild tables:

- **Active rounds** — one row per open game: opener, message id, rolls map (deserialised), reroll state, auto-close settings, special-roll outcomes. Deleted on close.
- **Pending questions** — between resolution and the question being asked. Includes the "two questioners" sub-game when the loser rolled 1.
- **Posted questions** — a question that's been sent and is awaiting a reply. Keyed by the question message id. Auto-swept on bot startup once older than 7 days.
- Two per-guild rows in the shared config table for the ping role and the min-game-time floor.

No DM data. No filesystem cache. In-flight rounds, prompts, and questions persist across restarts; the cog rebuilds in-memory state and re-attaches persistent views on next boot.
