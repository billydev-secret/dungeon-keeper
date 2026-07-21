# Todo — Feature Spec

Shared per-guild todo list. Two Discord entry points (a slash command and a message context menu) feed the same list; mods curate it from the web dashboard.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/todo task:<text>` | Slash | Moderator (server only) | Add a free-form task to the guild's todo list |
| `Add to Todo` | Message context menu | Moderator (server only) | Capture a Discord message as a todo entry with optional notes |
| Web `GET /api/todos?status=pending\|completed` | Web (dashboard) | Mod | List todos (newest first, capped at 200) |
| Web `POST /api/todos` | Web (dashboard) | Mod | Create a free-form todo as the authenticated user |
| Web `POST /api/todos/{id}/complete` | Web (dashboard) | Mod | Mark a todo complete |

The todo list is a moderator worklist end to end: the Discord entry points and the web endpoints are all gated to moderators. `/todo` uses the same `has_mod_or_admin_permissions` rule as the other mod-tier commands (administrator, manage_guild, or manage_channels).

## Behavior

### `/todo`

Strips whitespace, rejects empty input, rejects task text longer than 500 characters, otherwise adds the todo and replies ephemerally with the new id.

### `Add to Todo` (context menu)

Opens a modal with one optional **Notes** field (max 1 000 chars). On submit, the todo is created with:

- **Headline** — `"Message from @{author} in #{channel}"`, truncated to 500 chars.
- **Description** — the source message content (stripped, truncated to 1 500 chars), followed by the notes if any. If the source message has no text content, the description starts with the literal marker `[no text content]` so the jump link still has framing.
- **Source URL** — the Discord jump link to the source message.

Reply is ephemeral with the new id.

Both flows reject DMs.

### Web list

The dashboard shows pending and completed lists for the active guild. Names are resolved against the active guild. Completion records the moderator who clicked complete.

## Permissions

- Discord: moderator-gated. `/todo` and `Add to Todo` require administrator, manage_guild, or manage_channels; both reject DMs.
- Web: every endpoint requires the `moderator` perm.

## User-visible errors

| When | The user sees |
|---|---|
| Used in DMs | "Server only." |
| Used by a non-moderator | "Only moderators can add to the todo list." |
| Task is empty after stripping | "Task cannot be empty." |
| Task is longer than 500 characters | "Task must be 500 characters or fewer." |
| Web completion targets a missing or already-completed row | HTTP 404: "Todo not found or already completed." |

## Non-goals

- **No descriptions / source URLs via slash.** `/todo` only carries the headline. The context menu is the only path that fills description and source URL.
- **No assignees, priorities, due dates, or labels.** The list is intentionally flat.
- **No editing.** A todo can be created and completed, never amended.
- **No DM access.** Both Discord entry points short-circuit on DM use.
- **No notifications.** Mentions in descriptions don't ping; completion doesn't DM the creator.

## Configuration

None. Behavior is fixed; the only per-guild scoping is on guild id.

## Stored data

One table, per-guild. Each row holds the headline, optional description and source URL, creator id, creation timestamp, and (once completed) completion timestamp and completer id. No per-user PII beyond Discord ids.
