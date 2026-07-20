# dungeon-keeper

Discord moderation, community, voice, and analytics bot.

## Features

> Each entry below is written as a short, advertising-ready blurb. Slash commands and
> the web dashboard panels that drive each feature are listed in
> [Slash Commands](#slash-commands) and the dashboard sections further down.

### Moderation & safety
- **Jail** — Send a member to a private intake channel with their roles stripped and automatically restored on release. Pull witnesses in or remove them, and every action lands in a unified `/modinfo` history and audit log.
- **Tickets** — Panel-button-driven private support channels members open for help, or mods open for a quiet word. Claim, escalate, close, reopen, and generate a full transcript on close — all from persistent buttons that survive restarts.
- **Warnings** — Document infractions with `/warn`, review them with `/warnings`, and undo with `/revokewarn`, with configurable escalation to admins at a threshold. `/modinfo` rolls jail history, warnings, and tickets into one per-member profile.
- **AI Moderation** — On-demand AI review of a user, a channel, or a free-form question, backed by a guard model that understands your community's consent norms. Every confirmed or dismissed flag becomes a labeled example that tunes future judgments to your server.
- **Rules Watch** — A passive, recall-leaning AI monitor that pre-screens public chat with cheap heuristics, then weighs suspicious messages against context signals like mutual history, reciprocity, and stated boundaries. Flags route to a human-reviewed priority queue with one-click confirm/dismiss buttons that grow a server-specific training set.
- **Spoiler guard** — Images posted in spoiler-required channels without a spoiler flag are removed with a friendly, self-deleting reminder. Bypass roles keep trusted members exempt, and the check never blocks the rest of the message pipeline.
- **Policies** — Collaborative policy proposals with an open / vote / close / list flow so rule changes are decided in the open. Keeps your moderation team aligned without leaving Discord.
- **Purge & Privacy** — Bulk-delete messages by count and/or cutoff time with `/purge`. Members can erase all of their own data with `/delete_me`, and mods can fully purge a user with `/delete_user`.

### XP & analytics
- **XP & leveling** — Earn XP from text, replies, voice participation, and reactions on your image posts, with anti-grind multipliers keeping rewards fair. Configurable level milestones grant roles, and level-up and level-5 announcements celebrate progress.
- **Leaderboards** — `/xp_leaderboards` ranks top earners by source and time window and shows you exactly where you stand. The dashboard adds time-to-level histograms and per-source breakdowns.
- **Web analytics dashboard** — An opt-in LAN dashboard (`DASHBOARD_ENABLED=1`) with 25+ cached, read-only panels covering engagement, retention, community structure, growth, anomalies, and quality. Caches pre-warm hourly and refresh every 15 minutes so big servers load instantly.
  - *Engagement & retention:* DAU/MAU stickiness, cohort retention curves, newcomer activation funnel, churn-risk early-warning scores.
  - *Activity patterns:* 7×24 message heatmap, message-rate trends, join-time distribution, voice stats, activity timeline.
  - *Community structure:* force-directed interaction graph, animated interaction heatmap, participation Gini/Lorenz, channel-health comparison.
  - *Growth & onboarding:* invite effectiveness, role growth, time-to-level, greeter response latency.
  - *Anomalies & at-risk:* message-rate drops, burst ranking, drop-off, session burst, chilling-effect detection.
  - *Quality & demographics:* per-member quality score, NSFW gender activity, oldest SFW members, reaction analytics.
- **Reports** — Member, role, and engagement reports live in the dashboard, and `/quality_leave add/remove/list` tracks members on an approved leave of absence so they aren't flagged as inactive.

### Voice & music
- **Voice Master** — Join a hub channel to instantly spawn your own voice room, then lock, hide, rename, set a limit, invite, kick, transfer, or claim it. Per-user profiles persist trust lists, block lists, and knock-to-join across sessions, all configurable from the dashboard.
- **Music** — YouTube and Spotify playback via Lavalink with a persistent now-playing card and queue: `/play`, `/skip`, `/shuffle`, `/loop`, `/queue`, `/pause`, `/resume`, `/stop`, `/nowplaying`, `/disconnect`. Mod-only `/247` keeps the bot parked in a channel and auto-queues from a playlist when idle.

### Party games
A 17-game social suite that shares session windows, anonymous audit logging, per-guild
enable/disable, channel allowlists, and an AI question-bank fallback.
- **Free For All** — A host poses a question and everyone answers, in chat or through a name-hiding popup modal. Lurk anonymously or jump in as yourself.
- **Truth or Dare Card** — The banner variant of Free For All: just drops the prompt card in the channel for open chat, no reply buttons.
- **Would You Rather** — Multi-round voting where each prompt splits the room between two options. Queue your own scenarios or let the bot generate them, then reveal who picked what.
- **Never Have I Ever** — Confess or claim innocence as each statement is read aloud. Play with lives for elimination stakes or set lives to zero for casual voting.
- **Most Likely To** — The room votes on who best fits each prompt and the winner takes a crown. Most crowns after all rounds wins — and yes, you can vote for yourself.
- **Marry / Fornicate / Kiss** — Get three random names from the player pool and sort them into the three categories. Rename the categories to anything you like for a custom spin.
- **Two Truths & a Lie** — Submit two truths and a lie, then watch the room vote on which is fake. Earn points for fooling others and for catching their lies.
- **Truth or Dare** — Classic Truth or Dare with opt-in SFW/NSFW Truth and Dare pools. Turn weighting keeps everyone involved by favoring whoever's been asked least.
- **Spin the Compliment** — Everyone is matched to one other person and gives them a genuine compliment. Pairings post publicly for a warm moment in the middle of the chaos.
- **Hot Takes** — Submit spicy anonymous opinions, then rate each one on a 5-step 🧊-to-🔥 temperature scale. The average heat for every take is revealed at the end.
- **Story Builder** — Write a story together one sentence at a time on alternating turns. Choose blind mode (see only the previous line) for chaos or full visibility for coherence.
- **Anonymous AMA** — One player takes the hot seat and fields anonymous questions from the room. Run it unfiltered or host-screened, with DM pings when a questioner gets a reply.
- **Fantasies & Dealbreakers** — Anonymously submit what you'd love or hate, then vote "Same" or "Not for me" on each entry. The host runs as many rounds as the vibe can sustain.
- **Name Your Price** — Name the secret price it would take you to do a given scenario. Prices reveal low-to-high, then the room votes Most Reasonable and Most Unhinged.
- **Mt. Rushmore Draft** — Snake-draft your top four picks for a topic, with no duplicates allowed once a pick is gone. Everyone reveals their board and the room votes on the best lineup.
- **Clapback** — A prompt drops and everyone writes their funniest anonymous one-liner. Answers go head-to-head for votes, and sweeping every vote earns a "CLAPBACK!" bonus.
- **LegitLibs** — Mad-Libs–style template fill with reveals in parallel (Quiplash) or round-robin (Classic) mode. Four heat tiers run from Flirty to Unhinged.
- **Photo Challenge** — A host drops a Photo Challenge card and a thread opens for everyone's photo submissions.

### Head-to-head & group games
High-stakes games with server-authoritative hidden state, per-pair cooldowns, audit
logging, and 24-hour auto-reverting nickname stakes (or custom cosmetic stakes).
- **Pressure Cooker** — A 1v1 duel where each press adds 1–15 to a shared gauge. Whoever pushes it past 100 loses — and the winner renames them for 24 hours.
- **Quickdraw** — A hidden timer counts down to "DRAW!" and the first to hit FIRE wins. Draw early and you instantly lose; if nobody fires in time it's a clean void.
- **Hot Potato** — A bomb with a hidden fuse passes between players (1v1 or group free-for-all), with a 2-second anti-ping-pong lock. Whoever's holding at detonation is out.
- **Chicken** — A shared meter climbs toward 100 while everyone decides when to bail. In a duel the first to bail loses; in a group, everyone still holding at the crash goes down together.
- **Musical Chairs** — 3+ players, one fewer chair each round, and music that plays for a hidden duration before you scramble to SIT. Sit too early and you false-start; last one standing wins.

### Engagement & content
- **Whisper** — Send an anonymous message to an opted-in member who gets three guesses to name the sender. Share publicly, reply back, or reveal yourself once you're guessed.
- **Pen Pals** — Members join a pool and a scheduled round pairs eligible members (nobody re-matched more than once a month) into private 2-person channels, each seeded with a conversation-starter question. Channels tear down after ~24 hours, and mods can pair specific members or kick off a new round.
- **Confessions** — Post an anonymous confession via `/confess` to a channel or forum thread, each with anonymous-reply buttons. Replies use either a stable per-thread identity or a fresh ephemeral one, and everything mirrors to a mod-only log.
- **Starboard** — Reactions with a configured emoji repost high-engagement messages to a dedicated board once they cross a threshold. Self-stars don't count and an NSFW guard keeps age-gated content out of SFW channels.
- **Quote** — Right-click any message to render it as a styled quote card over the author's avatar, with theme and font pickers. Post it publicly and the bot auto-reacts so great quotes can reach the starboard themselves.
- **Auto-react** — Automatically drop chosen emoji on images and embeds in configured channels. A frictionless nudge that gets visual content the engagement it deserves.
- **Needle (auto-thread)** — Automatically spawn a thread from each new message in designated channels, with custom thread names, welcome messages, and status-reaction tracking. Keeps Q&A and discussion channels tidy at a glance.
- **Bios** — Members build rich, multi-field profiles through an interactive wizard. Finished bios live as persistent cards in a dedicated channel so the community can get to know each other.
- **Emoji Stealer** — Right-click a message or paste an image URL to upload it as a custom emoji to one of your servers. Build out your emoji library without ever leaving Discord.
- **Bump Tracker** — Track cooldowns for listing sites like DISBOARD and get pinged the moment each is ready to bump again, with a live status widget. Essential for servers that grow through listing traffic.
- **Risky Rolls** — Everyone rolls 1–100; the highest unique roll asks a question and the lowest answers. Special rolls unlock variants — 69 opens a room question, 100 lets the winner pick, and 1 triggers two questioners.
- **Guess** — Consenting members submit an NSFW image that the bot auto-crops with face-excluding AI detection, and the community guesses the submitter from a tight crop. All-time leaderboards track submitters, guessers, accuracy, and the hardest crops.

### Onboarding & community
- **Role grants** — `/grant role:<key> member:<@user>` hands out community roles through a per-role permission allowlist (e.g. greeters can grant Denizen, mods can grant NSFW/Veteran). Self-serve role-giving without handing out Manage Roles.
- **Welcome / leave** — Configurable join and leave messages, edited and previewed live from the dashboard. Make a strong first impression without redeploying.
- **Booster role buttons** — Persistent click-to-claim buttons for booster perks that survive restarts. Set them up once and they keep working.
- **Birthday** — Members record their birthday with `/birthday set`, and the bot posts a daily celebration in a configured channel. The message template is customizable and the dashboard previews the next 90 days.
- **DM permissions** — A full opt-in DM consent system: members pick Open/Ask/Closed modes, requests route through a panel and DM buttons, and acceptance records a bidirectional consent pair. Either side can revoke at any time with mutual notification.
- **Server todo** — Add tasks to a shared list with `/todo` or the "Add to Todo" message context menu. Mods curate, complete, and filter the list from the dashboard.
- **Watch list** — `/watch add @user` quietly forwards a member's public posts to your DMs. A lightweight tool for keeping an eye on a situation without a heavy moderation footprint.

### Wellness
- **Wellness Guardian** — A self-managed boundary tool: opt in, set message and voice caps, schedule blackout windows, and pair with an accountability partner. When you hit a limit the bot applies gentle friction (nudges, cooldowns, slow mode) instead of lockouts, and DMs you a supportive weekly summary.

### Setup & utilities
- **`/setup`** — First-time setup in two phases: provision every bot channel and category, then walk a wizard for mod/admin roles, jail/ticket categories, and log/transcript channels. Get a server fully wired in minutes.
- **`/help`** — A contextual command reference that only shows the sections your permissions unlock. Newcomers and mods each see exactly what's relevant to them.
- **`/invite` / `/support`** — Quick links to invite the bot and reach the support server.
- **Owner tools** — `/reload_cog` hot-reloads an extension and `/spotify_authorize` runs the one-time Spotify auth flow.

### Background services
- DB backup loop, voice-XP loop, sentiment-score backfill, message archive, health-metrics batch (15 min), and reports cache warmer (hourly) keep analytics fresh and data durable without manual intervention.

## Quick Start

```bash
python -m venv .venv
.\.venv\Scripts\activate        # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -e .
cp .env.example .env            # fill in DISCORD_TOKEN
python -m dungeonkeeper
```

For full setup instructions (bot permissions, guild configuration, production deployment) see [DEPLOYMENT.md](DEPLOYMENT.md).

## Music cog setup

The music cog runs Lavalink as a child process. One-time setup:

1. Install **Java 17 or newer** and ensure `java` is on `PATH`
   ([Temurin downloads](https://adoptium.net/temurin/releases/?version=17)).
2. Create a Spotify app at <https://developer.spotify.com/dashboard> (Client
   Credentials flow — no redirect URI needed). Copy the Client ID and Secret.
3. Run the installer to download Lavalink + LavaSrc:
   ```
   python scripts/setup_lavalink.py
   ```
4. Fill in the music section of `.env`:
   ```
   SPOTIFY_CLIENT_ID=...
   SPOTIFY_CLIENT_SECRET=...
   LAVALINK_PASSWORD=<random>
   ```
5. Start the bot — Lavalink starts automatically on cog load. If startup fails,
   the rest of the bot keeps running and music commands return "Music is
   currently unavailable."

## Environment

Required:
- `DISCORD_TOKEN` — bot token from the [Discord Developer Portal](https://discord.com/developers/applications)

Optional:
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` / `LAVALINK_PASSWORD` — music cog
- `DASHBOARD_ENABLED=1`, `DASHBOARD_HOST`, `DASHBOARD_PORT` — LAN web dashboard

## Configuration

Runtime config is stored in `dungeonkeeper.db` (`config` and `config_ids` tables).
Most settings are configured through the web dashboard after the bot is running (`DASHBOARD_ENABLED=1`).

`config` keys:
| Key | Description |
|-----|-------------|
| `debug` | `1` = guild-scoped command sync (dev), `0` = global sync (production) |
| `guild_id` | Target guild ID (required in debug mode) |
| `mod_channel_id` | Channel for moderation notifications |
| `xp_level_5_role_id` | Role granted at XP level 5 |
| `xp_level_5_log_channel_id` | Channel for level-5 milestone announcements |
| `xp_level_up_log_channel_id` | Channel for all level-up announcements |
| `greeter_role_id` | Role allowed to run `/grant` for the Denizen role |
| `denizen_role_id` | Role assigned by `/grant role:denizen` |

`config_ids` buckets:
| Bucket | Description |
|--------|-------------|
| `spoiler_required_channels` | Channels/threads enforcing spoiler images |
| `bypass_role_ids` | Roles exempt from spoiler guard |
| `xp_grant_allowed_user_ids` | Users allowed to run `/xp_give` |
| `xp_excluded_channel_ids` | Channels/threads where XP is disabled |

## Slash Commands

**General**
- `/help` — Contextual command reference (sections shown based on your permissions)
- `/invite` — Get a link to invite this bot
- `/support` — Get a link to the support Discord
- `/xp_leaderboards [timescale]` — Top XP earners by source and your standing
- `/todo <task>` — Add a task to the shared server todo list
- `/birthday set` — Record your birthday
- `/confess` — Post an anonymous confession (modal)
- `/dmrequest` — Send moderators a private DM request
- `/delete_me` — Permanently delete all your messages and data

**DM Permissions**
- `/dm_help` — Overview of the DM request system
- `/dm_set_mode` — Set your DM request mode
- `/dm_revoke` — Revoke DM permission with another user
- `/dm_status` — Check mutual DM permission status with a user

**Wellness**
- `/wellness setup` — Opt in (timezone + enforcement style)
- `/wellness away on` / `/wellness away off` — Toggle your away auto-reply

**Party Games**
- `/games play <game>` — Start a party game in an allowed channel. Games: `ffa`, `ffa_banner`, `wyr`, `nhie`, `mlt`, `mfk`, `twotruths`, `traditional` (Truth or Dare), `compliment`, `hottakes`, `story`, `ama`, `fantasies`, `price`, `rushmore`, `clapback`, `legitlibs`, `photo`
- `/recap` — Recap of the current game-night session
- `/games help` / `/games support` — Game list and support link
- *Spicier (NSFW) prompts appear only in channels an admin has marked age-restricted in Discord.*
- `/games config game-status` / `game-end` — Inspect or force-close the active game (mod)
- *The games channel allowlist and audit-log channel are managed from the web dashboard's Games Config panel.*

**Head-to-Head & Group Games**
- `/games pressure challenge @user` — Pressure Cooker duel (loser renamed 24h)
- `/games quickdraw challenge @user` — Quickdraw duel
- `/games hotpotato challenge @user` — Hot Potato duel
- `/games hotpotatogroup start` — Hot Potato group free-for-all
- `/games musicalchairs start` — Musical Chairs, 3+ players
- `/games chicken start` — Chicken, duel or group
- *Per-game settings (cooldowns, timers, player counts, channel restrictions) are managed from the web dashboard's Games nav section — one Config panel per game.*

**Content & Engagement**
- `/whisper send @user <message>` — Send an anonymous whisper (recipient gets three guesses); also `optin`, `optout`, `sent`, `forget-me`
- `/penpals join` / `/penpals leave` — Get a pen pal (matched on the spot if someone's waiting) or exit the pool; also `status`, `new-question`, `end`, plus mod `pair <user1> <user2>` and `round`
- `/bio` — Create or update your profile bio (wizard)
- `/risky start` — Open a Risky Rolls round in this channel
- `/guess submit` — Submit an image to start a Guess round; also `optin`, `confess`, `leaderboard`, `prompt`, `round` (mod), `delete`
- `/steal_emoji <url> <name>` — Add a custom emoji from an image URL; also a **Steal Emoji** message context-menu
- **Quote** — message context-menu that renders a styled quote card over the author's avatar
- `/bump status` / `/bump log` — Check bump cooldowns or record a manual bump (mod)
- *Auto-thread channels and bump-tracker sites are managed from the web dashboard.*

**Voice (your channel)**
- `/voice lock` / `/voice unlock` — Lock or unlock your channel
- `/voice hide` / `/voice unhide` — Hide or reveal your channel
- `/voice rename <name>` / `/voice limit <n>` — Rename or set user limit
- `/voice invite <member>` / `/voice kick <member>` — Manage access
- `/voice transfer <member>` / `/voice claim` / `/voice owner` — Ownership
- `/voice reset` — Reset permissions (and optionally your saved profile)
- `/voice trusted add/remove/list` — Manage your trust list
- `/voice blocked add/remove/list` — Manage your blocklist
- `/voice profile show/reset` — Inspect or reset your saved profile

**Voice Master Admin** (mod)
- `/voice-admin post-panel` — Repost the owner-control panel
- *All other admin controls (hub/category/control-channel/template/name-blocklist settings, inline-panel toggle, force-delete / force-transfer / force-clear-profile, profile inspection) are managed from the web dashboard (`/voice-master/config`, `/voice-master/name-blocklist`, `/voice-master/channels`, `/voice-master/profiles`).*

**Music**
- `/play <query>` — Play YouTube/Spotify URL or search terms
- `/skip`, `/shuffle`, `/loop <off|track|queue>`
- `/queue [page]`, `/nowplaying`, `/pause`, `/resume`, `/stop`, `/disconnect`
- `/247 <enabled> [channel]` — Toggle 24/7 mode for your voice channel (mod)
- `/247_status` — Show 24/7-enabled channels in this server

**Role Grants** (configurable allowlist)
- `/grant role:<key> member:<@member>` — Give a configured community role
- `/grant_missing role:<key> min_level:<n>` — (mod) List members past a level who are missing a configured grant role (defaults to the NSFW role, level 5); skips anyone on an active inactive-channel hold

**XP**
- `/xp_give @member` — Manually award 20 XP (mod or allowlisted users)
- *XP-excluded-channel management, history backfill, and level-review live in the web dashboard.*

**Reports** (mod)
- `/quality_leave add/remove/list` — Manage members on leave of absence
- *Member/role/engagement reports (promotion review, role growth, inactivity, activity graphs, drop-off, session/burst, interaction graphs, etc.) live in the web dashboard.*

**Watch List** (mod)
- `/watch add @user` / `/watch remove @user` / `/watch list`

**AI Moderation** (mod)
- `/ai review @user` — AI review of a member's recent activity
- `/ai channel` — AI scan of the current channel
- `/ai scan` — Run an AI moderation sweep
- `/ai query <question>` — Ask a free-form moderation question
- *Rules Watch (passive monitoring), prompt testing, and model management live in the web dashboard.*

**Jail & Tickets** (mod)
- `/setup` — First-time jail/ticket/mod setup
- `/jail @user [reason]` / `/unjail @user`
- `/pull @user` / `/remove @user` — Add/remove people in the current jail or ticket
- `/warn @user [reason]` / `/warnings @user` / `/revokewarn <id>`
- `/modinfo @user` — Full mod profile (jail, warnings, tickets)
- `/ticket panel` / `/ticket open` / `/ticket close` / `/ticket reopen` / `/ticket claim` / `/ticket escalate` / `/ticket delete`
- `/policy open` / `/policy vote` / `/policy close` / `/policy list`

**Privacy** (mod)
- `/delete_user @user` — Permanently delete all messages and data for a user

**Configuration** (mod)
- `/setup` — First-time bot setup: provision channels + walk through role/category config
- *All other settings (welcome/leave, role grants, XP logging, spoiler guard, booster roles, AI moderation & rules watch, auto-react, needle auto-thread, bump tracker, voice master, games content, channels) are managed from the web dashboard.*

**Utility** (mod)
- `/purge [count] [after]` — Delete messages by count and/or cutoff time
- `/rename <target> [new_name]` — Set a member's nickname (requires Manage Nicknames; leave `new_name` blank to reset to their username)

**Owner**
- `/reload_cog <extension>` — Hot-reload a cog
- `/spotify_authorize` — One-time Spotify private-playlist auth link

## Development

Run all checks:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
```

Set up pre-commit hooks (runs ruff, mypy, and pytest on every commit):

```powershell
.\.venv\Scripts\pre-commit.exe install
```

Run hooks across all files:

```powershell
.\.venv\Scripts\pre-commit.exe run --all-files
```

### Running the beta tools sidecar (dev only)

The sidecar drives synthetic Discord activity in the test guild for moderator
testers to exercise. It refuses to run outside `BOT_ENV=dev`.

1. Register a new Discord application "Dungeon Keeper Tools" plus 3 puppet
   apps ("Puppet Alice", "Puppet Bob", "Puppet Clara") in the Developer
   Portal. Get a bot token + bot user ID for each.
2. Invite all 4 to the test guild with the `bot` scope.
3. Fill in the new env vars in `.env` (see `.env.example`).
4. In one terminal: `BOT_ENV=dev python -m dungeonkeeper`
5. In another terminal: `BOT_ENV=dev python -m beta_tools`

Verify with `/beta-puppets-list` in the test guild — all 3 puppets should
show as connected. Use `/beta-puppets-impersonate alice #general "hello"`
to test that puppet sends are working.

See `docs/superpowers/specs/2026-04-30-beta-tools-sidecar-design.md` for the
full design.
