# Pressure Cooker — Feature Spec

A 1-v-1 stakes-pumping duel. Two players take turns clicking a single **Pump** button; each press adds a random 1–15 to a shared gauge. Whoever pushes the gauge past 100 loses. What the loser owes depends on what the challenger staked: by default the winner gets a modal to impose a custom nickname on the loser for 24 hours, but a challenge can instead (or additionally) carry a coin wager and/or free-text stakes — see [Opening a challenge](#opening-a-challenge). Gameplay is Discord-only; per-guild settings are configured from the web dashboard's Games nav section (Live Games → Pressure Cooker). Pressure Cooker runs on the same shared duel/nickname-stake machinery as the other 1-v-1 and group games in that family — see [[dk-pvp-games-suite-spec]]; it is not part of the party-games system, see [[games-system-spec]].

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/pressure challenge user:<member> stakes:[text] wager:[coins] nickname:[bool]` | Slash | Everyone (server only) | Open a challenge against the named member |

`stakes`, `wager`, and `nickname` are all optional and independent. `nickname`
defaults to "on" only when nothing else is staked — plain `/pressure challenge
user:X` still means the classic nickname duel. Adding `stakes:` and/or
`wager:` without touching `nickname` turns the rename off automatically;
setting `nickname:true` explicitly keeps it on top of whatever else is
staked. Setting `nickname:false` with no `stakes:` or `wager:` is rejected —
a duel needs at least one live stake.

`cancel`, `stats`, and `revert` subcommands used to exist but were never actually reachable —
each was stripped from the command tree in `setup()` before ever registering under `/games`,
same as every duel/group game's dead `config` subcommand (see [[dk-pvp-games-suite-spec]] §8).
The dead methods were deleted rather than wired up: a pending challenge self-expires after
`CHALLENGE_RESPONSE_SECONDS` (5 minutes, no need to cancel), and neither stats-viewing nor
early nickname revert have ever been possible in Discord. Per-guild config (was
`/pressure config`) lives on the web dashboard — see [Configuration](#configuration).

## Behavior

### Opening a challenge

`/pressure challenge` validates a number of preconditions before the public challenge embed goes up:

- Server-only. Self-challenges and bot targets are rejected.
- Pressure Cooker must be switched on for the server (the dashboard's "Available on This Server" toggle for this game — off is a per-guild opt-out, not the default).
- The current channel must be in the per-guild channel allowlist (if the allowlist is non-empty).
- Per-challenger rate limit: configurable per-guild cap, default 30 challenges per hour (0 = no limit). Older challenges fall out of the window.
- If `stakes` is supplied, the text is validated with a lighter version of the nickname filter (zero-width strip, length cap, denylist — no impersonation or `@`/`#`/`/`/everyone-here checks).
- Whether this challenge renames the loser is resolved from `stakes`, `wager`, and `nickname` together (see Commands above). Only when it does:
  - Bot permission preflight: the bot must have **Manage Nicknames**, or the challenge is refused outright. Role hierarchy is **not** a hard gate any more — if a player's top role sits at or above the bot's, the challenger gets a non-fatal warning and the game proceeds; if that player loses, the win stands but the rename is silently skipped for them. The server owner gets the same kind of heads-up (Discord never lets a bot rename the owner) — the loss is recorded either way, with no enforced rename.
  - Neither player may currently be serving an active nickname sentence from a prior game.
- If `wager` is supplied, it must be a positive whole number, the guild's economy must be enabled, and the challenger must hold the balance — checked when the challenge is opened. Nothing is actually taken from either player's balance until the target accepts; a decline or an unanswered challenge costs nothing.
- No non-terminal game already exists between this pair in either direction.
- **No pair-cooldown check runs here.** `duels.db.check_cooldown`/`set_cooldown`
  (backed by the `duel_cooldowns` table) exist, are unit-tested, and are wrapped
  in this game's own `db.py` — but nothing in `base_duel.py` or this cog's
  challenge/accept/sweep path ever calls them, for Pressure Cooker or any other
  1-v-1 duel game. See the `cooldown_hours` note under Configuration.

On success, a public embed with **Accept** / **Decline** buttons posts in the channel. The buttons only respond to the target; everyone else gets an ephemeral rejection. The challenge embed lasts `duels.db.CHALLENGE_RESPONSE_SECONDS` (5 minutes) and shows a live countdown to its own deadline — if the target doesn't act, the next sweep marks it expired and the buttons disable.

### Playing the game

On accept, the buttons swap to a single **Pump** button. The starting player is picked at random. Each press rolls 1–15 and adds it to the gauge; the gauge is shown as a 20-char bar plus `current/100`. Because the per-press maximum (15) is strictly less than the ceiling (100), the first pump can never bust. After each pump, the turn passes to the other player.

When a pump pushes the gauge over 100, that player loses. The result embed posts in the same channel. If a nickname is staked (see Commands above — the default when nothing else was staked), it carries a winner-only **Name the loser** button; a wager and/or custom-stakes-only game posts the result with no button and settles automatically (see Naming the loser and Background sweep below).

If a player double-clicks Pump or both players press near-simultaneously, an internal per-game lock serialises the presses so one is processed and the other returns "It's not your turn." cleanly.

### Naming the loser

This step only happens for a nickname-staked game (the default when the challenge carried no `stakes:`/`wager:`, or when the challenger explicitly asked for `nickname:true`). A wager-only or custom-stakes-only game has no rename step at all — the result embed states the stakes/pot and the game is done; a wager pays out automatically, see below.

The winner clicks **Name the loser** to open a single-field modal capped at 32 characters. The submitted nickname runs through a validation pipeline:

- Strip zero-width unicode and NFC-normalise the string.
- Reject blanks.
- Enforce the configured max length (default 32, hard upper bound 32 — Discord's nickname cap).
- Reject matches against the built-in slur denylist and any per-guild additions.
- Reject names starting with `@`, `#`, or `/` (which can trigger Discord mentions or command parsing).
- Reject the literal strings `everyone` and `here`.
- Reject impersonation of an admin's display name or any other member's display name in the guild.

On pass, the bot applies the rename and starts a sentence timer (default 24 hours). On fail, the modal returns with the reason and stays open for another try.

**Server-owner edge case:** Discord forbids bots from renaming the guild owner. If the loser is the owner, the rename is skipped and a public message asks the owner to apply the nickname themselves: "Discord won't let me rename the server owner..." The sentence is recorded for stats either way.

**Loser outranks the bot:** the pre-challenge warning (see Opening a challenge) covers the common case, but role positions can still change between challenge and bust. If the loser's top role sits at or above the bot's when the winner submits the nickname, the rename is skipped the same way: the win stands, the winner is told the name couldn't be applied, and the loser is publicly told the nickname on an honour-system basis.

If the winner doesn't click **Name the loser** within 5 minutes of the bust, the result transitions to no-nick-set and the prize lapses.

### Background sweep

A sweep runs every 60 seconds and handles three lifecycles:

- **Pending challenges past `CHALLENGE_RESPONSE_SECONDS` (5 minutes)** expire — the embed swaps to a "challenge expired" message and the buttons disable. The sweep's own cadence is unchanged at 60 seconds, so expiry lands within a minute of the deadline.
- **Active games idle more than 5 minutes** (no pump in 300 seconds) are abandoned — no cooldown follows; nothing stops the same pair from re-challenging immediately (pair cooldown is dead code here, see the `cooldown_hours` note under Configuration).
- **Resolved games where the winner hasn't named the loser within 5 minutes** transition to no-nick-set; the result embed updates accordingly.

The sweep also walks active nickname sentences and reverts every sentence whose timer has elapsed: restore the original nickname, mark the sentence reverted, and DM the loser. If the rename fails (member left the guild, bot lost permission, etc.) the sentence is still marked closed with the failure reason so it doesn't keep getting retried forever.

**Economy settlement.** Every terminal state (win, decline, expiry, abandonment) runs one economy hook: a wagered game either pays the whole pot to the winner (win/resolved states) or refunds every player's ante (any other terminal state, including a declined or expired challenge) — exactly once per game. Separately, a resolved game also pays the standard participation/win coin rewards through the same shared reward path every other game uses, regardless of whether it was a nickname, wager, or custom-stakes game.

### Restart recovery

After a restart, the Pump button on active games and the Name the loser button on resolved games re-attach to their stored messages so the views remain interactive without anyone re-running the command.

## Permissions

**Bot needs:** Manage Nicknames, and View Channel + Send Messages + Embed Links in any channel where games can run. Without Manage Nicknames, any challenge that would rename the loser is refused outright before any embed posts (a wager/custom-stakes-only challenge, with `nickname:false`, does not need this permission at all). A top role higher than both players' top roles is only needed to actually *perform* a rename — it is not a precondition to open or play a nickname-staked challenge; without it the game still runs and the rename for that player is silently skipped at settlement.

**User needs:**
- `/pressure challenge`: no Discord-side gate (server-only). Stakes text and any wager are subject to per-guild config and, for a wager, to the guild's economy being enabled.
- Per-guild config (web dashboard): admin.
- **Accept** / **Decline** buttons: only the challenged member can press.
- **Name the loser** button (nickname-staked games only): winner only.

## User-visible errors

| When | The user sees |
|---|---|
| Run in DMs | "This command only works in a server." |
| Self-challenge | "You can't challenge yourself." |
| Bot target | "You can't challenge a bot." |
| Game switched off for this server | "Pressure Cooker is switched off on this server." |
| Channel not in the allowlist | "Pressure Cooker isn't allowed in this channel." |
| Rate-limited | "You've issued too many challenges recently. Maximum {N} per hour." (N is the per-guild dial, default 30) |
| `nickname:false` with no `stakes:`/`wager:` | "Turning the nickname stake off means you need to stake something else — add `wager:` or `stakes:`." |
| Nickname-staked challenge, bot lacks Manage Nicknames | "I need the Manage Nicknames permission to enforce this game." |
| Nickname-staked challenge, a player's role sits above the bot's | Non-fatal ephemeral warning at challenge time; the challenge still opens and the rename is skipped for that player only if they lose. |
| Wager under 1 coin | "A wager has to be at least 1." |
| Wager with the economy off | "The economy isn't enabled here, so wagers can't run." |
| Wager exceeds the challenger's balance | "You need {amount} to stake that — you have {balance}." |
| Either player already serving a sentence | "{Name} is serving a nickname sentence and can't play again until it expires." |
| Wrong player clicks Pump | "It's not your turn." |
| Nickname fails validation | "Nickname rejected: {reason}" |
| Stakes fail validation | "Stakes rejected: {reason}" |
| Loser is the server owner | Public message: "Discord won't let me rename the server owner..." (sentence still recorded) |
| Rename fails for any other reason | "I don't have permission to rename that user." or "Failed to rename: {reason}" |
| Any unexpected modal / view error | "Something went wrong." |

## Non-goals

- **No team or >2 player variant.** Hard-coded to two players.
- **No dashboard for gameplay.** Challenges stay Discord-only; only per-guild config lives on the web dashboard.
- **No stats viewing, cancel, or manual early revert.** These subcommands were never actually reachable in Discord (see Commands above) and were removed rather than wired up — no interface surfaces W/L records, style/gauge history, or a manual nickname-revert control, even though every roll is recorded and nicknames still auto-revert on natural expiry.
- **No standalone XP integration beyond the shared economy hooks.** A resolved game pays the same participation/win coin rewards every duel/group game pays, and a `wager:` challenge escrows and settles coins through the shared wager service — but Pressure Cooker itself has no bespoke XP or stats system, and nothing about a game (nickname, custom stakes, or wager) feeds any tracked stat beyond that.
- **No spectator influence.** Outsiders cannot bet, vote, or otherwise affect a game.
- **No server-owner rename enforcement.** Discord blocks it; the system records the sentence and asks the owner publicly.
- **No retraction of a result.** Once a player busts, that game is locked in.

## Configuration

Per-guild row, editable from the web dashboard's Games nav section (Live Games →
Pressure Cooker). Previously `/pressure config`, an admin-only slash command that was
removed — see [[dk-pvp-games-suite-spec]] §8.

| Key | Default | Range | Purpose |
|---|---|---|---|
| `cooldown_hours` | 48 | ≥ 0 (dashboard input capped at 8760) | Labeled "Wait Before a Rematch" on the panel; stored and editable, but currently a dead dial for this game — see note below |
| `sentence_hours` | 24 | ≥ 1 (dashboard input capped at 8760) | How long an imposed nickname lasts |
| `channel_allowlist` | empty (allow all) | JSON array of channel ids | Empty means allow everywhere; non-empty restricts the game to those channels |
| `max_nick_length` | 32 | 1–32 | Hard upper bound matches Discord's 32-char cap |
| `max_stakes_length` | 200 | 1–2000 | Stakes are display-only — never persisted past the original embed |
| `nick_denylist` | empty | comma-separated words on the panel (stored as a JSON array) | Extra banned words checked on top of the built-in slur denylist, for both nicknames and stakes text |
| `challenge_limit_per_hour` | 30 | 0–999 | Per-challenger challenges-per-hour cap; 0 = no limit |

These live in the shared `duel_config` table (one row per guild per game type,
keyed by `game_type = 'pressure'`) alongside every other duel/group game's
config — see [[dk-pvp-games-suite-spec]] §11. `duel_config` used to also carry
an `allow_early_revert` column; it was never read by anything and its column
was dropped outright (migration 194), so early nickname revert isn't just
unexposed, it no longer has anywhere to be stored. `cooldown_hours` is a
milder version of the same drift: `duels.db.check_cooldown`/`set_cooldown`
(the pair-cooldown functions this key would feed, backed by `duel_cooldowns`)
are still defined and unit-tested, but nothing on the challenge/accept/sweep
path for Pressure Cooker — or `hot_potato`/`quickdraw`, the other two
`BaseDuel` games — ever calls them. Only `check_group_cooldown`/
`set_group_cooldown` (a separate pair, for N-player `BaseGame` games) are
actually wired up. So this dial is stored and dashboard-editable but has no
effect on this game today.

A separate, game-agnostic **"Available on This Server" toggle** (also on this
panel) switches Pressure Cooker on or off per guild; no row for a guild means
enabled.

## Stored data

Per-guild config (the keys above, plus the on/off toggle in its own shared
table) and a per-guild record of every challenge: who challenged whom, the
stakes text, the current state, current gauge, the per-pump audit log (roll +
gauge-before + timestamp), and (on resolution) winner and loser ids. Active
nickname sentences are stored separately with the loser's original nickname
snapshot, the imposed nickname, applied and expiry timestamps, and a revert
reason once cleared (the automatic expiry sweep is the only thing that ever
sets this — see Background sweep). A per-pair `duel_cooldowns` table exists
and would track the most recent resolved game between any two players, but
nothing currently writes or reads it for this game (see the `cooldown_hours`
note under Configuration) — it stays empty. A wagered game's escrowed
coins live in the shared duel wager-service tables, not in a Pressure
Cooker-specific one, for exactly as long as the game is unresolved.

No DM content is persisted. No filesystem cache. Per-game locks and the per-challenger rate-limit window live in memory only and reset on restart.
