# Todo From Message — Context Menu

**Date:** 2026-04-30
**Status:** Design approved, awaiting implementation plan

## Summary

Add a Discord message context menu ("Apps → Add to Todo") that lets moderators turn any message into an entry on the existing server todo list, optionally attaching their own notes. The original message's author/channel/content and a jump link are preserved on the todo so it stays useful even if the message is later edited or deleted.

## Motivation

The existing `/todo` slash command requires mods to retype context from a message they want to follow up on. A right-click flow removes that friction and captures a durable reference (jump URL + content snapshot) so the todo doesn't go stale when the source message changes.

## Scope

In scope:
- Message context menu on Discord, mod-gated.
- Modal with a single optional "Notes" field.
- New `description` and `source_message_url` columns on the `todos` table.
- Read-side API support (`GET /todos` returns the new fields).
- Service-layer + cog tests.

Out of scope (future iterations):
- Dashboard UI changes to render the new fields. The API will return them; we'll wire the UI after dogfooding the feature.
- A web POST endpoint that mirrors this (the menu is Discord-only for now).
- Editing description/source after creation.

## User flow

1. A moderator right-clicks a message in Discord and picks **Apps → Add to Todo**.
2. Non-mods get an ephemeral `"Mod only."` reply and the flow ends. (Matches the gating pattern in `cogs/jail_cog.py:183`.)
3. A modal opens with one field: **Notes** (paragraph style, max 1000 chars, optional).
4. On submit, a todo is created and an ephemeral `"Todo #N added"` confirmation is sent.

### What gets stored

For a created todo:

- `task` — `"Message from @<author display name> in #<channel name>"`. Names are resolved and snapshotted at creation time, consistent with how `task` is treated as plain text today.
- `description` — message content, then a blank line, then the user's notes. Either part may be omitted:
  - If the message has no text content (embed/attachment-only), the message portion is `"[no text content]"`.
  - If the user leaves Notes blank, only the message portion is stored.
  - If both portions exist, they are joined with a single blank line between them.
- `source_message_url` — `message.jump_url`.

### Edge cases

- **Empty message content** (embed/attachment only): description starts with `"[no text content]"` so the jump link still has framing.
- **Long content**: message content over 1500 characters is truncated with `…`. The full message remains accessible via `source_message_url`.
- **DM context**: the context menu is registered on the global tree but the callback short-circuits with an ephemeral error if `interaction.guild` is `None` (mirrors the existing `/todo` guard).

## Schema

New migration: `migrations/007_todo_message_source.sql` (next available number after `006_music.sql`).

```sql
ALTER TABLE todos ADD COLUMN description TEXT;
ALTER TABLE todos ADD COLUMN source_message_url TEXT;
```

Both columns are nullable. Existing rows retain `NULL` for both — no backfill is required.

## Code changes

### `services/todo_service.py`

Extend `create_todo` with two optional keyword-only parameters:

```python
def create_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    added_by: int,
    task: str,
    *,
    description: str | None = None,
    source_message_url: str | None = None,
) -> int:
    ...
```

The INSERT is updated to include the new columns. Existing callers (`/todo` slash command in `cogs/todo_cog.py`, `POST /todos` in `web/routes/todo.py`) keep working without modification because the new args default to `None`.

### `cogs/todo_cog.py`

- Register a message context menu in `cog_load` (mirroring the pattern at `cogs/jail_cog.py:202`):
  - Name: `"Add to Todo"`.
  - Type: message context menu (`discord.AppCommandType.message`).
  - Stored on the cog instance for cleanup in `cog_unload`.
- Callback `_todo_from_message_ctx(interaction, message)`:
  1. Guard: `interaction.guild` must be present; otherwise ephemeral `"Server only."`.
  2. Mod check via `_is_mod` (the helper used in `commands/jail_commands.py:90`); on failure, ephemeral `"Mod only."` (matches `cogs/jail_cog.py:188`).
  3. `await interaction.response.send_modal(_TodoFromMessageModal(message, self.ctx))`.
- Modal `_TodoFromMessageModal(discord.ui.Modal)`:
  - Title: `"Add to Todo"`.
  - One `TextInput`: label `"Notes"`, style paragraph, max 1000, required=False, placeholder `"Optional context for this todo"`.
  - `on_submit`:
    1. Build `task` from author display name + channel name (snapshot at this moment).
    2. Build `description` per the rules above.
    3. Call `create_todo(conn, guild_id, user_id, task, description=..., source_message_url=message.jump_url)`.
    4. Send ephemeral `"Todo #{id} added"` confirmation.
- `cog_unload`: remove the context menu by name + type, matching `cogs/jail_cog.py:216`.

Helper functions for building `task` and `description` should live as module-level helpers in `cogs/todo_cog.py` (or `services/todo_service.py` if they grow) so the cog tests can call them directly without instantiating Discord objects.

### `web/routes/todo.py`

Extend the SELECT and the response dict in `list_todos` to include `description` and `source_message_url`:

```python
"SELECT id, added_by, task, description, source_message_url, "
"created_at, completed_at, completed_by FROM todos ..."
```

```python
{
    ...,
    "description": r["description"],
    "source_message_url": r["source_message_url"],
}
```

No other endpoints change.

## Tests

Existing test layout convention: no `__init__.py` in `tests/` subdirectories (per project memory).

### `tests/services/test_todo_service.py`

Add cases:
- `create_todo` with `description` + `source_message_url` persists both columns.
- `create_todo` without the new kwargs leaves both columns `NULL` (regression check for existing callers).

### `tests/cogs/test_todo_cog.py`

Focused test of the modal-submit path. Use the project's existing fixtures for fake interactions and mocked DB. Verify:
- Mod-gating: non-mod hits `"Mod only."` and `create_todo` is never called.
- Empty message content + empty notes → description is `"[no text content]"`.
- Empty message content + notes → description is `"[no text content]\n\n<notes>"`.
- Message content + empty notes → description is just the (possibly truncated) message content.
- Message content + notes → both joined with one blank line.
- Long message content (> 1500 chars) is truncated with `…`.
- `task` formatting matches `"Message from @<display> in #<channel>"`.
- `source_message_url` equals `message.jump_url`.

## Migration ordering

The new migration is additive and SQLite-safe. It can be applied independently of any code change because the new columns are nullable. Recommended apply order during deploy:

1. Run the migration.
2. Deploy the new code (cog + service + web).

If the code ships first, it still works — the columns won't exist yet but `description=None` and `source_message_url=None` defaults mean no INSERTs would actually fail until someone uses the new context menu. To avoid that ordering risk, apply the migration first.

## Risk and rollback

- **Schema** is additive. Rollback is a no-op (leave columns in place) or `ALTER TABLE todos DROP COLUMN ...` if SQLite supports it on your version.
- **Behavior** is gated behind the new context menu; the existing `/todo` slash command and dashboard are unchanged. Disabling the cog or deregistering the context menu reverts the feature.
