# Todo — Feature Spec

Shared per-guild todo list for the mod team. Tasks arrive from a slash command,
from a sticky Discord board, from the web dashboard, or from a recurring
schedule; they all land in the same list, curated from the dashboard.

The list has **one** Discord board, carrying four headed sections: **quest
sign-offs** waiting on a mod, **paid requests** waiting on a mod, **today's
chores** as a "did we do it today?" scoreboard, then **everything else
outstanding**. It was two separate boards between migrations 166 and 180 — see
"Why the split was undone" below — and it took over the quest sign-off queue on
2026-08-27 and the economy's three paid-approval queues on 2026-08-29.

## Surfaces

| Surface | Type | Permission | Purpose |
|---|---|---|---|
| `/todo task:<text>` | Slash | Moderator (server only) | Add a free-form task |
| Board **➕ Add Task** | Button + modal | Moderator | Add a task with optional notes |
| Board **✅ Complete** | Button + select | Moderator | Tick one or several off — chores first, then tasks |
| Board **✍️ Sign-Offs** | Button + select | **Economy manager** | Review a pending quest claim and approve or deny it |
| Board **🧾 Approvals** | Button + select | **Economy manager** | Review a paid request — themed day, sponsored question, pin — and approve or decline it |
| `GET /api/todos` | Web | Mod | List todos (newest first, capped at 200) + board placement |
| `POST /api/todos` | Web | Mod | Create a free-form todo as the authenticated user |
| `POST /api/todos/{id}/complete` | Web | Mod | Mark a todo complete |
| `PUT /api/todos/board` | Web | **Admin** | Post / move / remove the board |
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
and no Manage Server passed on the dashboard and was refused by the board's
buttons; Manage Channels did the reverse. The configured mod role — the
thing a server actually means by "moderator" — was consulted by neither. Roles
now decide, with the two elevated bits kept as a short-circuit and a bits-only
fallback for a guild that has configured no staff roles at all. `admin` and
`manage_server` stay bit-only: a config row must not be a path to the ceiling.

Board *placement* is the one admin-gated action: choosing a channel makes the
bot post into it, which is server configuration rather than worklist curation.

## Behavior

### `/todo`

Strips whitespace, rejects empty input, rejects task text longer than 500
characters, otherwise adds the todo and replies ephemerally with the new id.
Rejects DMs. Refreshes the board if one is posted.

### The board

A single message the bot keeps at the **bottom of a configured channel**.

- **Contents.** Four headed sections — **✍️ Quest sign-offs**, **🧾 Paid
  requests**, **🔁 Today's chores**, then **📋 Tasks**. Each is omitted entirely
  when empty, rather than stacking "nothing here" sentences; when all are, the
  board says so once.
- **Quest sign-offs.** Pending `econ_quest_claims` rows, oldest first, one line
  each: `**Alex** — Post a selfie · 🪙 500` (the guild's own currency emoji).
  They lead the board because they are the only section where somebody else is
  waiting on the mods rather than the other way round, and they carry **no id**
  — a claim `#14` printed beside a task list whose `#14` is a different row
  invites the wrong one being ticked. See "Sign-offs on the board" below.
- **Paid requests.** Pending rows from the economy's three paid-submission
  queues — a themed day, a sponsored question, a pin — merged oldest first, one
  line each: `**Alex** — 🎨 Theme: Cursed Cooking · 🪙 300`. They sit directly
  under the sign-offs for the same reason those lead: a member has already
  spent the coins and is waiting. The queue label is shown because one section
  covers three products; no id, same rule as above. See "Paid requests on the
  board" below.
- **Tasks.** Outstanding tasks only — neither completed nor written off —
  **oldest first**, so the longest-waiting task sits at the top where it nags.
  Each row is `` `#12` `` — the id as a monospace chip — followed by the task
  as ordinary flowing text, clipped at `TASK_CLIP` (44).
  **Chore-spawned rows are excluded** — the chores section above already shows
  them, with more state than a task line can carry, and listing them twice was
  the first thing that looked wrong when the boards merged.
- **No age on a task row, and why.** The row used to end in a live `<t:…:R>`,
  inside a cell padded to a fixed 48 characters. Measured against the 13 real
  tasks on the production board at a phone's ~34-character width: the padded
  layout cost 27 wrapped lines, dropping the padding but keeping the age cost
  **43**, and no layout that keeps the age beats the padded one — "2 months
  ago" pushes almost every short row over the width by itself. The current
  shape costs **22**, while showing more of each task than the old cell did
  (44 characters against 42). The list is oldest-first, so *position* already
  carries what the age was for here, and the exact date is one tap away on the
  dashboard. The chore rows keep their timestamp: there is one per chore, a
  handful per guild, and "who did it and when" is the question that board
  answers.
- **Row budget.** 15 rows across the sections. Sign-offs take up to 5 off the
  top, paid requests up to 5, and chores up to 8; tasks get the rest, floored at
  3 — no backlog of claims, requests or chores may push the task list off the
  board, which is the failure the merge existed to end, just pointing the other
  way. Overflow reads "…and **N** more on the dashboard". Accent from
  `safe_resolve_accent`.
- **Footer.** `J sign-offs waiting · P paid requests waiting · N of M chores
  done · K tasks · updates automatically`. Each part is dropped when its
  section is, rather than reading "0 sign-offs waiting" or "0 of 0 chores
  done".
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
- **One repaint on boot.** The 60s loop's first pass repaints *every* posted
  board, not only the guilds where something spawned. A board posted by a
  previous release can be carrying a view the running one no longer registers —
  after the 180 merge the surviving message is the old chore board's, and its
  ✅ Mark Done button answers "This interaction failed" until something
  repaints it. An edit replaces the view along with the content, so one pass on
  boot closes that window instead of waiting for the next message in the
  channel. It also heals any board that drifted while the bot was down.
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
`todo_board_complete`, `todo_board_signoffs`, `todo_board_approvals`)
re-registered in `cog_load`, so buttons survive restarts.

**One Complete button over both sections.** `board_logic.completable_options`
puts open chores first, then tasks, and carries a chore forward by its
`todo_id`: the thing being completed is a todo row either way, and the select
only ever needed the id. Two boards meant two buttons and a mod having to know
which list a row was on before they could tick it.

### Sign-offs on the board

Quest claims that need a human to say yes moved here from a per-claim card in
the economy's bank channel on **2026-08-27**. Nothing about them is stored by
this feature:

- **No mirrored rows.** The section reads `econ_quest_claims` live, through
  `economy_quests_service.pending_signoff_rows` (claim joined to quest, oldest
  first) and `pending_signoff_count` for the footer. There is nothing to keep
  in sync, resolving a claim anywhere removes it from the board, and the
  Complete button structurally cannot offer one — a claim is approved, not
  ticked off.
- **The button, not the row, acts.** The board is one sticky message and
  Discord caps components per message, so Approve/Deny cannot hang off it once
  per claim. **✍️ Sign-Offs** opens an ephemeral pick-one select (Discord's
  25-option cap), and picking a claim edits that ephemeral into the claim's
  full detail — member, quest, reward, criteria, prior denials — with
  Approve/Deny under it. The identical shape the Complete button uses. A claim
  someone else resolved while the picker was open renders as resolved, with no
  buttons.
- **Whose permission.** `can_manage_economy` (admin or the configured economy
  manager role), *not* the board's `AppContext.is_mod` — approving pays real
  currency. In the main guild the two roles are the same one, so the move shut
  nobody out. The board's own Add/Complete buttons keep the mod gate.
- **Repaints.** Every edge repaints the board: a claim filed (`/bank quests`,
  a trigger phrase, a game — one repaint per batch there, not per claimant),
  resolved from the board or the dashboard, or expired by the sweep. All
  best-effort via `quest_views.refresh_signoff_board`, which reaches the cog
  through `get_cog("TodoCog")` — the claim is already committed and, on the
  resolving side, the member already paid.
- **Where the outcome is announced.** Not here, and never by DM — the register
  channel carries it. See `docs/economy_spec.md` §4 (sign-off quests) for the
  full rule, including the two cases that announce nowhere at all.

### Paid requests on the board

The economy sells three things a moderator has to approve before they happen: a
**themed day**, a **sponsored question of the day**, and a **pin**. Each used
to post its own Approve/Decline card into the economy's `bank_channel_id`.
They moved here on **2026-08-29**, on exactly the sign-off pattern above and
for exactly the same reason — plus a sharper one: a request names the member
and quotes what they wrote, so a card in a member-facing channel published an
unreviewed submission to the whole server.

- **No mirrored rows.** The section reads the three submission tables live,
  through `economy_approvals_service.pending_approvals` (a `UNION ALL` over the
  products in `QUEUES`, oldest first, each arm served by its own
  `(guild_id, state, created_at)` index) and `pending_approval_count` for the
  footer. Nothing to keep in sync; resolving anywhere removes it; the Complete
  button structurally cannot offer one.
- **One section, one button, three queues.** A heading per product would be
  three headings over nothing most days. The row carries its queue label, the
  select option carries it again, and **🧾 Approvals** opens one ephemeral
  pick-one select across all three. Picking a request edits that ephemeral into
  **that product's own review card** — the same embed builder the bank-channel
  card used, with that product's own Approve/Decline buttons under it. A
  request someone else resolved while the picker was open renders as resolved,
  with no buttons. The select's value is `kind:id`, because the ids are
  per-table and theme #3 is not pin #3.
- **Whose permission.** `can_manage_economy`, not `AppContext.is_mod`: every
  decision moves currency, and a denial refunds it.
- **Repaints.** Every edge repaints the board — a request submitted, resolved
  from the board or the dashboard, or expired by the economy's hourly sweep —
  best-effort through `view_helpers.refresh_todo_board`.
- **The money is already taken.** These are charged-at-submit queues. Deny and
  expiry are refund paths, and the exactly-once guarantee lives in
  `economy_submission_store.refund_once`'s `refunded_at IS NULL` predicate, not
  in anything this board does.
- **What did not move.** The **emoji sponsorship** is a fourth paid submission
  but is not on the board: its approval is an upload, not a yes/no, and it has
  the dashboard queue Pin of the Day never had. `notify_member`'s public
  bank-channel fallback for a member with DMs closed is a different mechanism
  and is unchanged.

### Today's chores

The board's top section, same question it always asked: **"did we do it
today?"**, where the Tasks section below asks "what is outstanding?"

- **Contents.** One row per *active* recurring definition — its **latest**
  instance, done or not — in `time_of_day` order, so it reads like a shift
  checklist rather than by an id nobody thinks in. Each row is a state box
  (✅ / ⬜) leading the line, the chore in bold, then who ticked it and a live
  `<t:…:R>`, then a 🔥 streak. No padded cell: the boxes align just as well by
  starting the line, and a fixed-width cell wrapped on a phone and stranded a
  bare backtick on its own line. Paused definitions are left out: a
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
  two disagreed about a definition with no instance. When the button has
  nothing at all to offer, `nothing_to_tick_message` distinguishes three cases
  that used to be spoken as one: no chores configured, none due yet (naming the
  soonest and when it lands), and genuinely all ticked off. It is only reached
  when chores exist and no tasks are pending either — with tasks outstanding
  the picker is never empty.
- **No separate Add.** A chore is a *recurring definition* with a cadence,
  created on the dashboard; **➕ Add Task** makes a one-off.

### Why the split was undone (migration 180, 2026-08-25)

Migration 166 split the board in two so a daily "post the QOTD" wasn't buried
among "fix the quote bot". It solved that by putting the two lists in two
channels, and that cost more than it saved:

- The boards could never share a channel. A Discord channel has one bottom
  slot; both wanting it meant every member message woke both, they raced, and
  whichever landed second owned the bottom while the other sat buried — quiet,
  permanent, and unrepairable by any amount of activity in the channel. So a
  server with one mod channel had to choose.
- **Choosing is what happened.** In prod the chore board was posted and the
  all-todos board never was, leaving 25 open tasks — the oldest from 12 June —
  with no Discord surface at all.
- Every path forked: two placements, two refreshes, two views, two sticky
  listeners, a collision check and its 409.

One board with headed sections answers both questions in one place, which is
what having two boards was trying to do. `todo_board.kind` went with the split
— a single-valued discriminator is a worse lie than no column — and the
`conflicting_board` / `board_conflict_detail` pair went with it. The general
`panel_posting.sticky_conflict` guard still covers collisions with *other*
sticky panels, which is the case that remains real.

**Merge rule.** Keep the posted row. If a guild somehow has both posted, keep
the **chores** channel: chores are mod-facing and the merged board carries
them, so landing it in a public channel would disclose more than landing tasks
in a mod channel. The losing board's Discord message is left where it is — a
migration cannot call Discord — and goes stale until someone deletes it. In
prod this never arises. Pinned in
`tests/test_migration_180_todo_board_merge.py` against production's real rows.

> Not covered: `economy_auction_service.sticky_panel_channels` still does not
> know about the todo board, so `/bank auction start` won't warn about it.
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

**Board placement is visible.** The board card states whether it is posted, and
carries a warning while it is not: it is the only Discord surface that can tick
anything off. The same cost is spelled out in the remove confirmation. In prod
the all-todos board was unposted at 12:15 on 2026-08-18 to free ✅│todo for the
chore board, and the loss of every Discord completion path went unannounced; it
was found by failing to tick anything off, and it stayed unposted until the
boards merged.

Creating, editing, deleting, pausing or resuming a **definition** repaints the
board directly. The 60s loop is not a backstop for it: that only repaints
guilds where a spawn or a write-off happened, and the chores section is one row
per definition — so without the explicit repaint an added chore stays invisible
and a deleted one leaves a ghost row until the next scheduled fire, a day away
for a daily and a week for a weekly.

## Permissions

- Discord: moderator-gated through `AppContext.is_mod` — administrator or
  manage_guild, otherwise a configured mod/admin role. `/todo` rejects DMs.
- Web: every endpoint requires `moderator`, resolved by `resolve_guild_perms`
  from the same rule; `PUT /api/todos/board` additionally requires `admin`. The
  panel disables the board card off `can_manage_board`.
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
| A `kind` field in the board body | HTTP 422 — the body forbids extras, so a client still sending it is told rather than having it silently ignored |
| **Complete** with chores configured, all ticked, nothing else pending | "Every chore is already ticked off. ✨" |
| **Complete** with chores configured but none yet due, nothing else pending | "Nothing due yet — **&lt;chore&gt;** first lands &lt;relative timestamp&gt;. ⏳" |
| **Complete** with no chores configured and nothing pending | "Nothing pending — the list is already clear. ✨" |
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
- **The sign-off and paid-request sections are guests, not features of this
  list.** They render rows the economy owns; this feature stores nothing about
  them, adds no column, and gains no migration for them. If either queue ever
  goes away, its section goes with it and the todo list is unchanged.
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
  `missed_at` (migration 166) for a recurring row the daily reset wrote off, and
  `purchase_id` (migration 179) for a row a custom-shop-item order spawned. No
  per-user PII beyond Discord ids. Registered in `docs/data_register.md`.
- `todo_board` — `guild_id` PK, `channel_id`, `message_id`, `updated_at`;
  zeroes mean "not posted". One row per guild keeps the board's channel and
  message ids atomic, which is the original reason for the table. Migration 166
  widened the key to `(guild_id, kind)` for the second board; **migration 180
  narrowed it back** when the boards merged.
- `todo_recurring` — definition, cadence, `next_run_at` cache, `status`,
  `last_run_at`/`last_status`.

The sign-off and paid-request sections add **no table and no migration**: they
read `econ_quest_claims` and the three `econ_*_submissions` tables, which the
economy owns and `docs/data_register.md` already covers.

## Notes / history

- **Paid approval requests moved onto the board (2026-08-29).** Flash Themes,
  the QOTD sponsor and Pin of the Day each posted an Approve/Decline card into
  `bank_channel_id`, finishing the migration the sign-offs started two days
  earlier. The trigger was a live "📋 Theme Requested" card — a named member,
  300 coins, and their unreviewed idea — appearing in `🏦│how-it-works` in
  front of the whole server. Pin of the Day was the worst affected: it has no
  dashboard queue, so that public card was its *only* review surface. As with
  the sign-offs, the persistent Approve/Decline buttons stay registered so
  older cards remain clickable, and only new posts moved.
- **Quest sign-offs moved onto the board (2026-08-27).** They used to post one
  Approve/Deny card per claim into the economy's bank channel — which in the
  main guild is `🏦│how-it-works`, a member-facing explainer channel a mod had
  to go looking in. The board is one sticky message the mod team already reads,
  and it already had the shape the move needed (a button that opens an
  ephemeral select) from the Complete button. The card's builder survives as
  the ephemeral detail view; its persistent Approve/Deny buttons stay
  registered so the cards posted before the move are still clickable.
- The **`Add to Todo` message context menu** described in earlier revisions of
  this spec **does not exist in the code** — no `ContextMenu` is registered, and
  nothing but the slash command, the board, and the web routes calls
  `create_todo`. The `todos.description` and `todos.source_message_url` columns
  (migration `008`) were added for it; `description` is now populated by the
  board's optional notes field and by recurring definitions, and
  `source_message_url` is currently written by nothing. Removed from the spec
  2026-07-26 rather than left standing as an aspirational claim.
