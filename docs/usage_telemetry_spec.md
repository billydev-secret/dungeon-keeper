# Usage Telemetry

**Status:** Reference — matches current behavior.

Records which slash commands members run and which dashboard panels get opened,
so the admin report can answer "what actually gets used" and, more usefully,
"what never does".

## Storage

One table, `usage_events` (migration `139_usage_telemetry.sql`):

| column | notes |
|---|---|
| `guild_id` | every query is guild-scoped |
| `kind` | `command` or `panel_view` |
| `name` | command name space-joined through parent groups (`quest board`), or dashboard panel id (`economy-stats`) |
| `user_id` | raw Discord id |
| `channel_id` | commands only; `NULL` for panel views |
| `ok` | `0` when the invocation raised |
| `extra` | JSON, currently unused — a hook for future per-event detail |
| `ts` | unix seconds |

Two indexes: `(guild_id, ts)` — for `totals`, the only query with no `kind`
predicate — and `(guild_id, kind, ts, name, user_id, ok)` for everything else.

`ts` sits **before** `name`/`user_id` on purpose. Put a high-cardinality column
ahead of it and `ts >= ?` stops being a range seek and degrades to a
post-filter, so a 7-day report reads every row ever recorded for that kind.
Since rows are kept forever, that turns report cost into a function of total
history rather than of the window. Measured at 90k rows: per-name rollup
70.5ms → 4.5ms, per-user 15.8ms → 3.9ms.

A third `(guild_id, kind, name)` index was tried and **removed**. It makes the
never-used lists' distinct-name lookups ~100x faster in isolation, but the
planner then prefers it for the per-name rollup as well (it avoids a sort),
which puts that query back to a 70ms full-history scan. Across a whole report
render that was ~188ms versus ~83ms. The never-used lists have to read all
history by definition, so they absorb a scan rather than making every other
query pay one. Adding indexes here is not free in the other direction either —
each one is write cost on the hot path of every slash command.

### Why not `audit_log`

`audit_log` is the moderation trail — 946 rows over ~108 days at the time this
shipped, unindexed, read as a human-scale history. Command telemetry alone runs
a couple hundred rows/day, so within a month it would have been ~95% of that
table and every report `GROUP BY` would scan the moderation record. Keeping them
separate also means telemetry can be pruned or exported without touching
moderation history.

## Capture

**Commands** — `bot_modules/cogs/usage_telemetry_cog.py`, two plain listeners:

* `on_app_command_completion` → `ok=1`
* `on_app_command_error` → `ok=0`

`on_app_command_error` is **not** a discord.py built-in. `CommandTree.error` is
a single-slot hook (registering a second handler replaces the first), and
`events_cog._on_tree_error` already owns it. Rather than wrapping that slot —
which would have made telemetry silently depend on `events_cog` loading first in
`__main__.extension_names` — `events_cog` re-broadcasts the error as a normal
bot event that any cog can listen for. `tests/cogs/test_usage_telemetry_cog.py`
asserts that cross-cog contract directly, since breaking it would zero the error
count with no other symptom.

Writes go through `asyncio.to_thread`. `open_db` sets `busy_timeout=30000`, so a
write landing behind another writer would otherwise stall the whole bot —
heartbeat included — for up to 30s, on the hot path of every slash command.

Guards: DM invocations (no `guild_id`) and bot actors are dropped, and every
write is wrapped so a telemetry failure can never break the command it measures.

**Known gap:** a cog that defines its own `cog_app_command_error` and swallows
the error — `confessions_cog`, `risky_roll_cog`, `advisor_cog` — never reaches
`tree.on_error`, and its completion listener doesn't fire either, so a *failed*
invocation of those commands records no row at all. This undercounts errors
rather than miscounting successes. Fix by making those handlers re-raise if
their error rates ever become a question worth answering.

**Panel views** — `POST /api/telemetry/panel`, pinged fire-and-forget by
`app.js` after each successful panel mount. The user id comes from the session
cookie, never from the request body, and the panel id must match
`^[a-z0-9][a-z0-9-]*$`.

Ingest is gated on **`moderator`**, not "any authenticated user". Any guild
member can authenticate — the Wellness nav section is `perms: []` — so an
unprivileged writer could post a well-formed id for a panel they cannot see and
make a genuinely never-opened panel look used, silently invalidating the one
number this report exists to produce. The cost is that members' own Wellness
panel views are not recorded; dashboard usage was always the mod/admin
question, and a trustworthy deletion list is worth more than member wellness
traffic. `app.js` skips the ping for non-mods, so the 403 only reaches a caller
bypassing the dashboard.

Deliberately **not** a request-logging middleware: `home.js`, `mod-tickets.js`,
`mod-jails.js`, `economy-stats.js`, `config-ai.js` and `config-bump-tracker.js`
all poll on timers, so a middleware would mostly record background refreshes
from an open tab and "how often is the dashboard used" would measure tab uptime
instead of people. One row per panel open is ~50–200 rows/day against
~5–15k for every request.

## Report

`GET /api/reports/usage?days=<1-365>` (admin only), rendered by
`static/js/panels/usage-telemetry.js` under **Dev → Command & Panel Usage**.
(Moved here from a one-item Reports heading in the IA3 nav pass — its job,
spotting dead commands and panels, is owner tooling rather than a report a
moderator needs day to day.)

The headline is the never-used pair, because it is the only output that tells
you to *delete* something:

* **Commands never run** — the live `bot.tree` walk minus everything ever
  recorded. Empty in standalone mode (no bot attached), so an unavailable tree
  reads as "nothing is unused" rather than "everything is".
* **Panels never opened** — computed **client-side**. The nav is defined in
  `app.js`, so the browser is the only source of truth for the full panel list
  (~139 ids / 2.4 KB, growing with every panel — too much for a query string
  behind a proxy). The endpoint returns `seen_panels`, the much smaller set of
  names actually recorded, and the panel subtracts it from its own list, read
  via `static/js/nav-registry.js`.

Both are judged against **all** recorded history, not the selected range — a
command last run 90 days ago is unpopular, not unused. Everything else on the
panel (per-name tables, busiest members, dashboard visitors, daily line, hour
histogram) respects the range picker.

Bots are excluded by default via `core/bot_exclusion`, matching every other
dashboard metric. In practice this removes nothing — bots cannot invoke slash
commands and panel views require an OAuth login — but the convention holds here
rather than being the one place it silently doesn't.

Snowflakes are serialized as strings (`str(user_id)`); a bare JSON number above
2^53 would be silently rounded by the browser.

## Privacy & retention

Per-user attribution with **no routine pruning** — an explicit choice, matching
the server's existing retain-for-moderation posture (see `privacy_spec.md`).

* `/delete_me` and `/delete_user` do **not** touch this table. They never
  touched server-side data — they only delete Discord-side messages, and that
  retention is disclosed in the confirmation prompt before the member confirms.
* `privacy_service.purge_user_data()` — the out-of-band hard-erasure path used
  for legal/GDPR requests — **does** clear it. Since nothing prunes this table
  on a schedule, that function is the only thing that ever removes a row, which
  is what `tests/test_privacy_service.py::test_deletes_usage_events` guards.

Volume at the time of writing: ~200–500 command rows/day plus ~50–200 panel-view
rows/day on a ~150-active-member server, or roughly 150k rows and ~15 MB/year.
If that stops being acceptable, the cheapest change is a nightly sweep that rolls
rows older than N days into a per-day summary rather than deleting them.

## Tests

* `tests/test_usage_telemetry_service.py` — aggregation, windowing, bot
  exclusion, timezone bucketing, and the pure `unused_names` set difference.
* `tests/cogs/test_usage_telemetry_cog.py` — which invocations become rows, the
  guards, and that `tree.on_error` chaining doesn't swallow the prior handler.
* `tests/web/test_usage_telemetry_routes.py` — ingest, admin gate, day clamping,
  never-used lists, and snowflake stringification.
* `tests/test_privacy_service.py` — the purge wiring.
