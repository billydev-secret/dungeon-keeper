# dungeon-keeper

Discord moderation and community utility bot with:
- XP tracking and leaderboards
- Role workflows for new member intake
- Spoiler guard channel controls
- Auto-delete schedules with DB-tracked message queues

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

## Environment

Required:
- `DISCORD_TOKEN` — bot token from the [Discord Developer Portal](https://discord.com/developers/applications)

## Configuration

Runtime config is stored in `dungeonkeeper.db` (`config` and `config_ids` tables).
Most settings are configured through slash commands after the bot is running.

`config` keys:
| Key | Description |
|-----|-------------|
| `debug` | `1` = guild-scoped command sync (dev), `0` = global sync (production) |
| `guild_id` | Target guild ID (required in debug mode) |
| `mod_channel_id` | Channel for moderation notifications |
| `xp_level_5_role_id` | Role granted at XP level 5 |
| `xp_level_5_log_channel_id` | Channel for level-5 milestone announcements |
| `xp_level_up_log_channel_id` | Channel for all level-up announcements |
| `greeter_role_id` | Role allowed to run `/grant_denizen` |
| `denizen_role_id` | Role assigned by `/grant_denizen` |

`config_ids` buckets:
| Bucket | Description |
|--------|-------------|
| `spoiler_required_channels` | Channels/threads enforcing spoiler images |
| `bypass_role_ids` | Roles exempt from spoiler guard |
| `xp_grant_allowed_user_ids` | Users allowed to run `/xp_give` |
| `xp_excluded_channel_ids` | Channels/threads where XP is disabled |

## Slash Commands

**General** (everyone)
- `/help` — Contextual command reference (sections shown based on your permissions)
- `/xp_leaderboards [timescale]` — Top 5 XP earners per source for a time window, plus your standing
- `/activity [resolution] [member] [channel]` — Bar chart of message volume over time
- `/session_burst [member]` — How active a member is after returning from a 20-min break
- `/connection_web [member] [timescale] [...]` — Reply/mention network graph

**Role Grants** (greeters and mods)
- `/grant_denizen @member` — Grant the Denizen role
- `/grant_nsfw @member` — Grant the NSFW role
- `/grant_veteran @member` — Grant the Veteran role

**XP**
- `/xp_give @member` — Manually award 20 XP (mod or allowlisted users)
- `/xp_leaderboards [timescale]` — Leaderboard by source and time window
- `/xp_excluded_channels` — List channels where XP is disabled (mod)
- `/xp_backfill_history [days]` — Scan message history to fill XP gaps (mod)
- `/xp_level_review [level]` — Histogram of time-to-reach for a given level (mod)

**Reports** (mod)
- `/report list_role @role` — List every current holder of a role
- `/report inactive_role @role [days]` — Role members inactive for N days
- `/report inactive [time_period]` — All server members inactive for a given period
- `/report oldest_sfw [count]` — Members without NSFW access, ranked by last post date

**Activity & Graphs** (mod)
- `/dropoff [period] [limit] [channel]` — Members with the largest message-rate drop
- `/burst_ranking [limit]` — Who most reliably drives conversation after a break
- `/connection_web [member] [timescale] [min_pct] [layers] [limit] [spread] [max_per_node]` — Interaction network graph
- `/interaction_scan [days] [reset]` — Backfill the interaction graph from message history
- `/chilling_effect [...]` — Members whose arrival correlates with others going quiet

**Watch List** (mod)
- `/watch add @user` — Start forwarding a member's public posts to your DMs
- `/watch remove @user` — Stop watching a member
- `/watch list` — Show members you are currently watching

**AI Moderation** (mod, requires `OPENAI_API_KEY`)
- `/ai review @user [days]` — AI review of a user's recent messages for concerns
- `/ai scan [count]` — AI scan of recent messages in this channel
- `/ai channel [question] [minutes] [channel]` — Ask the AI about a channel's recent activity
- `/ai query @user [question] [days]` — Ask the AI a specific question about a user

**Configuration** (mod)
- `/config welcome` — Set welcome/leave channels and message templates
- `/config roles` — Configure Greeter, Denizen, NSFW, and Veteran role grant settings
- `/config xp` — Set XP log channels, toggle channel XP, manage the `/xp_give` allowlist
- `/config prune` — Set up or disable the inactivity prune schedule
- `/config spoiler` — Toggle spoiler guard on channels
- `/welcome_preview` / `/leave_preview` — Preview welcome/leave message templates

**Inactivity Prune** (mod)
- `/inactivity_prune status` — Show current config and exemption list
- `/inactivity_prune exempt @member` — Protect a member from being pruned
- `/inactivity_prune unexempt @member` — Remove a member's exemption
- `/inactivity_prune run` — Trigger an immediate prune run

**Utility** (mod)
- `/purge [count] [after]` — Delete messages by count, cutoff time (e.g. `after:19:35` UTC), or both

**Auto-Delete** (mod)
- `/auto_delete [del_age] [run]` — Delete old messages and optionally set a recurring schedule
- `/auto_delete_configs` — List all auto-delete schedules for this server

Duration syntax for `del_age` and `run`: compound units like `30d`, `1h30m`, `7d12h`, or `once` / `off`.

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
