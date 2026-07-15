# Mod — Feature Spec

General moderation and help commands (`ModCog`, `src/bot_modules/cogs/mod_cog.py`): the server-facing `/help` browser and the `/purge` bulk-delete tool. Distinct from `docs/tools_spec.md`, which covers the owner/utility ToolsCog (`/reload_cog`, `/spotify_authorize`, `/support`).

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/help` | Slash | None | Browse all commands, organized by category, in an ephemeral embed |
| `/purge count:<1–1000> after:<HH:MM>` | Slash | Moderate Members (default perms) **and** `ctx.is_mod` | Bulk-delete messages in the current channel by count and/or time |

## Behaviour

### `/help`
Ephemeral embed with a section dropdown. Pages are a **hand-curated** list (not auto-generated), shown conditionally by the invoker's permissions:

- **General** — always shown (public commands: XP, tickets, birthday, wellness, DMs, pen pals, whisper, etc.).
- **Role Grants** — only if the invoker can grant at least one configured grant role; one `/grant` entry per role in the guild config.
- **XP Grant** — only if the invoker passes `can_use_xp_grant`.
- **Moderation** — only if `ctx.is_mod` (lists `/purge`, `/rename`).
- **Voice, Music, Whisper, Image Guessing Games, Games Night** — always shown.

A **Browse by Module** button opens a second, auto-generated pager: one page per loaded cog, listing its registered app commands (groups flattened to `/group sub [subsub]`), sorted alphabetically. Bodies over 4000 chars are truncated with an ellipsis.

Embeds use the guild accent colour (`resolve_accent_color`) when invoked in a guild; per-section decorative colours are the DM fallback. Both views time out after 120 s (controls disabled); only the invoker can interact — others' clicks are silently ignored.

Because the curated pages are hand-maintained, they can drift from the real command set; the module browser is always accurate.

### `/purge`
Both arguments optional:

- `count` — delete the most recent N messages (1–1000).
- `after` — `HH:MM` or `HH:MM:SS` in **server time** (guild `tz_offset`, global fallback if unset); deletes messages from that time onward. A time later than now is interpreted as *yesterday* at that time.
- Both — up to `count` messages since `after`.
- Neither — purges the **entire channel**.

Runtime gate is `ctx.is_mod` even though Discord's default-permission gate is Moderate Members. Works only in text channels and threads; the bot needs **Manage Messages** there. Deletion runs in batches of 100 via `channel.purge`, pausing `bulk_delete_pause_seconds` (1.1 s) between batches to soften rate limits. The ephemeral follow-up reports the deleted count and scope, e.g. `Deleted 42 messages (last 50 since 19:35).`

## User-visible errors

| When | The user sees |
|---|---|
| Non-mod invokes `/purge` | "You don't have permission to use this command." |
| `after` fails to parse | "Invalid time format. Use `HH:MM` or `HH:MM:SS` (server time is UTC±N), e.g. `19:35`." |
| Invoked outside a text channel/thread | "This command only works in text channels and threads." |
| Bot lacks Manage Messages in the channel | "I need the **Manage Messages** permission in this channel to delete messages." |

## Configuration

- Guild `tz_offset` (via `get_tz_offset_hours`) — interprets `/purge after:` times; falls back to the global offset when the guild has no row.
- `AUTO_DELETE_SETTINGS.bulk_delete_pause_seconds` (1.1 s) — pause between purge batches.
- Guild grant-role config and mod/XP-grant permission checks (`AppContext`) — decide which `/help` sections appear.

## Stored data

None. The cog only reads the timezone offset; nothing is written.
