# Todo — Feature Spec

Shared per-guild todo list for the mod team. Tasks arrive from a slash command,
from a sticky Discord board, from the web dashboard, or from a recurring
schedule; they all land in the same list, curated from the dashboard.

The list has **two** Discord boards. The original shows everything outstanding;
the **mod chore board** shows only recurring chores, as a daily "did we do it
today?" scoreboard. They are configured the same way and **may never share a
channel** — see "Two boards, never one channel" below.

## Surfaces

| Surface | Type | Permission | Purpose |
|---|---|---|---|
| `/todo task:<text>` | Slash | Moderator (server only) | Add a free-form task |
| Board **➕ Add Task** | Button + modal | Moderator | Add a task with optional notes |
| Board **✅ Complete** | Button + select | Moderator | Tick one or several tasks off |
| Chore board **✅ Mark Done** | Button + select | Moderator | Tick off today's chores |
| `GET /api/todos` | Web | Mod | List todos (newest first, capped at 200) + board placement |
| `POST /api/todos` | Web | Mod | Create a free-form todo as the authenticated user |
| `POST /api/todos/{id}/complete` | Web | Mod | Mark a todo complete |
| `PUT /api/todos/board` | Web | **Admin** | Post / move / remove either board (`kind`: `all` \| `chores`) |
| `GET/POST /api/todos/recurring` | Web | Mod | List / create recurring definitions |
| `PUT/DELETE /api/todos/recurring/{id}` | Web | Mod | Edit / delete a definition |
| `POST /api/todos/recurring/{id}/{pause,resume,run-now}` | Web | Mod | Row actions |

The list is a moderator worklist end to end, and both surfaces resolve
"moderator" the same way: **`AppContext.is_mod`** — administrator or
manage_guild, otherwise a role in the guild's configured
`mod_role_ids`/`admin_role_ids`. The dashboard applies that rule through
`web_server.auth.resolve_guild_perms`, which the `moderator` tier resolves
through for **every** panel, not only this one.

Until 2026-08-18 the two disagreed. Discord gated on the games cogs'
`has_mod_or_admin_permissions` (administrator, manage_guild, manage_channels)
while the web resolved `moderator` from a wider bit set (adding kick, ban,
manage_messages, manage_roles, moderate_members). A mod with Timeout Members
and no Manage Server passed on the dashboard and was refused by the chore
board's buttons; Manage Channels did the reverse. The configured mod role — the
thing a server actually means by "moderator" — was consulted by neither. Roles
now decide, with the two elevated bits kept as a short-circuit and a bits-only
fallback for a guild that has configured no staff roles at all. `admin` and
`manage_server` stay bit-only: a config row must not be a path to the ceiling.

Board *placement* is the one admin-gated action: choosing a channel makes the
bot post into it, which is server configuration rather than worklist curation.
Both boards go through the same endpoint, distinguished by `kind`.

## Behavior

### `/todo`

Strips whitespace, rejects empty input, rejects task text longer than 500
characters, otherwise adds the todo and replies ephemerally with the new id.
Rejects DMs. Refreshes the board if one is posted.

### The board

A single message the bot keeps at the **bottom of a configured channel**.

- **Contents.** Outstanding tasks only — neither completed nor written off —
  **oldest first** — the longest-waiting task
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
- **Post before delete.** The replacement is sent *first*, then the old board
  removed. Deleting first would destroy a working board whenever the new
  channel turns out to be unpostable — and if the target is the old channel
  there'd be nothing left to heal from. Any `HTTPException` (not just
  `Forbidden`) leaves the existing board untouched and surfaces a 400.
- **Text channels only.** The picker filters `/api/meta/channels` down to text
  channels: `guild.get_channel()` can't resolve a thread, so offering one would
  400 on a channel the UI had just listed, and a thread can archive out from
  under a board that lives by delete-and-repost.

The view is a static-`custom_id` persistent view (`todo_board_add`,
`todo_board_complete`) re-registered in `cog_load`, so buttons survive restarts.

### The mod chore board

A second sticky panel, same machinery, different question. The all-todos board
asks "what is outstanding?"; this one asks **"did we do it today?"**

- **Contents.** One row per *active* recurring definition — its **latest**
  instance, done or not — in `time_of_day` order, so it reads like a shift
  checklist rather than by an id nobody thinks in. Each row is a state box
  (✅ / ⬜) plus the chore in a padded monospace cell, then who ticked it and a
  live `<t:…:R>`, then a 🔥 streak. Paused definitions are left out: a
  chore parked for the holidays is not one the team is failing, and showing it
  with a dead streak reads as a reproach.
- **A scoreboard, not a pending list.** A ticked chore *stays* on the board
  until the next reset replaces it. A board that removes the answer the moment
  it is yes cannot answer the question it exists for.
- **Streaks.** Consecutive completed instances, ending at the first missed one.
  Shown from two up — a 🔥 1 on everything that happened once is noise that
  hides a real run. **Today does not count against you:** the newest instance is
  skipped while it is still outstanding, or a chore due at 09:00 would read as a
  broken streak every morning until someone ticked it.
- **How a miss reaches the board — `missed_previous`, not `chore_state`.** The
  reset writes the old instance off and spawns its replacement *in the same
  call*, so the latest instance — the only one this board renders — is never
  the missed one. Rendering `chore_state == "missed"` therefore showed three
  consecutive undone days as a plain ⬜ with the footer's missed count
  structurally pinned at zero, while `missed_at` was being written correctly
  the whole time. So each row also carries whether the instance *before* it was
  written off, rendered as `❌ missed last run` on a still-open row and cleared
  the moment the chore is ticked again. `chore_state`'s `missed` branch stays
  as defence for a row `mark_missed` closed with nothing spawned behind it,
  which the service permits.
- **Footer.** `N of M done`, plus `· K missed last run` when any are. Counts
  every active chore including those past the visible window, so it can't
  disagree with the dashboard about how the day went.
- **What the button can offer.** `board_logic.tickable_chores` — a row with a
  todo behind it, still open. A done chore has nothing to tick and a missed one
  is closed business `complete_todo` refuses anyway. It lives beside
  `chore_state` rather than in the cog so the button and the board read the
  same rows through the same rule; while the filter lived alone in the cog the
  two disagreed about a definition with no instance. When it offers nothing,
  `nothing_to_tick_message` distinguishes three cases that used to be spoken as
  one: no chores configured, none due yet (naming the soonest and when it
  lands), and genuinely all ticked off.
- **One button, `todo_chore_board_complete`.** No Add: a chore is a *recurring
  definition* with a cadence, created on the dashboard — the thing the other
  board's Add button makes is a one-off task, which is exactly what this board
  exists not to show. The picker offers only chores still open; a done one has
  nothing to tick and a missed one is closed business `complete_todo` refuses.

### Two boards, never one channel

A Discord channel has exactly one bottom slot, and both boards want it. Put
them in the same channel and every member message wakes both: they race, and
whichever lands second owns the bottom while the other sits buried above it —
the one state a sticky panel exists to avoid, and one no amount of activity in
the channel repairs.

Neither board sets `restick_on_bot`, so this is *not* the repost storm
`docs/reviews/2026-08-06-sticky-panel-machinery.md` F1 found between the casino
and bounty hubs — they cannot chase each other's reposts at all. It is quieter
and permanent. Measured with that file's harness over 10 member
messages, the split between the two panels varies run to run (3/7 placements in
one run, 9/10 in another; with three panels one took 0 of 10) — so the
committed test asserts the part that doesn't vary, **one bottom, one winner**,
rather than a send count that would make it flaky.

The sticky layer cannot arbitrate this — someone has to lose — so
`todo_service.conflicting_board` refuses the configuration where a human can
still read the reason, and `PUT /api/todos/board` answers **409** naming the
board already in that channel. The check runs *before* anything is posted, and
catches the collision from either direction. Unposting a board frees its
channel for the other.

> Not covered: `economy_auction_service.sticky_panel_channels` still does not
> know about either todo board, so `/bank auction start` won't warn about them.
> Tracked as todo #103.

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
- **Daily reset.** When an occurrence comes round and the previous instance is
  still outstanding, that instance is **written off** (`todos.missed_at`) and a
  fresh row spawns in its place. Exactly one row per definition is ever
  outstanding, so nothing stacks — and unlike the skip-if-pending rule this
  replaced, the day that did not happen leaves a durable record.

  Skip-if-pending kept one ageing row instead: Monday's untouched QOTD was still
  Monday's row on Wednesday, you could not tell which day it stood for, one tick
  credited both, and no streak was computable because a skipped day left no row
  at all. The trade is more rows for an answerable question.

  A written-off row is **closed**: `pending_todos`/`pending_count` exclude it (so
  it also leaves the all-todos board), and `complete_todo` refuses it — you
  cannot tick yesterday's box today, which would invent a completion and put a
  hole in the streak either side of it.
- **Catch-up.** `compute_next_run(after=…)` advances past every missed slot to
  the next future one, so three days of downtime spawns one row on boot — and
  writes off *one* instance, not three. Downtime is not evidence that a chore
  was skipped; the record deliberately covers only the days the bot was
  watching.
- **Created after its own time of day (2026-08-18).** A chore whose slot has
  already gone by on the guild's current local day materialises **one instance
  immediately**, so it is tickable from the moment it exists. It borrows *Run
  now*'s semantics, not the reset's: `next_run_at` is untouched (still the real
  next occurrence) and nothing is written off as missed — creating a chore is
  not a day boundary. A weekly chore created on a day it does not run gets
  nothing, because it has no occurrence today to have missed.

  Without this, a chore added at 12:16 for an 09:00 daily had no instance until
  the next morning, while the chore board drew it ⬜ open — correctly, per
  `chore_state` — and **Mark Done** answered "Every chore is already ticked
  off" over a board showing open work. Two prod dailies sat like that from
  2026-08-18 12:16 until the next 09:00.
- **Resume** recomputes `next_run_at` from now, so a long pause doesn't come
  back and immediately fire a stale slot.
- **Run now** adds one instance immediately and changes nothing else: not the
  entry's `status`, not its `next_run_at`. **Skip-if-pending still applies
  here**, deliberately: a manual add is not a day boundary, so pressing the
  button twice can neither stack duplicates nor write the first press off as
  missed — that would fabricate a failure out of a double click and the streak
  would wear it. It deliberately does *not* go
  through `spawn_due` — that scans every guild, so driving it from one guild's
  request would spawn other guilds' due chores and rewrite their `next_run_at`
  with the requesting guild's UTC offset. Leaving `status` alone likewise means
  "add one now" can't quietly un-pause an entry paused for the holidays.
- **Delete** stops the repeat but leaves any already-spawned row on the list —
  that's real outstanding work, and silently removing a task a mod is part-way
  through would be worse than orphaning it.

### Web list

The dashboard shows pending and completed lists for the active guild, plus both
board cards and the recurring-task table. Names are resolved against the active
guild. Completion records the moderator who clicked complete.

A written-off recurring row shows a **Missed** chip and no Mark Complete button,
and is excluded from the Pending filter — the same rule the boards and
`pending_count` use, so the three can't disagree. The stat tiles count all
three states independently rather than deriving one by subtraction, and a
**Missed** tile appears only when there is something in it.

**Board placement is visible.** Each board card states whether it is posted,
and the all-todos card carries a warning while it is not: it is the only
Discord surface that can complete an ordinary todo, since the chore board's
**Mark Done** offers recurring instances and nothing else. The same cost is
spelled out in that card's remove confirmation and appended to the 409 when the
all-todos board is the resident of a channel another board is being placed in —
clearing it is the way through that refusal, so the price belongs in the
sentence that sends a mod to do it. In prod the all-todos board was unposted at
12:15 on 2026-08-18 to free ✅│todo for the chore board, and the loss of every
Discord completion path went unannounced; it was found by failing to tick
anything off.

Creating, editing, deleting, pausing or resuming a **definition** repaints the
chore board directly. The 60s loop is not a backstop for it: that only repaints
guilds where a spawn or a write-off happened, and the chore board is one row per
definition — so without the explicit repaint an added chore stays invisible and
a deleted one leaves a ghost row until the next scheduled fire, a day away for a
daily and a week for a weekly.

## Permissions

- Discord: moderator-gated through `AppContext.is_mod` — administrator or
  manage_guild, otherwise a configured mod/admin role. `/todo` rejects DMs.
- Web: every endpoint requires `moderator`, resolved by `resolve_guild_perms`
  from the same rule; `PUT /api/todos/board` additionally requires `admin`, for
  either `kind`. The panel disables both board cards off `can_manage_board`.
- The parity between the two is pinned by `tests/test_todo_mod_tier_parity.py`,
  which asserts both surfaces agree case by case.

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
| Board `kind` is neither `all` nor `chores` | HTTP 400: "Unknown board." |
| The other todo board already holds the chosen channel | HTTP 409: "The server todo board is already in that channel. Two sticky boards can't share one — they'd take turns being buried. Move that one first, or pick a different channel." When the resident is the all-todos board the refusal adds what clearing it costs (see *Board placement is visible*). |
| Chore board **Mark Done** with everything ticked | "Every chore is already ticked off. ✨" |
| Chore board **Mark Done** with chores configured but none yet due | "Nothing due yet — **&lt;chore&gt;** first lands &lt;relative timestamp&gt;. ⏳" |
| Chore board **Mark Done** with no chores configured at all | "No recurring chores set up yet — add them on the dashboard. ✨" |
| Chore board button clicked by a non-moderator | "Only moderators can tick off chores." |
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
- **No per-chore reset rule.** The reset is uniform. A per-definition "carry
  over vs. reset" switch was considered and dropped: two rules doubles what a
  streak, a footer, and a missed row each mean, for a distinction a mod team can
  express by choosing the cadence instead.
- **No backfill of missed days.** `missed_at` starts empty; chores that ran
  before migration 166 have no history, so their streaks begin at zero.

## Stored data

Three tables, all per-guild:

- `todos` — headline, optional description and source URL, creator id, creation
  timestamp, completion timestamp + completer id, `recurring_id` provenance, and
  `missed_at` (migration 166) for a recurring row the daily reset wrote off. No
  per-user PII beyond Discord ids. Registered in `docs/data_register.md`.
- `todo_board` — `(guild_id, kind)` PK, `channel_id`, `message_id`,
  `updated_at`; zeroes mean "not posted". `kind` is `all` or `chores`
  (migration 166 widened the key from `guild_id` alone; existing rows became
  `all`). One row per board keeps a board's channel and message ids atomic,
  which was the original reason for the table and survives the composite key
  unchanged.
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
