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
| Risky Rolls panel | Web (dashboard) | Admin | Configure the ping role, the min-game-time floor, the per-channel round cap, and the two payoff dials (chase / bank fallback) |
| Feature Rotation "Setup" | Web (dashboard) | Admin | Pick Risky Rolls as a room's featured game — a round starts automatically each day the room is featured, no command involved |
| Scheduled Games | Web (dashboard) | Admin | Schedule recurring/one-off Risky Rolls launches in a channel, independent of (and stacking with) Feature Rotation |

## Behavior

### Starting a round

`/risky start` opens a new round. The bot checks Send Messages + Embed Links in the channel, refuses if the channel already has the configured number of active games (default 10, dashboard-adjustable — see **Configuration**), then posts the round embed with the **Roll / How to Play / Close Round** buttons. If a ping role is configured, the bot also posts a one-line ping ("A new Risky Rolls round has begun!") — allow-listing exactly that role rather than a blanket `roles=True`. A guild that has **never** set the dial gets a `@Risky Rolls` role created on the first pinged round (`core/role_provision.py`); an admin who picked "(none)" keeps silent rounds. Passing `ping:false` skips the role ping and bypasses the min-game-time floor — the two move together, since the floor exists to give pinged members time to arrive. (This replaced the separate `/risky start_no_ping` command on 2026-07-28 — the callbacks were identical but for the flag, so the flag absorbed the command.)

An auto-close is scheduled at start: by default the round auto-closes 120 minutes after start, or sooner once 25 distinct players have rolled (whichever comes first, never before the min-game-time floor).

### Starting automatically (Feature Rotation / Scheduled Games)

A round doesn't need `/risky start` at all. Two dashboard-configured paths call the same launcher (`RiskyRollCog.launch`, registered as `bot.game_launchers["risky_roll"]`) with no interaction behind it:

- **Feature Rotation** — an admin picks Risky Rolls under a room's "run a game while this room is open" setup (`/#/feature-rotation`). A round starts the moment the room becomes the day's featured room and, if still open, is closed out when the room's day ends through the round's own resolution (winner picked, no-contact gate consulted, question prompts sent) rather than a silent cancel — though most rounds resolve on their own auto-close timer well before then.
- **Scheduled Games** (`/#/games-scheduling`) — a recurring or one-time schedule targeting a channel, independent of Feature Rotation. A schedule skips a day the rotation currently has that room hidden.

Both paths refuse to start on top of an already-running round in the channel — `launch()` caps a channel at **one** auto-launched round regardless of the dashboard's per-channel round cap (that cap governs `/risky start` only), so neither auto-launch path trips over the other or over a round already open by hand; picking Risky Rolls for a room's Feature Rotation slot *and* scheduling Risky Rolls in that same channel just means both fire on the room's open day, stacking rather than replacing each other, one after the other closes. An auto-launched round always opens quietly: it skips the role ping and the min-game-time floor unconditionally (equivalent to `ping:false`). The round's opener differs by path, though — Feature Rotation launches with `host_id=0` (hosted by nobody), while Scheduled Games launches with the schedule's creator as `host_id`, so a scheduled round's opener is whoever set up that schedule.

### Rolling

Pressing **Roll** rolls 1–100 once per player. The roll is appended to the round embed with a decoration (🔥 for 69, ⭐/🥇 for current winner, 💀/☠️ for current loser, 🎲 otherwise). A player can't roll twice.

### Closing and resolving

**Close Round** (opener or admin) checks two things: that the min-game-time has elapsed, and that at least two players have rolled. If a tie for the top is detected, the bot runs a hidden re-roll-off among the tied players (recursively if the re-roll also ties) until a single winner emerges; same for the bottom if needed.

Special-roll outcomes:
- **Anyone rolled 69** — that roller wins; the prompt becomes a "room" question that asks every participant. The bot creates a thread off the prompt message (`auto_archive_duration = 1440`) for the conversation, falling back to a channel followup if thread creation fails.
- **Winner rolled 100** — the winner picks both the lowest and second-lowest players as recipients of their question.
- **Loser rolled 1** — a "two questioners" sub-game spawns: both the top and second-top each get to ask the loser one question.

After resolution, the **Roll / Close** view is disabled and replaced with an **Ask Question** prompt aimed at the eligible questioner(s).

A resolved round — whether closed by the button or by the auto-close timer — is put on the games record before its own rows are deleted: one `games_game_history` row with `game_type = 'risky_roll'`, the opener as `host_id`, `player_count` = the number of rolls, `round_count` 1, the guild id set, and a payload of who rolled what plus the resolved seats (`players`, `rolls`, `highest_user`, `lowest_user`, `second_*`). That is what Play Statistics, `/recap`, the game-night session tracker and the Ping Response game-player join read, and Risky Rolls — the most-played game on the server — was in none of them until 2026-09-04. A round that closes without resolving (fewer than two rolls, or the no-contact refusal that looks the same) writes nothing; recording is best-effort and never holds up the winner's prompt. The write goes through `games.utils.game_history.history_insert`, the same statement the duel and group games use, and is idempotent on `game_id`.

### Asking and replying

**Ask Question** opens a 300-character modal. On submit, the bot posts the question (in a thread for room/69 questions, in the channel for direct questions) with a **Reply** button. **Reply** opens a 300-character reply modal; the first valid reply edits the original question message in place to embed the reply text, and closes the reply window.

Both the question **and** the reply are public free text, so both are screened against the shared slur/abuse denylist (`duels/filters.contains_disallowed_content`) — a match is rejected with an ephemeral "contains disallowed content" and nothing is posted.

### Chasing the payoff

The round's payoff is the winner's question, and most rounds never got one: in one week at least 12 rounds resolved with a winner who never pressed **Ask Question**, four of six posted questions sat unanswered for over a day, and nothing re-pinged anyone — the 7-day sweep just deleted the prompt in silence. Two dashboard dials chase it. **Both ship at 0 (off)**; a guild that never sets them behaves exactly as before, and a dial an admin turns off is off at the next tick (the chaser reads the config rows fresh, no restart).

- **Chase the winner's question after N hours** (`risky_chase_hours`) — once a pending prompt is N hours old, whoever still owes a question (the winner; on a 1-rule round, whichever of the two questioners has not asked) gets **one** in-channel re-ping: "⏰ … your Risky Rolls question is still waiting. Press **Ask Question** above to send it." Once a question is posted, the answerer(s) get one re-ping of their own N hours after it was posted: "⏰ … {asker}'s question is still waiting for your reply." Only the answerer(s) are on that message's mention allow-list — the asker's `<@id>` is in the content so it renders as a name, not so they get pinged about their own question. Each re-ping fires exactly once: `chased_at` is written to the row when it goes, so a restart cannot repeat it.
- **Fall back to a bank question after N hours** (`risky_fallback_hours`) — once a pending prompt is N hours old with a question still owed, the bot draws a **Truth** from the Truth or Dare bank (`games.utils.question_source.get_ffa_prompt`, `kind="truth"`; the same bank the rotation rooms' prompts come from) and posts it as the winner's question, so the loser still answers. The channel's own age gate decides whether spicy rows are eligible (`channel_allows_nsfw`, i.e. Discord's `is_nsfw()` — never a bot-side toggle). The post has the same shape as a winner's own question — targets, then "{winner} ran out of time, so the deck asks for them:", then the question, with the **Reply** button — and is registered as a posted question flagged `from_bank`, so the reply render says "the deck asks for {winner}:" rather than putting the bank's words in the winner's mouth. The disabled prompt message is left reading "{winner} ran out of time — the deck asked {targets} for them: > …". A 69 room prompt gets the room version ("rolled 69 but never asked, so the deck asks the room"), in a thread off the prompt where one can be made, with no Reply button (a room question never had one). On a 1-rule prompt the fallback speaks for whichever questioner still owes — the winner first; if neither asked, the prompt is kept between the two (re-saved with the winner marked as asked, its message updated, exactly as when the first of two asks by hand) and the next tick speaks for the second questioner.

**A fallback that fails backs off, and after three tries the prompt is abandoned** (ship review, 2026-09-05). Every way the fallback can fail to post is the slow kind — an empty question bank, a channel that has been deleted, a pairing the no-contact list now forbids — and the old code returned "leave it for the next tick", which meant re-drawing every five minutes for the seven days until the sweep aged the row out. The failure is now counted on the row (`fallback_attempts`, `fallback_attempted_at`, migration 213): the deck waits an hour after the first failure and two after the second, and once `logic.FALLBACK_MAX_ATTEMPTS` (3) are spent the prompt is picked up no more — no fallback, and no chase in its place either, since a due fallback already outranks the chase. The count is persisted, so a restart does not hand an unpostable prompt three fresh attempts, and the give-up is logged once at warning. A successful fallback on a 1-rule prompt leaves the count at 0, so the second questioner's turn gets its own three tries.

Timing decisions are `logic.pending_payoff_action` and `logic.posted_chase_due`, tested bare in `tests/test_risky_roll_payoff.py`. When both dials are on and both are due at once (after a restart, or with a chase window no shorter than the fallback window), the **fallback wins** and no chase goes out — a stalled prompt gets the question, not a nag and the question. A prompt with no usable age (`created_at` NULL, pre-migration-173) is left alone rather than treated as infinitely old.

The chaser is one background loop (`views.run_payoff_pass`, every `PAYOFF_TICK_SECONDS` = 5 minutes) started lazily from the game's own traffic — a roll, a round closing — rather than from cog load, so prompts restored across a restart are picked up by the first roll after it. It acts on **at most one prompt per channel per tick**: flipping a dial on over a backlog of stale prompts drains them a message every five minutes rather than dumping a week of questions into the room at once (an admin who wants a clean start can `/risky reset_state` first).

**No-contact.** The pairing was gated on the draw, but the list can change in the hours before a fallback fires, and a question the bot posts *for* the winner is still the winner's question to the loser. A direct fallback whose (asker, target) pair the list now forbids is skipped silently — nothing posts, nothing says why, the prompt is left for the sweep — which is indistinguishable from the dial being off. A room fallback drops the asker's partners from its `@`-mention list, exactly as the winner's own room question does. The answerer re-ping on a posted question is gated the same way (`logic.posted_chase_blocked`): it is one message to everyone who may reply with the asker's question attached, so **any** answerer the list now pairs with the asker skips the whole chase — a deck question included, since the reply still goes to the asker. The skipped row is still stamped `chased_at`, so the next tick does not pick it again and the silence is indistinguishable from the dial being off.

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
| `/risky start` with the channel at its configured game cap (default 10) | "This channel already has N active games. Close one before starting another." |
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
| Dashboard sends max-games-per-channel < 1 | HTTP 400 |
| Dashboard sends a payoff dial outside 0–168 hours | HTTP 400 |

## Economy integration

Pressing **Roll** fires the `risky_roll` economy quest trigger (once per member
per round, keyed on the game id — `bot_modules/services/risky_roll/views.py:386-389`,
via `fire_member_trigger`). The roll itself is the qualifying act, so it fires at
roll time, not round close. Best-effort: an economy failure never blocks the roll.

## Non-goals

- **No leaderboards.** Wins / losses aren't aggregated; closed rounds delete their state. The one thing that outlives a round is its `games_game_history` row (see **Closing and resolving**) — a play record for the dashboard, not a scoreboard.
- **No DM mode.** Server-only.
- **No multi-channel rounds.** A round lives in one channel; the per-channel game cap (**Configuration**, default 10) applies per channel.
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
| Ping role | unset | Optional role to ping on `/risky start` (not when `ping:false`, and not on an auto-launched round — see **Starting automatically**). Setting it to "no role" clears the row |
| Min game seconds | unset = 0 (no floor) | Floor on round duration; blocks an early **Close Round** and delays auto-close by the same amount. Saving 0 clears the row. `ping:false` and an auto-launched round both bypass it |
| Max games per channel | 10 | How many rounds `/risky start` will let stack in one channel before refusing (1–100). Auto-launched rounds ignore this dial — they cap at one per channel regardless |
| Chase hours (`risky_chase_hours`) | unset = 0 (off) | Hours before the one re-ping of whoever owes a question, and of the answerer once a question is posted (0–168). Saving 0 clears the row. See **Chasing the payoff** |
| Fallback hours (`risky_fallback_hours`) | unset = 0 (off) | Hours before a bank Truth is posted as the winner's question (0–168). Saving 0 clears the row. Set longer than the chase, or the chase never gets its turn |

Per-round only (not persisted as config):
- **Auto-close after N players** — default 25 (must be ≥ 2).
- **Auto-close after N minutes** — default 120 (must be > 0).

## Stored data

Four per-guild tables:

- **Active rounds** — one row per open game: opener, message id, rolls map (deserialised), auto-close settings, special-roll outcomes. Deleted on close, after a resolved round's summary has been copied into the shared `games_game_history` table (`docs/data_register.md`, the `games_*` row).
  The table also carries a `reroll_user_ids` column, left over from a player-visible reroll flow that was never wired up; nothing reads or writes it (see **Non-goals**).
- **Pending questions** — between resolution and the question being asked. Includes the "two questioners" sub-game when the loser rolled 1. Swept on bot startup once older than 7 days (migration 173): the row is deleted when the winner asks, so a winner who never asks used to leave it forever. A row re-saved mid-round (the first of two questioners asking) keeps its original timestamp rather than restarting the clock. `chased_at` (migration 210) records the one chase re-ping.
- **Posted questions** — a question that's been sent and is awaiting a reply. Keyed by the question message id. Auto-swept on bot startup once older than 7 days. `chased_at` records the one answerer re-ping; `from_bank` marks a question the fallback drew for a winner who never asked (migration 210).
- Five per-guild rows in the shared config table for the ping role, the min-game-time floor, the max-games-per-channel cap, and the two payoff dials.

No DM data. No filesystem cache. In-flight rounds, prompts, and questions persist across restarts; the cog rebuilds in-memory state and re-attaches persistent views on next boot.
