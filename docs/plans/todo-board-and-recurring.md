# Todo Board + Recurring Tasks — implementation plan

Extends the existing Todo feature (`docs/todo_spec.md`) with three things:

1. A **persistent, auto-updating board** posted in a Discord channel that stays
   at the bottom of that channel.
2. **Add / Complete buttons** on the board so mods curate the list without
   leaving Discord.
3. **Recurring task definitions** on the dashboard ("Post QOTD" daily, "Photo
   challenge prompt" weekly) that materialise a todo row when they come due.

Recurring entries are **reminders only** — the bot never posts QOTD or the photo
prompt on their behalf. A due entry spawns a normal todo row; a mod does the
thing in Discord and ticks it off. (Photo Challenge already has real automation
of its own at `/api/photo-challenge`; this is the checklist, not a second copy
of that.)

## Stage 1 — schema

`src/migrations/134_todo_board.sql`:

- `todo_board(guild_id PK, channel_id, message_id, updated_at)` — sticky
  placement, `0` meaning "not posted". Own table rather than `config` KV so the
  two ids stay a single atomic row.
- `todo_recurring(id, guild_id, task, description, recurrence, time_of_day,
  recur_days, status, next_run_at, last_run_at, last_status, created_by,
  created_at)` — column names and semantics deliberately mirror
  `games_scheduled` so `compute_next_run` can be reused verbatim: `time_of_day`
  is minutes since guild-local midnight, `recur_days` is a JSON weekday set
  (Mon=0), `next_run_at` is a derived UTC-epoch cache.
- `todos.recurring_id` — provenance, so the board can mark spawned rows 🔁 and
  the spawner can tell whether the last instance is still outstanding.

Only `daily` and `weekly` are offered. A one-shot todo is just a todo.

## Stage 2 — service layer

**`services/todo_service.py`** (grows): `list_todos`, `complete_todo`,
`pending_todos`, plus `get_board` / `save_board` / `clear_board`. The SQL
currently inlined in `routes/todo.py` moves here so the board renderer and the
route share one query.

**`services/todo_recurring_service.py`** (new): frozen `RecurringTask`
dataclass, CRUD (`create/list/get/update/delete/set_status`), and the tick:

```
spawn_due(conn, *, now_ts, offset_hours_for) -> list[SpawnResult]
```

Pure-ish and `now_ts`-injected so it unit-tests without sleeping.

**Dedup rule — skip-if-pending.** If the previous instance spawned from a
recurring entry is still pending, the new occurrence does **not** create a
second row; `next_run_at` advances and `last_status` records
`skipped_pending`. Otherwise "Post QOTD" stacks five deep over a quiet week.
The one surviving row simply reads as increasingly overdue, which is the signal
you actually want.

`compute_next_run(..., after=)` already advances past many missed occurrences to
the next future slot, so a bot that was down for three days spawns one row on
boot, not three.

## Stage 3 — the board (bot side)

`cogs/todo_cog.py` grows the perk-shop sticky pattern, which is the house
standard (`economy_cog._place_shop_panel` + `should_restick_guide`):

- `_place_board(guild, target)` — per-guild `asyncio.Lock`, re-reads stored ids
  *inside* the lock, deletes the old message, posts fresh, records the new id in
  the TTL cache *before* the DB-save await so its own gateway event is skipped.
- `on_message` listener → `should_restick_guide(...)` → 6s debounce → repost.
  Reuses the existing predicate rather than a fourth copy of the same logic.
- `refresh_board(guild_id)` — in-place `message.edit(...)`, called after every
  task mutation. Guarded by a rendered-content signature so an unchanged board
  costs no API call.
- `todo_board_loop(bot)` — 60s startup task registered in `__main__.py`: spawns
  due recurring tasks, then refreshes any board whose signature changed. This is
  also what picks up dashboard-side edits.

`TodoBoardView` — `timeout=None`, static custom_ids (`todo_board_add`,
`todo_board_complete`), re-registered in `cog_load` via `add_view`. Both buttons
are moderator-gated with the same `has_mod_or_admin_permissions` rule the slash
command uses. Add opens a modal; Complete opens an ephemeral select of pending
tasks.

No new slash command — per CLAUDE.md, where the board lives is dashboard
configuration.

## Stage 4 — dashboard

`routes/todo.py` grows:

| Route | Perm | Purpose |
|---|---|---|
| `GET /api/todos` | mod | existing list + new `board` block + `can_manage_board` |
| `PUT /api/todos/board` | **admin** | post / move / unpost (`channel_id: "0"`) |
| `GET /api/todos/recurring` | mod | list + `tz_offset_hours` |
| `POST/PUT/DELETE /api/todos/recurring[/{id}]` | mod | CRUD |
| `POST /api/todos/recurring/{id}/pause\|resume\|run-now` | mod | row actions |

Board placement is admin (it makes the bot post into an arbitrary channel);
tasks and recurring entries stay moderator, matching the existing "moderator
worklist end to end" contract. The panel disables the board card for
non-admins off `can_manage_board`.

`panels/todo.js` gains a Board card (channel picker + Post/Remove) and a
Recurring card (rows with cadence, weekday chips, time-of-day, pause/resume,
edit, delete + an add form), following the Bump Tracker / Announcements
row-list pattern.

## Stage 5 — docs + tests

- `docs/todo_spec.md` rewritten; `docs/INDEX.md` one-liner updated.
- `manual.html` gets a Todo section (the feature has none today) + a row in
  `help-sections.js`, and the nav item gains `help: "help-todo"`.
- README feature bullet updated. No slash-command changes.
- Tests: `test_todo_service.py` extended; `test_todo_recurring_service.py` new
  (spawn, skip-if-pending, weekly day selection, paused rows, missed-occurrence
  catch-up); `test_todo_cog.py` extended for board render + the mod gate on both
  buttons; `tests/web/test_todo_routes.py` new.
