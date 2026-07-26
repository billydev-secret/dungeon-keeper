# Todo — Feature Spec

Shared per-guild todo list for the mod team. Tasks arrive from a slash command,
from a sticky Discord board, from the web dashboard, or from a recurring
schedule; they all land in the same list, curated from the dashboard.

## Surfaces

| Surface | Type | Permission | Purpose |
|---|---|---|---|
| `/todo task:<text>` | Slash | Moderator (server only) | Add a free-form task |
| Board **➕ Add Task** | Button + modal | Moderator | Add a task with optional notes |
| Board **✅ Complete** | Button + select | Moderator | Tick one or several tasks off |
| `GET /api/todos` | Web | Mod | List todos (newest first, capped at 200) + board placement |
| `POST /api/todos` | Web | Mod | Create a free-form todo as the authenticated user |
| `POST /api/todos/{id}/complete` | Web | Mod | Mark a todo complete |
| `PUT /api/todos/board` | Web | **Admin** | Post / move / remove the Discord board |
| `GET/POST /api/todos/recurring` | Web | Mod | List / create recurring definitions |
| `PUT/DELETE /api/todos/recurring/{id}` | Web | Mod | Edit / delete a definition |
| `POST /api/todos/recurring/{id}/{pause,resume,run-now}` | Web | Mod | Row actions |

The list is a moderator worklist end to end. `/todo` and both board buttons use
the same `has_mod_or_admin_permissions` rule as the other mod-tier commands
(administrator, manage_guild, or manage_channels).

Board *placement* is the one admin-gated action: choosing a channel makes the
bot post into it, which is server configuration rather than worklist curation.

## Behavior

### `/todo`

Strips whitespace, rejects empty input, rejects task text longer than 500
characters, otherwise adds the todo and replies ephemerally with the new id.
Rejects DMs. Refreshes the board if one is posted.

### The board

A single message the bot keeps at the **bottom of a configured channel**.

- **Contents.** Pending tasks only, **oldest first** — the longest-waiting task
  sits at the top where it nags. Each row is one padded monospace cell
  (`` `#12  Post QOTD` ``) followed by a live `<t:…:R>` age outside the code
  span, per `docs/embed_style_guide.md`. Rows spawned by a recurring definition
  carry a 🔁 marker. Capped at 15 rows, then "…and **N** more on the dashboard".
  Accent from `resolve_accent_color`.
- **Staying at the bottom.** A member message in the board's channel arms a 6s
  debounce, then the board is deleted and re-posted (Discord has no reorder
  API). Reuses `economy.guide.should_restick_guide` — bot messages are filtered
  by the caller so a repost can't self-loop. Placement is serialised per guild
  by an `asyncio.Lock`, and the stored ids are re-read *inside* the lock so a
  racing post can't orphan the live board.
- **Staying current.** Task mutations from Discord refresh the board
  immediately; dashboard mutations refresh best-effort and are otherwise picked
  up by the 60s loop. An edit is skipped when the rendered signature is
  unchanged — ages tick client-side, so "2h → 3h" costs no API call.
- **Self-healing.** If the board message is deleted by hand, the next refresh
  re-posts it rather than going quietly dead.

The view is a static-`custom_id` persistent view (`todo_board_add`,
`todo_board_complete`) re-registered in `cog_load`, so buttons survive restarts.

### Recurring tasks

Definitions live on the dashboard and materialise a normal todo row when due.

- **Reminders, not automation.** The bot adds "Post QOTD" to the list; a mod
  posts the QOTD and ticks it off. The bot never performs the chore. (Photo
  Challenge has real automation of its own — see `photo_challenge_spec.md`;
  this is the checklist, not a second copy of it.)
- **Cadence.** `daily` or `weekly` only, at a `time_of_day` in minutes since
  guild-local midnight; weekly carries a JSON weekday set (Mon=0). A one-shot
  task is just a task. Column names mirror `games_scheduled` and the time math
  is `scheduled_games_service.compute_next_run`, so the two can't drift.
  Guild-local time comes from the fixed `tz_offset_hours` (no DST).
- **Skip-if-pending.** If the previous instance is still outstanding, the new
  occurrence does *not* create a second row — `next_run_at` advances and
  `last_status` records `skipped_pending`. A chore nobody did all week reads as
  one ageing task rather than seven identical ones.
- **Catch-up.** `compute_next_run(after=…)` advances past every missed slot to
  the next future one, so three days of downtime spawns one row on boot.
- **Resume** recomputes `next_run_at` from now, so a long pause doesn't come
  back and immediately fire a stale slot.
- **Run now** routes through the same due-window the loop uses, so
  skip-if-pending and the advance behave identically to a natural fire.
- **Delete** stops the repeat but leaves any already-spawned row on the list —
  that's real outstanding work, and silently removing a task a mod is part-way
  through would be worse than orphaning it.

### Web list

The dashboard shows pending and completed lists for the active guild, plus the
board card and the recurring-task table. Names are resolved against the active
guild. Completion records the moderator who clicked complete.

## Permissions

- Discord: moderator-gated. `/todo` and both board buttons require
  administrator, manage_guild, or manage_channels; `/todo` rejects DMs.
- Web: every endpoint requires `moderator`; `PUT /api/todos/board` additionally
  requires `admin`. The panel disables the board card off `can_manage_board`.

## User-visible errors

| When | The user sees |
|---|---|
| `/todo` used in DMs | "Server only." |
| Used by a non-moderator | "Only moderators can add to the todo list." |
| Board button clicked by a non-moderator | "Only moderators can manage the todo list." |
| Task is empty after stripping | "Task cannot be empty." |
| Task is longer than 500 characters | "Task must be 500 characters or fewer." |
| Web completion targets a missing or already-completed row | HTTP 404: "Todo not found or already completed." |
| Board channel doesn't exist in the guild | HTTP 400: "That channel doesn't exist here." |
| Bot can't post in the chosen channel | HTTP 400: "I can't post in that channel — check my Send Messages and Embed Links permissions." |
| Board action while the bot is disconnected | HTTP 503: "The bot isn't connected right now — try again in a moment." |
| Weekly recurrence with no days | HTTP 400: "Pick at least one day of the week." |
| Recurrence other than daily/weekly | HTTP 400: "Repeat must be daily or weekly." |
| Time of day out of range | HTTP 400: "Time of day must be between 00:00 and 23:59." |
| Run now while a copy is still pending | 200 with "That task is already on the list — nothing new added." |

## Non-goals

- **No automation of the chore itself.** A recurring entry never posts the
  QOTD, the photo prompt, or anything else — it only adds the reminder.
- **No assignees, priorities, due dates, or labels** on individual tasks. The
  list is intentionally flat; recurrence lives on the definition, not the row.
- **No editing a task.** A todo can be created and completed, never amended.
  (Recurring *definitions* are editable.)
- **No deleting a task.** Completion is the only way off the board, so the
  record of who did what survives.
- **No sub-daily cadence.** The loop ticks every 60s; anything finer belongs in
  a real scheduler, not a mod checklist.
- **No notifications.** Mentions in descriptions don't ping; completion doesn't
  DM the creator; a due recurring task doesn't ping anyone.
- **No in-Discord configuration.** Board placement and recurring definitions are
  dashboard-only, per the project's configuration rule.

## Stored data

Three tables, all per-guild:

- `todos` — headline, optional description and source URL, creator id, creation
  timestamp, completion timestamp + completer id, and `recurring_id`
  provenance. No per-user PII beyond Discord ids.
- `todo_board` — `(guild_id PK, channel_id, message_id, updated_at)`; zeroes
  mean "not posted".
- `todo_recurring` — definition, cadence, `next_run_at` cache, `status`,
  `last_run_at`/`last_status`.

## Notes / history

- The **`Add to Todo` message context menu** described in earlier revisions of
  this spec **does not exist in the code** — no `ContextMenu` is registered, and
  nothing but the slash command, the board, and the web routes calls
  `create_todo`. The `todos.description` and `todos.source_message_url` columns
  (migration `008`) were added for it; `description` is now populated by the
  board's optional notes field and by recurring definitions, and
  `source_message_url` is currently written by nothing. Removed from the spec
  2026-07-26 rather than left standing as an aspirational claim.
