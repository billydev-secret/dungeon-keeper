# dungeon-keeper

Discord moderation and community utility bot with:
- XP tracking and leaderboards
- Role workflows for intake
- Spoiler guard channel controls
- Auto-delete schedules with DB-tracked message queues

## Run

Required environment variable:
- `DISCORD_TOKEN`

Start:

```powershell
.\.venv\Scripts\python.exe .\dungeonkeeper.py
```

## Configuration

Runtime config is stored in `dungeonkeeper.db` (`config` and `config_ids` tables).

Common keys in `config`:
- `debug` (`0`/`1`)
- `guild_id`
- `mod_channel_id`
- `xp_level_5_role_id`
- `xp_level_5_log_channel_id`
- `xp_level_up_log_channel_id`
- `greeter_role_id`
- `denizen_role_id`

Common buckets in `config_ids`:
- `spoiler_required_channels`
- `bypass_role_ids`
- `xp_grant_allowed_user_ids`
- `xp_excluded_channel_ids`

## Slash Commands

General:
- `/help` - Show command reference and examples.
- `/xp_leaderboards` - Show top 5 XP earners for a timescale, plus caller standing.

Moderation:
- `/listrole` - List members who currently have a role.
- `/inactive_role` - Report role members inactive for N days.

Denizen:
- `/grant_denizen` - Grant the configured Denizen role to a member.
- `/set_greeter_role` - Set the role allowed to run `/grant_denizen`.
- `/set_denizen_role` - Set the role that `/grant_denizen` assigns.

XP:
- `/xp_give` - Grant manual XP to one member.
- `/xp_give_allow` - Allow a user to run `/xp_give`.
- `/xp_give_disallow` - Remove a user from `/xp_give` access.
- `/xp_give_allowed` - List users currently allowed to run `/xp_give`.
- `/xp_set_levelup_log_here` - Set this channel/thread for level-up announcements.
- `/xp_set_level5_log_here` - Set this channel/thread for level 5 announcements.
- `/xp_exclude_here` - Disable XP gain in this channel/thread.
- `/xp_include_here` - Re-enable XP gain in this channel/thread.
- `/xp_excluded_channels` - List channels/threads where XP gain is disabled.
- `/xp_backfill_history` - Backfill historical message XP into the database.

Spoiler guard:
- `/spoiler_guard_add_here` - Enable spoiler guard in this channel/thread.
- `/spoiler_guard_remove_here` - Disable spoiler guard in this channel/thread.
- `/spoiler_guarded_channels` - List channels/threads where spoiler guard is enabled.

Auto-delete:
- `/auto_delete` - Delete older messages now and optionally set recurring cleanup.
- `/auto_delete_configs` - List auto-delete schedules configured for this server.

Notes:
- `/auto_delete` can run once immediately and/or set recurring schedule.
- Recurring runs delete tracked messages posted after the rule is enabled.

## Development Checks

Run all checks manually:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
```

Set up git pre-commit hooks:

```powershell
.\.venv\Scripts\pre-commit.exe install
```

Run hooks across all files:

```powershell
.\.venv\Scripts\pre-commit.exe run --all-files
```
