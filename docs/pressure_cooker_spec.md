# Pressure Cooker — Feature Spec

A 1-v-1 stakes-pumping duel. Two players take turns clicking a single **Pump** button; each press adds a random 1–15 to a shared gauge. Whoever pushes the gauge past 100 loses, and the winner gets a modal to impose a custom nickname on the loser for 24 hours (default). Gameplay is Discord-only; per-guild settings are configured from the web dashboard's Games nav section (Pressure Cooker → Config). Not part of the games system; see [[games-system-spec]].

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/pressure challenge user:<member> stakes:[text]` | Slash | Everyone (server only) | Open a challenge against the named member |

`cancel`, `stats`, and `revert` subcommands used to exist but were never actually reachable —
each was stripped from the command tree in `setup()` before ever registering under `/games`,
same as every duel/group game's dead `config` subcommand (see [[dk-pvp-games-suite-spec]] §8).
The dead methods were deleted rather than wired up: a pending challenge self-expires after 60s
(no need to cancel), and neither stats-viewing nor early nickname revert have ever been
possible in Discord. Per-guild config (was `/pressure config`) lives on the web dashboard — see
[Configuration](#configuration).

## Behavior

### Opening a challenge

`/pressure challenge` validates a number of preconditions before the public challenge embed goes up:

- Server-only. Self-challenges and bot targets are rejected.
- The current channel must be in the per-guild channel allowlist (if the allowlist is non-empty).
- Per-challenger rate limit: max 3 challenges per hour. Older challenges fall out of the window.
- Bot permission preflight: the bot must have **Manage Nicknames**, and the bot's top role must sit above both players' top roles. The server owner is a recognized exception (Discord doesn't let bots rename the owner) — preflight skips the role check for owners and the loss is recorded without an enforced rename.
- Neither player may currently be serving an active nickname sentence from a prior game.
- No non-terminal game already exists between this pair in either direction.
- Pair cooldown check (canonicalised both directions): if cooldown is still running, the challenger sees the remaining hours/minutes.
- If `stakes` is supplied, the text is validated against the same filter rules as nicknames (zero-width strip, length cap, denylist).

On success, a public embed with **Accept** / **Decline** buttons posts in the channel. The buttons only respond to the target; everyone else gets an ephemeral rejection. The challenge embed lasts 60 seconds — if the target doesn't act, the next sweep marks it expired and the buttons disable.

### Playing the game

On accept, the buttons swap to a single **Pump** button. The starting player is picked at random. Each press rolls 1–15 and adds it to the gauge; the gauge is shown as a 20-char bar plus `current/100`. Because the per-press maximum (15) is strictly less than the ceiling (100), the first pump can never bust. After each pump, the turn passes to the other player.

When a pump pushes the gauge over 100, that player loses. The result embed posts in the same channel with a winner-only **Name the loser** button.

If a player double-clicks Pump or both players press near-simultaneously, an internal per-game lock serialises the presses so one is processed and the other returns "It's not your turn." cleanly.

### Naming the loser

The winner clicks **Name the loser** to open a one-paragraph modal capped at 32 characters. The submitted nickname runs through a validation pipeline:

- Strip zero-width unicode and NFC-normalise the string.
- Reject blanks.
- Enforce the configured max length (default 32, hard upper bound 32 — Discord's nickname cap).
- Reject matches against the built-in slur denylist and any per-guild additions.
- Reject names starting with `@`, `#`, or `/` (which can trigger Discord mentions or command parsing).
- Reject the literal strings `everyone` and `here`.
- Reject impersonation of an admin's display name or any other member's display name in the guild.

On pass, the bot applies the rename and starts a sentence timer (default 24 hours). On fail, the modal returns with the reason and stays open for another try.

**Server-owner edge case:** Discord forbids bots from renaming the guild owner. If the loser is the owner, the rename is skipped and a public message asks the owner to apply the nickname themselves: "Discord won't let me rename the server owner..." The sentence is recorded for stats either way.

If the winner doesn't click **Name the loser** within 5 minutes of the bust, the result transitions to no-nick-set and the prize lapses.

### Background sweep

A sweep runs every 60 seconds and handles three lifecycles:

- **Pending challenges over 60 seconds old** expire — the embed swaps to a "challenge expired" message and the buttons disable.
- **Active games idle more than 5 minutes** (no pump in 300 seconds) are abandoned. A cooldown is applied to the pair so the loser can't immediately re-challenge.
- **Resolved games where the winner hasn't named the loser within 5 minutes** transition to no-nick-set; the result embed updates accordingly.

The sweep also walks active nickname sentences and reverts every sentence whose timer has elapsed: restore the original nickname, mark the sentence reverted, and DM the loser. If the rename fails (member left the guild, bot lost permission, etc.) the sentence is still marked closed with the failure reason so it doesn't keep getting retried forever.

### Restart recovery

After a restart, the Pump button on active games and the Name the loser button on resolved games re-attach to their stored messages so the views remain interactive without anyone re-running the command.

## Permissions

**Bot needs:** Manage Nicknames, View Channel + Send Messages + Embed Links in any channel where games can run, and a top role higher than both players' top roles to perform the rename. Without Manage Nicknames the challenge is refused outright before any embed posts.

**User needs:**
- `/pressure challenge`: no Discord-side gate (server-only). Stakes text is subject to per-guild config.
- Per-guild config (web dashboard): admin.
- **Accept** / **Decline** buttons: only the challenged member can press.
- **Name the loser** button: winner only.

## User-visible errors

| When | The user sees |
|---|---|
| Run in DMs | "This command only works in a server." |
| Self-challenge | "You can't challenge yourself." |
| Bot target | "You can't challenge a bot." |
| Channel not in the allowlist | "Pressure Cooker isn't allowed in this channel." |
| Rate-limited | "You've issued too many challenges recently. Maximum 3 per hour." |
| Bot lacks Manage Nicknames | "I need the Manage Nicknames permission to enforce this game." |
| Bot's top role isn't above both players | "My highest role must be above both players' roles to rename the loser." |
| Either player already serving a sentence | "{Name} is serving a Pressure Cooker sentence and can't play again until it expires." |
| Wrong player clicks Pump | "It's not your turn." |
| Nickname fails validation | "Nickname rejected: {reason}" |
| Stakes fail validation | "Stakes rejected: {reason}" |
| Loser is the server owner | Public message: "Discord won't let me rename the server owner..." (sentence still recorded) |
| Rename fails for any other reason | "I don't have permission to rename that user." or "Failed to rename: {reason}" |
| Pair is still on cooldown | Ephemeral: "You're on cooldown with this player — try again in {hours}h {minutes}m." |
| Any unexpected modal / view error | "Something went wrong." |

## Non-goals

- **No team or >2 player variant.** Hard-coded to two players.
- **No dashboard for gameplay.** Challenges stay Discord-only; only per-guild config lives on the web dashboard.
- **No stats viewing, cancel, or manual early revert.** These subcommands were never actually reachable in Discord (see Commands above) and were removed rather than wired up — no interface surfaces W/L records, style/gauge history, or a manual nickname-revert control, even though every roll is recorded and nicknames still auto-revert on natural expiry.
- **No XP integration.** Outcomes don't feed any other system.
- **No spectator influence.** Outsiders cannot bet, vote, or otherwise affect a game.
- **No server-owner rename enforcement.** Discord blocks it; the system records the sentence and asks the owner publicly.
- **No retraction of a result.** Once a player busts, that game is locked in.

## Configuration

Per-guild row, editable from the web dashboard's Games nav section (Pressure Cooker →
Config). Previously `/pressure config`, an admin-only slash command that was removed —
see [[dk-pvp-games-suite-spec]] §8.

| Key | Default | Range | Purpose |
|---|---|---|---|
| `cooldown_hours` | 48 | ≥ 0 | Hours before the same pair can rematch |
| `sentence_hours` | 24 | ≥ 1 | How long an imposed nickname lasts |
| `channel_allowlist` | empty (allow all) | JSON array of channel ids | Empty means allow everywhere; non-empty restricts the game to those channels |
| `max_nick_length` | 32 | 1–32 | Hard upper bound matches Discord's 32-char cap |
| `max_stakes_length` | 200 | 1–2000 | Stakes are display-only — never persisted past the original embed |

The shared `duel_config` table also carries `allow_early_revert` and `nick_denylist` columns,
but neither is exposed by any command or web panel — see [[dk-pvp-games-suite-spec]] §11.

## Stored data

Per-guild config (the five keys above) and a per-guild record of every challenge: who challenged whom, the stakes text, the current state, current gauge, the per-pump audit log (roll + gauge-before + timestamp), and (on resolution) winner and loser ids. Active nickname sentences are stored separately with the loser's original nickname snapshot, the imposed nickname, applied and expiry timestamps, and a revert reason once cleared (the automatic expiry sweep is the only thing that ever sets this — see Background sweep). A per-pair cooldown table tracks the most recent resolved game between any two players.

No DM content is persisted. No filesystem cache. Per-game locks and the per-challenger rate-limit window live in memory only and reset on restart.
