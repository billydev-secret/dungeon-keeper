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

**Dedup rule — skip-if-pending.** *(Superseded by Stage 6's daily reset — see
below. Left as written so the reasoning that was replaced survives.)* If the
previous instance spawned from a
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

---

## Stage 6 — the mod chore board (2026-08-17)

A second sticky board scoped to recurring chores, for the mod team's own
channel. Driven by the observation that the shared list carried 17 open rows of
very mixed kinds — "fix quote bot aspect ratio", "Rotate the cloudflared tunnel
token", "more qotd prompts" — and a daily "post the QOTD" buried in that is
invisible. Mod chores and dev todos are different lists with different
audiences.

### The reset decision

Stage 2 shipped **skip-if-pending**. Stage 6 replaces it with a **true daily
reset**: an untouched instance is written off (`todos.missed_at`) when the next
occurrence comes round, and a fresh row spawns.

This was the question the whole panel hung on, because it decides what the panel
*means* — skip-if-pending makes it a list of arrears, reset makes it a daily
scoreboard — and it was put to the owner before anything was built. Reset was
chosen, for the reason that also makes the panel worth having: it produces a
record of the days a chore did **not** happen, which is the thing a mod team
actually wants to see and which skip-if-pending could not express at all.

Two deliberate asymmetries:

- **"Run now" keeps skip-if-pending.** A manual add is not a day boundary.
  Resetting there would mark the first of two button presses missed.
- **Downtime does not write off three days.** `compute_next_run(after=…)` still
  jumps to the next future slot, so a bot that was down spawns one row and
  writes off one instance. The register covers days the bot was watching, not
  days it was absent.

Rejected: a per-definition "carry over vs. reset" switch. It doubles what a
streak, a footer and a missed row each mean, for a distinction expressible by
choosing the cadence.

### Schema — widen, don't duplicate

`todo_board` grows a `kind` column and a `(guild_id, kind)` primary key rather
than gaining a near-duplicate `todo_chore_board` table. Stage 1's one-row-per-
guild rule was about the channel and message ids staying **atomic**, which a
composite key preserves exactly; a second table would instead fork
`get_board`/`save_board`/`clear_board`/`guilds_with_board` and the sticky wiring
into two copies, and make "is the other board already in this channel?" a
cross-table union instead of a `WHERE` clause.

SQLite cannot widen a primary key in place, so migration 166 is the standard
create/copy/drop/rename rebuild. It runs inside the migration runner's explicit
`BEGIN`, so all four statements land together. Prod had one row; it became
`kind='all'`.

### The risk this stage had to clear

The bot already has several sticky panels, and
`docs/reviews/2026-08-06-sticky-panel-machinery.md` F1 found a **High**: two
`restick_on_bot` panels in one channel repost each other forever, reproduced at
26 sends with nobody typing. Adding a second sticky panel meant re-checking it.

**The F1 fix is sound and landed in the right place.** `core/sticky.py` keeps a
bounded `_placed` registry and consults `was_placed()` at the *decision points*
(`sticky.py:415`, `sticky.py:770`), with the `on_message` check
(`sticky.py:651`) documented as an unreliable optimisation — i.e. the corrected
version, after the first attempt was found in the wrong place.

**But the todo boards' hazard is a different one, and there was no guard at
all.** Neither board sets `restick_on_bot`, so they cannot storm. What they do
instead is share a channel with one bottom slot and leave one permanently
buried. Nothing prevented that configuration:
`PUT /api/todos/board` checked only that the channel exists and is postable, and
`economy_auction_service.sticky_panel_channels` — the only collision registry —
explicitly excludes the todo board and is consulted by exactly one caller
(`/bank auction start`).

So: `todo_service.conflicting_board` refuses the configuration, the route
answers 409 before posting anything, and both directions are covered. Proven by
test rather than argued:

- `tests/test_core_sticky.py::test_two_default_panels_cannot_both_hold_the_channel_bottom`
  — the hazard, asserted on the invariant that holds (one bottom, one winner)
  rather than on a send count, which measurement showed varies run to run
  (3/7 placements in one run, 9/10 in another; with three panels one took 0/10).
- `tests/test_todo_service.py` — the guard, both directions, plus self-repost
  and unpost-frees-the-channel.
- `tests/web/test_todo_routes.py` — the 409, with `place_*` asserted un-awaited.

**Left open:** `sticky_panel_channels` still doesn't know about either todo
board, so `/bank auction start` won't warn about them. Tracked as todo #103,
along with the F1 recommendation to hoist the check into `routes/panels.py` so
all postable panels get it.

### What shipped

| Layer | Change |
|---|---|
| `166_todo_chore_board.sql` | `todo_board` rebuilt with `(guild_id, kind)`; `todos.missed_at`; two indexes |
| `todo_service.py` | `BOARD_ALL`/`BOARD_CHORES`, kind on every board helper, `conflicting_board`, `mark_missed`, `_OPEN` excludes written-off rows |
| `todo_recurring_service.py` | `_spawn_one(reset_open=…)`, `open_instance_id`, `chore_streaks`, `chore_board_rows` |
| `board_logic.py` | `chore_state`, `render_chore_rows`, `render_chore_footer`, `chore_signature` |
| `todo_cog.py` | second `StickyPanel`, `TodoChoreBoardView`, `refresh_boards`, per-kind `_tick` |
| `routes/todo.py` | `kind` on the board body, 409 guard, `chore_board` in the list payload |
| `panels/todo.js` | second board card off a shared descriptor; Missed chip; Pending filter agrees with the boards |

### Stage 6 follow-up — six defects from code review (same day)

A review of `e68ca2d1` found six; all fixed in the commit that follows it. The
first is the one that mattered.

**The board could never show a miss.** `chore_board_rows` renders the *latest*
instance per definition, and `_spawn_one` writes the old row off and spawns its
replacement in the same call — so the latest instance is always open or done and
`chore_state` never returned `missed` on the real path. Four consecutive undone
days rendered as a plain ⬜, and `render_chore_footer`'s missed count was
structurally pinned at zero, while `missed_at` was being written correctly in the
DB the whole time. The feature's entire premise — that the reset exists to record
the days a chore did not happen — was invisible on the surface built to show it.

The test that should have caught this called `mark_missed` directly, which is not
a path the scheduler ever takes: it proved the renderer worked and said nothing
about whether the state was reachable. Replaced with one that drives `spawn_due`
over consecutive days and asserts on the rendered row. Rows now carry
`missed_previous` (was the instance *before* this one written off?), shown as
`❌ missed last run` on a still-open row and cleared as soon as the chore is
ticked again.

The other five:

| Where | Defect |
|---|---|
| `panels/todo.js` `renderStats` | Pending tile still counted written-off rows and derived Completed by subtraction, so the tile disagreed with the list directly beneath it. Now counts all three states independently, with a Missed tile that appears only when non-empty |
| `routes/todo.py` recurring CRUD | Create/edit/delete/pause/resume never repainted the chore board, and the 60s loop is no backstop — it only repaints guilds where a spawn or write-off happened. A new chore stayed invisible, a deleted one left a ghost row, for up to a day (daily) or a week (weekly) |
| `todo_recurring_service.chore_streaks` | `lookback` bounded the Python walk but not the SQL, so every chore-board repaint read the guild's entire recurring-todo history. Now bounded per definition with `ROW_NUMBER()`, which also makes the docstring true |
| `privacy_service` | The anonymisation undid itself: `_spawn_one` stamps `todo_recurring.created_by` onto every row it materialises, so an erased member's id was written back into `todos.added_by` at the next fire. `created_by` is now swept too, and the register row corrected — it had claimed the column was deliberately not swept |
| `todo_cog` Mark Done | Read `limit=25` where the board renders `_CHORE_FETCH` (50), so a guild whose first 25 chores were done got "Every chore is already ticked off" with open rows visible above it |

Reported but **not** fixed: `conflicting_board` only knows about the two todo
boards, so posting either into a channel already holding some *other*
`StickyPanel` (economy leaderboard, pen pals, quest board, auction card) is still
accepted silently. That is the pre-existing gap this stage deliberately scoped
out and filed as todo #103, not a regression — but the review is right that a
mod channel is exactly where those panels live, so the odds of hitting it went up.
