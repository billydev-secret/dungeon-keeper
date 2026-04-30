# dungeon-keeper

Discord moderation, community, voice, and analytics bot.

## Features

### Moderation & safety
- **Jail** — send a member to a private channel with role stripping and restoration on release (`/jail`, `/unjail`); pull/remove others into the jail or ticket conversation.
- **Tickets** — panel-button-driven private support tickets with `/ticket panel`, claim, escalate, close, reopen, and message context-menu opening.
- **Warnings** — `/warn`, `/warnings`, `/revokewarn`, plus `/modinfo` for a full per-member mod profile (jail history, warnings, tickets).
- **Policies** — collaborative policy proposals with open/vote/close/list flow.
- **AI moderation** (optional, requires `ANTHROPIC_API_KEY`) — Claude-powered `/ai review`, `/ai scan`, `/ai channel`, `/ai query`.
- **Purge** — bulk-delete by count and/or cutoff time.
- **Privacy** — members can erase all their own data with `/delete_me`; mods can purge a user with `/delete_user`.

### XP & analytics
- **XP system** — earn XP from text, voice, replies, and image posts; configurable level milestones grant roles; channel exclusions; history backfill; level-up + level-5 announcements.
- **Leaderboards** — `/xp_leaderboards` by source and time window with your own rank.
- **Activity graphs** — `/activity`, `/dropoff`, `/session_burst`, `/burst_ranking`.
- **Reports** — `/report promotion_review`, `/quality_leave add/remove/list` for tracking members on leave.
- **Optional web dashboard** — opt-in LAN dashboard (`DASHBOARD_ENABLED=1`) with cached metrics: DAU/MAU, heatmap, channel health, Gini, social graph, sentiment, newcomer funnel, cohort retention, churn risk, mod workload, incidents, NSFW gender distribution, and more. Cache is pre-warmed every hour and refreshed every 15 min.

### Voice
- **Voice Master** — click a hub channel to spawn your own voice channel; lock/unlock, hide/unhide, rename, user-limit, invite, kick, transfer, claim, owner. Persistent per-user profiles with trust list, block list, knock-to-join, and admin hub/category/template/name-blocklist configuration.
- **Music** — YouTube and Spotify playback via Lavalink: `/play`, `/skip`, `/shuffle`, `/loop`, `/queue`, `/pause`, `/resume`, `/stop`, `/nowplaying`, `/disconnect`. Mod-only `/247` keeps the bot in a voice channel indefinitely.
- **TTS** — `/tts` speaks text in your voice channel.

### Onboarding & community
- **Role grants** — `/grant role:<key> member:<@user>` with per-role permission allowlist (e.g. greeters can grant Denizen, mods can grant NSFW/Veteran).
- **Welcome / leave** — configurable templates with `/welcome_preview` and `/leave_preview`.
- **Booster role buttons** — persistent click-to-claim buttons that survive restarts.
- **Birthday** — `/birthday set` records a member's birthday.
- **Confessions** — anonymous `/confess` modal posting to a configured channel.
- **DM requests** — `/dmrequest` notifies mods; full opt-in DM permission system (`/dm_set_mode`, `/dm_status`, `/dm_revoke`, `/dm_help`, `/dm_request_panel_refresh`).
- **Starboard** — configurable channel, emoji, threshold, exclusion list, and on/off toggle.
- **Server todo** — `/todo` adds tasks to a shared list.
- **Watch list** — `/watch add` forwards a member's public posts to your DMs.

### Wellness
- **Wellness Guardian** — opt-in via `/wellness setup` (timezone + enforcement mode: gentle / cooldown / slow-mode / gradual). Background tick + active-list + weekly-report loops. Per-user `/away on` and `/away off` auto-reply when mentioned.

### Setup & utilities
- **`/init`** — provision all bot channels and categories.
- **`/setup`** — first-time jail role + channels + mod config.
- **`/help`** — contextual command reference, scoped to your permissions.
- **`/invite`** / **`/support`** — bot invite link and support server link.
- **`/reload_cog`** / **`/spotify_authorize`** — owner-only dev commands.

### Background services
- DB backup loop, voice-XP loop, sentiment-score backfill, health-metrics batch (15 min), reports cache warmer (hourly).

## Quick Start

```bash
python -m venv .venv
.\.venv\Scripts\activate        # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -e .
cp .env.example .env            # fill in DISCORD_TOKEN
.\.venv\Scripts\python.exe .\dungeonkeeper.py
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
- `ANTHROPIC_API_KEY` — enables `/ai *` moderation commands
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` / `LAVALINK_PASSWORD` — music cog
- `DASHBOARD_ENABLED=1`, `DASHBOARD_HOST`, `DASHBOARD_PORT` — LAN web dashboard

## Configuration

Runtime config is stored in `dungeonkeeper.db` (`config` and `config_ids` tables).
Most settings are configured through slash commands after the bot is running, primarily via `/config`.

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
- `/activity [resolution] [member] [channel]` — Bar chart of message volume over time
- `/session_burst [member]` — How active a member is after returning from a break
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
- `/dm_request_panel_refresh` — Repost the DM request panel (mod)

**Wellness**
- `/wellness setup` — Opt in (timezone + enforcement style)
- `/wellness away on` / `/wellness away off` — Toggle your away auto-reply

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
- `/voice-admin set-hub`, `/voice-admin set-category`, `/voice-admin set-control-channel`
- `/voice-admin post-panel`, `/voice-admin post-inline-panel`
- `/voice-admin set-default-name`, `/voice-admin set-int`
- `/voice-admin disable-saves`, `/voice-admin saveable-fields`
- `/voice-admin name-blocklist add/remove/list`
- `/voice-admin force-delete`, `/voice-admin force-transfer`, `/voice-admin force-clear-profile`, `/voice-admin view-profile`
- `/voice-admin show` — Show full Voice Master configuration

**Music**
- `/play <query>` — Play YouTube/Spotify URL or search terms
- `/skip`, `/shuffle`, `/loop <off|track|queue>`
- `/queue [page]`, `/nowplaying`, `/pause`, `/resume`, `/stop`, `/disconnect`
- `/247 <enabled> [channel]` — Toggle 24/7 mode for your voice channel (mod)
- `/247_status` — Show 24/7-enabled channels in this server

**TTS**
- `/tts <text>` — Speak text in your voice channel

**Role Grants** (configurable allowlist)
- `/grant role:<key> member:<@member>` — Give a configured community role

**XP**
- `/xp_give @member` — Manually award 20 XP (mod or allowlisted users)
- `/xp_excluded_channels` — List channels where XP is disabled (mod)
- `/xp_backfill_history [days]` — Scan message history to fill XP gaps (mod)
- `/xp_level_review [level]` — Histogram of time-to-reach for a given level (mod)

**Reports** (mod)
- `/report promotion_review` — Promotion candidate analysis
- `/quality_leave add/remove/list` — Manage members on leave of absence

**Activity & Graphs** (mod)
- `/dropoff [period] [limit] [channel]` — Members with the largest message-rate drop
- `/burst_ranking [limit]` — Who most reliably drives conversation after a break

**Watch List** (mod)
- `/watch add @user` / `/watch remove @user` / `/watch list`

**AI Moderation** (mod, requires `ANTHROPIC_API_KEY`)
- `/ai review @user [days]` — AI review of a user's recent messages
- `/ai scan [count]` — AI scan of recent messages in this channel
- `/ai channel [question] [minutes] [channel]` — Ask the AI about a channel's recent activity
- `/ai query @user [question] [days]` — Ask the AI a specific question about a user

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

**Starboard** (mod)
- `/starboard channel/threshold/emoji/toggle/exclude/unexclude/status`

**Configuration** (mod)
- `/config` — Open the unified settings panel for any feature
- `/init` — Provision all bot channels/categories (creates anything missing)
- `/welcome_preview` / `/leave_preview` — Preview welcome/leave templates

**Utility** (mod)
- `/purge [count] [after]` — Delete messages by count and/or cutoff time

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
