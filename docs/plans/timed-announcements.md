# Timed Announcements — implementation plan

Status: **built** 2026-07-19 (single commit; awaiting live testing). Decisions
confirmed with Billy 2026-07-19. Reference spec: [../announcements.md](../announcements.md).

## Context

There's no way to queue a server announcement ahead of time: QOTD is a manual
staff command, and the only scheduled posting the bot does is game launches.
This module lets an admin compose an announcement on the dashboard, see a
Discord-style preview, and schedule a one-shot post time; the bot posts it to
a chosen text channel. Web-only surface — no slash commands, per the design
philosophy.

## Confirmed requirements

- **One-shot only.** No recurrence (add later if wanted).
- **Format:** rich embed (title, markdown body, optional image URL, accent
  color defaulting to server branding) + optional **plain-text line above the
  embed** — needed for pings, since mentions inside embeds don't ping.
- **Mentions:** explicit picker — none / one role / @everyone — enforced via
  `discord.AllowedMentions`. Nothing pings unless explicitly picked.
- **Lifecycle:** `draft → scheduled → sent | error`. Drafts have no time
  (queue first, schedule later). Sent items move to a history list with a
  jump link to the posted message and a Clone button (clone = new draft).
- **Late-fire policy:** if the bot was down at post time, fire late up to
  **2 hours** (`MAX_LATE_SECONDS = 2*3600`); past that, mark
  `error` = "Missed post window (bot was offline)" — a stale "starts at 6PM!"
  post is worse than none. Admin clones + reschedules from history.
- **Post now** button: arms the row (`status='scheduled', post_at=now`) so it
  reuses the loop's single send path; fires within a minute.

## Architecture

Clone the **scheduled games** shape (`services/scheduled_games_service.py`):
dashboard route writes rows computing a UTC-epoch `post_at` cache from
guild-local wall-clock via `get_tz_offset_hours` (`core/db_utils.py:68`);
a hand-rolled 60s polling loop registered in `__main__.py` picks up due rows.
Own table + own service — `games_scheduled` is game-centric (launcher
registry, recurrence, busy-checks) and shouldn't grow announcement branches.

Time input is **guild-local** (scheduled-games convention, not the
browser-local economy-quests one): announcements are guild-facing events, and
two admins in different timezones must see the same number. Panel shows a
"Times are server-local (UTC−7)" hint from `tz_offset_hours` returned by the
list endpoint.

Preview is **client-side live** (`dp-embed` CSS + `mdToHtml`, the
`role-menus.js:173-228` pattern) — no templating to resolve server-side, so
no preview endpoint. The one server-owned input, the default accent, comes
back on the list response via `await resolve_accent_color(ctx.db_path, guild)`
(route precedent: `routes/voice_master.py:262`).

Crash safety: **atomic claim before send** —
`UPDATE ... SET status='sent' WHERE id=? AND status='scheduled'`, check
rowcount. A mid-send crash can't double-post; a concurrent edit can't race
the loop. (Hardened version of scheduled games' claim-before-launch.)

## Steps

### 1. Migration — `src/migrations/089_announcements.sql`

(089 free as of 2026-07-19; re-check for a same-number file before adding.)

```sql
CREATE TABLE IF NOT EXISTS announcements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    title            TEXT    NOT NULL DEFAULT '',
    body             TEXT    NOT NULL DEFAULT '',    -- markdown embed description
    image_url        TEXT,
    accent_hex       TEXT,                           -- NULL = server branding
    plain_text       TEXT,                           -- optional line above the embed
    mention_kind     TEXT    NOT NULL DEFAULT 'none',-- none | role | everyone
    mention_role_id  INTEGER,
    post_date        TEXT,                           -- guild-local YYYY-MM-DD (source of truth)
    post_time_min    INTEGER,                        -- minutes since local midnight
    post_at          REAL,                           -- derived UTC-epoch cache; NULL = draft
    status           TEXT    NOT NULL DEFAULT 'draft',-- draft | scheduled | sent | error
    sent_channel_id  INTEGER,                        -- channel actually posted to (channel_id is editable pre-send)
    sent_message_id  INTEGER,
    sent_at          REAL,
    error            TEXT,
    created_by       INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_due ON announcements(status, post_at);
```

Header comment in 046's style (wall-clock is truth, epoch is cache). Note:
touching `src/migrations/` makes the pre-commit gate run the full suite.

### 2. Service — `src/bot_modules/services/announcements_service.py`

Three layers like scheduled_games_service (pure math / sync CRUD / async loop).
DB access from the loop via `asyncio.to_thread` over `open_db(db_path)` (the
`economy_loop.py` idiom) — don't grow `GamesDb`.

Constants: `VALID_MENTION_KINDS`, `VALID_STATUSES`,
`MAX_LATE_SECONDS = 2*3600`, `MISSED_ERROR`.

Pure (tier-1 test surface):
- `compute_post_at(post_date, post_time_min, offset_hours) -> float` — copy
  the `_local_to_epoch` math (scheduled_games_service.py:42), don't import
  privates across services.
- `build_announcement_message(row, accent) -> (content|None, Embed, AllowedMentions)`
  — content = mention prefix (`@everyone` / `<@&id>`) + plain_text, or None;
  embed from title/body/image; color = row accent_hex else passed accent;
  AllowedMentions exactly matching mention_kind (`none()` / roles=[that role]
  / everyone=True only). **Safety-critical function.**

Sync CRUD (every query guards `AND guild_id = ?`):
`create_announcement`, `list_announcements` (pending by post_at NULLS LAST
then created_at; sent/error by sent_at DESC), `get_announcement`,
`update_announcement` (whitelist `_UPDATABLE_COLS`, never sent_*),
`delete_announcement`, `clone_announcement` (content only → new draft),
`fetch_due(conn, now)` (`status='scheduled' AND post_at <= ?`, all guilds),
`claim(conn, id, now) -> bool` (atomic UPDATE, rowcount==1),
`mark_sent`, `mark_error`.

Async: `_resolve_channel` (get_channel → fetch_channel → None, copy of
scheduled_games_service.py:224); `_process_due(bot, db_path, row, now)`:
claim → late-window check → resolve channel/guild →
`resolve_accent_color` → build → `channel.send(...)` in try/except
`(Forbidden, HTTPException)` → `mark_sent` / `mark_error`;
`announcements_loop(bot, db_path)`: `wait_until_ready`, 60s poll, per-row
try/except + `CancelledError` re-raise (scheduled_games_service.py:355-374).

**Modify `src/dungeonkeeper/__main__.py`** (~:317): append
`lambda: announcements_loop(bot, db_path)` to `bot.startup_task_factories`.

### 3. Route — `src/web_server/routes/announcements.py`

Shape from `routes/scheduled_games.py`, conventions from `routes/qa.py`:
`APIRouter()` mounted in `server.py` as
`app.include_router(..., prefix="/api", tags=["announcements"])` with paths
written `/announcements/...` in the module (repo convention); every endpoint
`Depends(require_perms({"admin"}))`; handlers use `get_ctx` /
`get_active_guild_id` / `run_query`; **snowflakes stringified** in JSON.

`AnnouncementBody` (Pydantic, `extra="forbid"`): channel_id str; title ≤256;
body ≤4096; image_url?; accent_hex?; plain_text? ≤300; mention_kind="none";
mention_role_id?; post_date?; post_time? ("HH:MM"). Validation (400s):
title or body nonempty; kind valid; role id required iff kind=role;
image_url http(s); accent_hex 6-hex; date+time both-or-neither; channel in
guild (`_channel_in_guild`, scheduled_games.py:91) + best-effort role check.

Endpoints:
- `GET /announcements` → `{items, tz_offset_hours, default_accent_hex, guild_id}`;
  sent rows get computed `jump_url`
  (`https://discord.com/channels/{g}/{sent_channel}/{sent_msg}`).
- `POST /announcements` — time given ⇒ compute `post_at` (reject past times),
  `scheduled`; else `draft`.
- `PUT /announcements/{id}` — 404 missing; **409 if sent** ("clone it
  instead"); setting a time flips draft/error → scheduled (clearing stale
  error); clearing time flips scheduled → draft.
- `DELETE /announcements/{id}`
- `POST /announcements/{id}/post-now` — 409 if sent; arm with `post_at=now`.
- `POST /announcements/{id}/clone` → `{ok, id}`.

### 4. Panel — `src/web_server/static/js/panels/announcements.js`

`export function mount(container)`; helpers: `api/apiPost/apiPut/apiDelete/esc`,
`toast/confirmDialog`, `loadChannels/mountChannelPicker`,
`renderLoading/renderEmpty`, `mdToHtml`. Template: `role-menus.js`.

- **Queue** (draft/scheduled/error): status badge, title, channel,
  "posts <guild-local datetime>" formatted from post_date/post_time_min (not
  epoch), mention badge, error text; Edit / Post now (confirm) / Delete
  (confirm). Server-time hint line.
- **Editor** (inline): channel picker, title, body textarea, image URL,
  accent hex ("blank = server branding"), plain-text line ("shown above the
  embed — mentions ping from here"), mention select + role select (visible
  only for kind=role), date + time inputs; Save (draft if no time), Post now,
  Cancel.
- **Live preview**: 200ms debounce; mention pill + escaped plain-text line,
  then `.dp-embed` (border color = valid accent_hex else default) with
  `.dp-title` / `.dp-desc` via `mdToHtml`, image if `^https?:`. Reuse `dp-*`
  CSS (app.css:3495-3530); new classes under an `.ann-` prefix.
- **Sent history**: newest-first; sent time, channel, "Open in Discord"
  (jump_url), Clone (opens clone in editor), Delete.

**Modify `static/js/app.js`**: one Config-section item
`{ id: "announcements", label: "Announcements", module: "./panels/announcements.js", adminOnly: true }`.

Syntax-check with the gjs `Reflect.parse` one-liner; JS appears after the
next service restart (user pushes that button).

### 5. Docs (same commit)

- New spec `docs/announcements.md` (schema, lifecycle, late-fire, claim
  semantics, API, panel) + `docs/INDEX.md` entry (Reference).
- `static/manual.html` new section (`<h2 id="announcements">`): queueing,
  preview, safe mentions, server-local time, Post now ≤1 min, 2-hour missed
  window, clone-from-history. Matching entry in
  `static/js/panels/help-sections.js` (anchor must match).
- README: no change (no slash commands).

### 6. Tests (same commit; new service file must map or the gate hard-fails)

- `tests/unit/test_announcements_logic.py` (model:
  `test_scheduled_games_logic.py`): `compute_post_at` at offsets 0 / −7 /
  +5.5, midnight & 23:59 edges; `build_announcement_message` — each mention
  kind's content prefix **and** exact AllowedMentions flags, plain-text-only,
  content None when both absent, embed fields, accent override vs fallback.
- `tests/cogs/test_announcements_loop.py` (model:
  `test_scheduled_games_loop.py`, `sync_db_path` fixture): due row sends with
  expected kwargs and ends `sent` with ids; past-window row → error, no send;
  30-min-late row sends; unreachable channel → error; send raising Forbidden
  → error and second pass sends nothing; claim on already-sent returns False;
  drafts/error rows never in `fetch_due`; post-now-armed row fires.
- `tests/web/test_announcements_routes.py` (model:
  `test_photo_challenge_routes.py`): draft create; scheduled create computes
  post_at with guild offset; 400s (past time, half a schedule, bad kind,
  role-without-id, bad channel, empty content, bad hex/image); 422 unknown
  field; PUT flips scheduled↔draft; PUT/post-now on sent → 409; clone resets
  schedule/sent fields; list stringifies snowflakes + correct jump_url +
  tz_offset_hours; non-admin 403.

### 7. Commit

Worktree; no service restarts. Subject:
`Announcements: web-scheduled one-shot channel posts`. Body ends with
`Testing:` checkboxes — queue+preview renders; scheduled post lands on time
in the right channel; only the picked mention pings; jump link works; clone
works; Post now ≤1 min; bot-down-past-window row shows missed error.

## Key template files

- `src/bot_modules/services/scheduled_games_service.py` → new service
- `src/web_server/routes/scheduled_games.py` + `routes/qa.py` → new route
- `src/web_server/static/js/panels/role-menus.js` → preview pattern
- `src/migrations/046_scheduled_games.sql` → schema/comment style
- `src/dungeonkeeper/__main__.py:317` → loop registration
