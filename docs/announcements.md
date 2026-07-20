# Announcements — dashboard-queued one-shot channel posts

Admins compose an announcement on the web dashboard, preview it live, and
either save it as a draft or schedule a guild-local post time; the bot posts
it to the chosen text channel. Web-only surface — no slash commands.
Plan: [plans/timed-announcements.md](plans/timed-announcements.md).

## Message shape

- **Embed**: title (≤256), markdown body (≤4096), optional image URL, accent
  color — a stored 6-hex override or, when blank, the server branding color
  via `resolve_accent_color`.
- **Plain-text line** (≤300, optional): rendered above the embed. This is
  where pings live — mentions inside embeds don't ping.
- **Mentions**: explicit `mention_kind` — `none` | `role` (+ `mention_role_id`)
  | `everyone`. `build_announcement_message` maps the kind to an exact
  `discord.AllowedMentions`; nothing pings unless picked, regardless of what
  the text contains.

## Role buttons

An announcement may carry up to **5** self-assign role buttons
(`MAX_BUTTONS`, one Discord action row). Click grants the role, click again
sheds it; replies are always ephemeral.

- **Persistence is custom-id-only.** `AnnouncementRoleButton` is a
  `DynamicItem` templated `ann_role:(?P<role_id>\d+)` and registered once in
  `__main__.py`. The role id is the entire state, so a posted announcement
  keeps working across restarts *and* after its queue row is deleted — a
  fire-and-forget post shouldn't die with its database row.
- **Safety is checked twice**, and this is the point of the design. The web
  route refuses a bad role up front (`_check_buttons_against_guild`); the
  callback re-checks on **every click** (`role_block_reason`) because a post
  stays clickable indefinitely and a role's permissions can change under it.
  A role that gains `administrator` months later stops being grantable at the
  next press, with no edit to the announcement.
- **No elevated override.** Role menus let a mod tick a per-option override to
  hand out a dangerous role; announcements deliberately don't — the post is
  public and permanent. Blocked: missing, `@everyone`, integration-managed,
  at-or-above DK's top role, or carrying any permission in
  `core.role_safety._DANGEROUS_PERMS`.
- **Shedding is never blocked.** Only the grant path is gated, so a member
  holding a role that later turned dangerous can still drop it.
- Refusals tell the member "that role isn't available anymore — ask a mod";
  the real reason goes to the log, since it describes server configuration.
- Blank `label` ⇒ the role's live name at post time, so renaming a role
  relabels its button on the next post.

`core/role_safety.py` is shared with role menus — one list of dangerous
permissions, two callers, no drift.

## Lifecycle

```
draft ──(set time)──► scheduled ──(loop fires)──► sent
  ▲                       │                         │
  └──(clear time / edit)──┘                         └─ jump link + Clone
                          └──(send fails / >2h late)──► error (edit or clone to retry)
```

- `post_date` ("YYYY-MM-DD") + `post_time_min` (minutes since local midnight)
  are guild-local wall-clock **source of truth**; `post_at` is the derived
  UTC-epoch cache (`compute_post_at`, guild's fixed `tz_offset_hours`, no
  DST). `post_at IS NULL` = draft — invisible to the loop by construction.
- Editing a non-sent row always re-derives status: time set → `scheduled`,
  time cleared → `draft`; a stale `error` is wiped either way. Sent rows are
  immutable (PUT/post-now → 409) — clone instead.
- **Post now** arms the row (`status='scheduled', post_at=now`) so it reuses
  the loop's single send path; fires on the next poll (≤ ~60s).
- Clone copies content columns only (`_CLONE_COLS`) into a fresh draft.

## Scheduler (announcements_service.py)

60s polling loop (`announcements_loop(bot, db_path)`), registered in
`__main__.py` next to `scheduled_games_loop`. DB work runs via
`asyncio.to_thread` over `open_db(db_path)`. Per row:

1. **Atomic claim**: `UPDATE … SET status='sent' WHERE id=? AND
   status='scheduled'`, rowcount checked — a concurrent pass or an admin edit
   loses cleanly; a crash mid-send can't double-post (worst case: a sent row
   with no message id).
2. **Late window**: more than `MAX_LATE_SECONDS` (2h) past `post_at` → mark
   `error` = "Missed post window (bot was offline)" without sending.
3. Resolve channel (`get_channel` → `fetch_channel` → None ⇒ error), resolve
   accent (guild branding, or default blurple if the guild is unavailable),
   build, send with the exact `allowed_mentions`. `Forbidden`/`HTTPException`
   ⇒ `error` (row is no longer claimable, so no retry).

## API (`/api/announcements`, admin-only)

| Endpoint | Behavior |
|---|---|
| `GET ""` | `{items, tz_offset_hours, default_accent_hex, max_buttons, guild_id}`; snowflakes stringified; sent rows carry `jump_url`; each item carries its `buttons` |
| `POST ""` | Create; date+time both-or-neither; past time → 400; time ⇒ `scheduled`, else `draft` |
| `PUT /{id}` | Full update; 404 missing, 409 sent; re-derives status/post_at |
| `DELETE /{id}` | Any status |
| `POST /{id}/post-now` | 409 sent; arms `post_at=now` |
| `POST /{id}/clone` | Content-only copy → new draft |

Validation: numeric channel (+ live `_channel_in_guild` check), title or body
required, kind `role` requires a role id, image URL must be http(s),
accent hex normalized to 6 uppercase hex digits, `extra="forbid"` on the body.
Buttons: ≤ `MAX_BUTTONS`, each needs a numeric role, no role twice on one
announcement (two buttons would toggle each other), style in
`primary|secondary|success`, plus the guild-side safety refusals above.
`replace_buttons` swaps the whole set on create and update — the editor always
submits the full list.

## Dashboard (`panels/announcements.js`, Config → Announcements)

Queue (draft/scheduled/error rows: edit / post now / delete), inline editor
with a debounced live preview (mention pill + plain line above a `dp-embed`
with the accent bar, button pills below it), and a Sent history (guild-local sent time, "Open in
Discord" jump link, clone, delete). A header line shows the server-local
clock and offset; date/time inputs are server-local, sent as strings. The
button rows (role picker, label, emoji, color) are edited through
`state.buttons` rather than read back off the DOM — adding or removing a row
re-mounts every role picker, which would otherwise drop unsaved text.

## Storage

`announcements` table (migration 089), index `(status, post_at)`. See the
migration header for the wall-clock-vs-cache rationale.
`announcement_buttons` (migration 095) holds one row per button, ordered by
`position`; foreign keys aren't enforced on this connection, so
`delete_announcement` drops the children explicitly — and only when the
guild-scoped parent delete actually matched.

## Tests

- `tests/unit/test_announcements_logic.py` — time math (offsets 0 / −7 /
  +5.5, day edges); mention matrix (content prefix + exact AllowedMentions
  flags per kind), embed fields, accent override/fallback.
- `tests/cogs/test_announcements_loop.py` — send + mark-sent bookkeeping,
  late-window miss, 30-min-late fire, unreachable channel, Forbidden →
  error with no re-send, claim atomicity, `fetch_due` filtering, post-now,
  and the button set riding along (order, `view=None` when there are none,
  wholesale replace, cascade delete, guild-scoped delete, clone copy).
- `tests/unit/test_role_safety.py` — every dangerous permission, hierarchy
  (`>=`, not `>`), managed/default/missing roles, the elevated override and
  what it does *not* bypass.
- `tests/unit/test_announcement_buttons.py` — view builder (cap, custom ids,
  label fallback, styles) and the click path: grant, toggle off, deleted
  role, non-guild click, HTTP failure, and the four re-check branches
  (turned dangerous, rose above DK, became managed, shedding still allowed)
  plus the refusal leaking no configuration.
- `tests/web/test_announcements_routes.py` — draft/scheduled create, guild
  offset in `post_at`, the 400 matrix, 409s on sent rows, clone reset,
  list shape (string snowflakes, `jump_url`, tz), non-admin 403, plus the
  button round-trip, replace/clear, the button 400 matrix, and each
  guild-side safety refusal on both create and edit.
