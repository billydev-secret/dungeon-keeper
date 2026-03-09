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

**General**
- `/help` — Command reference and examples
- `/xp_leaderboards` — Top 5 XP earners for a timescale, plus your standing

**Moderation**
- `/listrole` — List members who currently have a role
- `/inactive_role` — Report role members inactive for N days

**Denizen**
- `/grant_denizen` — Grant the configured Denizen role to a member
- `/set_greeter_role` — Set the role allowed to run `/grant_denizen`
- `/set_denizen_role` — Set the role that `/grant_denizen` assigns

**XP**
- `/xp_give` — Grant manual XP to a member
- `/xp_give_allow` — Allow a user to run `/xp_give`
- `/xp_give_disallow` — Remove a user from `/xp_give` access
- `/xp_give_allowed` — List users currently allowed to run `/xp_give`
- `/xp_set_levelup_log_here` — Set this channel/thread for level-up announcements
- `/xp_set_level5_log_here` — Set this channel/thread for level-5 announcements
- `/xp_exclude_here` — Disable XP gain in this channel/thread
- `/xp_include_here` — Re-enable XP gain in this channel/thread
- `/xp_excluded_channels` — List channels/threads where XP gain is disabled
- `/xp_backfill_history` — Backfill historical message XP into the database

**Spoiler Guard**
- `/spoiler_guard_add_here` — Enable spoiler guard in this channel/thread
- `/spoiler_guard_remove_here` — Disable spoiler guard in this channel/thread
- `/spoiler_guarded_channels` — List channels/threads where spoiler guard is enabled

**Auto-Delete**
- `/auto_delete` — Delete older messages now and optionally set a recurring schedule
- `/auto_delete_configs` — List auto-delete schedules configured for this server

Duration syntax for `/auto_delete`: named intervals (`daily`, `weekly`) or compound units like `30d`, `1h30m`, `7d12h`.

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
