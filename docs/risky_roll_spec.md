# Risky Rolls — Feature Spec

A channel-scoped dice game. Anyone in the channel presses **Roll** to roll 1–100; highest unique roll asks a question, lowest answers. Ties for the top auto-reroll until one player wins. Special rolls trigger variants: a **69** lets the winner ask the whole room in a thread, a **100** lets the winner pick the bottom two players, and a **1** triggers a two-questioner mode where the top two each fire a question at the loser. Persistent state — an in-progress round survives a bot restart.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/risky start` | Slash | Everyone (server only) | Open a new round; pings the configured role (if set) and applies the min-game-time floor |
| `/risky reset_state` | Slash | Administrator | Wipe every active round, pending question, and posted question in **this channel** |
| **Roll** button | Persistent | Round participant | Roll 1–100 once |
| **How to Play** button | Persistent | Everyone | Show the rules in an ephemeral message |
| **Close Round** button | Persistent | Round opener or admin | Resolve the round (blocked until min-game-time elapses unless the round was opened with `ping:false`) |
| **Ask Question** button | Persistent | Eligible questioner | Open the question modal |
| **Reply** button | Persistent | Allowed replier | Open the reply modal; first valid reply locks the question |
| Risky panel | Web (dashboard) | Admin | Configure the ping role and the min-game-time floor |

## Behavior

### Starting a round

`/risky start` opens a new round. The bot checks Send Messages + Embed Links in the channel, refuses if the channel already has 10 active games, then posts the round embed with the **Roll / How to Play / Close Round** buttons. If a ping role is configured, the bot also posts a one-line ping ("A new Risky Rolls round has begun!") — allow-listing exactly that role rather than a blanket `roles=True`. A guild that has **never** set the dial gets a `@Risky Rolls` role created on the first pinged round (`core/role_provision.py`); an admin who picked "(none)" keeps silent rounds. Passing `ping:false` skips the role ping and bypasses the min-game-time floor — the two move together, since the floor exists to give pinged members time to arrive. (This replaced the separate `/risky start_no_ping` command on 2026-07-28.)

An auto-close is scheduled at start: by default the round auto-closes 120 minutes after start, or sooner once 25 distinct players have rolled (whichever comes first, never before the min-game-time floor).

### Rolling

Pressing **Roll** rolls 1–100 once per player. The roll is appended to the round embed with a decoration (🔥 for 69, ⭐/🥇 for current winner, 💀/☠️ for current loser, 🎲 otherwise). A player can't roll twice.

### Closing and resolving

**Close Round** (opener or admin) checks two things: that the min-game-time has elapsed, and that at least two players have rolled. If a tie for the top is detected, the bot runs a hidden re-roll-off among the tied players (recursively if the re-roll also ties) until a single winner emerges; same for the bottom if needed.

Special-roll outcomes:
- **Anyone rolled 69** — that roller wins; the prompt becomes a "room" question that asks every participant. The bot creates a thread off the prompt message (`auto_archive_duration = 1440`) for the conversation, falling back to a channel followup if thread creation fails.
- **Winner rolled 100** — the winner picks both the lowest and second-lowest players as recipients of their question.
- **Loser rolled 1** — a "two questioners" sub-game spawns: both the top and second-top each get to ask the loser one question.

After resolution, the **Roll / Close** view is disabled and replaced with an **Ask Question** prompt aimed at the eligible questioner(s).

### Asking and replying

**Ask Question** opens a 300-character modal. On submit, the bot posts the question (in a thread for room/69 questions, in the channel for direct questions) with a **Reply** button. **Reply** opens a 300-character reply modal; the first valid reply edits the original question message in place to embed the reply text, and closes the reply window.

Both the question **and** the reply are public free text, so both are screened against the shared slur/abuse denylist (`duels/filters.contains_disallowed_content`) — a match is rejected with an ephemeral "contains disallowed content" and nothing is posted.

### No-contact enforcement

Risky Rolls consults the [no-contact list](no_contact_spec.md) on **every
draw**, not at resolution. A roll value that would seat a no-contact pair as
asker and answerer — including the extra seats the 100 and 1 rules create — is
redrawn before it exists, so the pairing never forms and there is nothing to
refuse. The draw is honest first and redrawn only on a collision, which leaves
the natural distribution alone except where it has to change; 69 is excluded
from the redraw pool specifically so it is never manufactured as an escape.

When no value can avoid it (a round that is only those two players), **Close
Round returns the ordinary "At least 2 players must roll."** and the round
stays open; the auto-close path ends it with the ordinary "not enough players
rolled". Both strings are module constants shared with the genuine
too-few-players path — see `views.NOT_ENOUGH_TEXT` /
`views.AUTO_CLOSE_NOT_ENOUGH_TEXT`. The cost: a large round can occasionally
die because two of its players landed in those seats.

A **69 room question is not directed contact** — it posts to the thread
intact, and the partner is only dropped from its `@`-mention list. The full
reasoning is in `no_contact_spec.md` §"Risky Rolls: the dice are nudged, not
the outcome".

### Cooldown / minimum game time

A configurable min-game-time floor prevents premature closes. **It is unset by default,
which means no floor** — the dashboard's "Minimum Round Length" shows 0 for a guild that has
never set it, and both close paths honour that. The one lookup is
`logic.effective_min_game_seconds`, shared by the host's **Close Round** button and by the
auto-close that fires once enough players have rolled; auto-close used to fall back to 1800s
on its own, so a guild reading 0 on the panel still watched a full round sit open for half an
hour. Opening the round with `ping:false` bypasses the floor entirely.

### Persistence and restarts

Active rounds, pending questions, and posted questions are all stored in SQLite. On bot restart the cog re-attaches all persistent views to the original messages, re-schedules auto-close timers from the remaining elapsed time, and sweeps both pending and posted questions older than 7 days.

**Roster names across a restart.** The roster embed prints display names as
plain text, never `<@id>` mentions — an embed mention is resolved client-side
only, so it shows a bare numeric id to any viewer who hasn't cached that user.
Names resolve via the shared chain in `services.name_resolver`: live member
cache → `state.display_names` → `<@id>`.

`state.display_names` is an in-memory dict filled when a player rolls, so it
empties on restart. Present players are recovered from the member cache, but
players who have since **left** cannot be — so on cog load
`seed_display_names_from_db` refills the dict from the persistent `known_users`
table for every restored round's roster (rollers plus the opener). Seeding never
overwrites an existing entry (a name captured at roll time is fresher than the
table) and is best-effort: a failed lookup logs and leaves those names as
mentions rather than blocking cog load.

## Permissions

- **User-side**:
  - `/risky start`: everyone, server only.
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
| Non-opener / non-admin presses **Close Round** | "Only the round opener can close this round." |
| **Close Round** before min-game-time elapsed | "This round cannot be closed yet. Please wait N more second(s)." |
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
- **No player-visible reroll.** Ties are settled by a hidden roll-off the bot runs
  itself; players are never asked to press Roll again. A dormant reroll state
  (`RiskyRollState.prepare_reroll`, `RoundResult.WAITING_FOR_REROLLS`, the ⚔️ Reroll
  embed field) shipped without a caller and was removed on 2026-08-20. The
  `risky_active_rounds.reroll_user_ids` column stays in the schema — it is nullable,
  was NULL on every live row, and dropping it would mean a migration against a table
  with rounds in flight for no gain.
- **No XP.** Round outcomes don't feed [[xp-spec]]; the economy quest trigger above fires on Roll instead.

## Configuration

| Key | Default | Purpose |
|---|---|---|
| Ping role | unset | Optional role to ping on `/risky start` (not when `ping:false`). Setting it to "no role" clears the row |
| Min game seconds | unset = 0 (no floor) | Floor on round duration; blocks an early **Close Round** and delays auto-close by the same amount. Saving 0 clears the row. `ping:false` bypasses |

Per-round only (not persisted as config):
- **Auto-close after N players** — default 25 (must be ≥ 2).
- **Auto-close after N minutes** — default 120 (must be > 0).

## Stored data

Four per-guild tables:

- **Active rounds** — one row per open game: opener, message id, rolls map (deserialised), auto-close settings, special-roll outcomes. Deleted on close.
  The table also carries a `reroll_user_ids` column, left over from a player-visible reroll flow that was never wired up; nothing reads or writes it (see **Non-goals**).
- **Pending questions** — between resolution and the question being asked. Includes the "two questioners" sub-game when the loser rolled 1. Swept on bot startup once older than 7 days (migration 173): the row is deleted when the winner asks, so a winner who never asks used to leave it forever. A row re-saved mid-round (the first of two questioners asking) keeps its original timestamp rather than restarting the clock.
- **Posted questions** — a question that's been sent and is awaiting a reply. Keyed by the question message id. Auto-swept on bot startup once older than 7 days.
- Two per-guild rows in the shared config table for the ping role and the min-game-time floor.

No DM data. No filesystem cache. In-flight rounds, prompts, and questions persist across restarts; the cog rebuilds in-memory state and re-attaches persistent views on next boot.
