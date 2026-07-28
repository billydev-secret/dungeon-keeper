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

Indexed on `(guild_id, ts)`, `(guild_id, kind, name, ts)`, and
`(guild_id, user_id, ts)`.

### Why not `audit_log`

`audit_log` is the moderation trail — 946 rows over ~108 days at the time this
shipped, unindexed, read as a human-scale history. Command telemetry alone runs
a couple hundred rows/day, so within a month it would have been ~95% of that
table and every report `GROUP BY` would scan the moderation record. Keeping them
separate also means telemetry can be pruned or exported without touching
moderation history.

## Capture

**Commands** — `bot_modules/cogs/usage_telemetry_cog.py`:

* `on_app_command_completion` → `ok=1`
* `bot.tree.on_error`, *chained* rather than replaced → `ok=0`

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
cookie, never from the request body.

Deliberately **not** a request-logging middleware: `home.js`, `mod-tickets.js`,
`mod-jails.js`, `economy-stats.js`, `config-ai.js` and `config-bump-tracker.js`
all poll on timers, so a middleware would mostly record background refreshes
from an open tab and "how often is the dashboard used" would measure tab uptime
instead of people. One row per panel open is ~50–200 rows/day against
~5–15k for every request.

## Report

`GET /api/reports/usage?days=<1-365>&panels=<csv>` (admin only), rendered by
`static/js/panels/usage-telemetry.js` under **Reports → Bot Usage**.

The headline is the never-used pair, because it is the only output that tells
you to *delete* something:

* **Commands never run** — the live `bot.tree` walk minus everything ever
  recorded. Empty in standalone mode (no bot attached), so an unavailable tree
  reads as "nothing is unused" rather than "everything is".
* **Panels never opened** — the `panels` query param minus everything ever
  recorded. The nav is defined in `app.js`, so the browser is the only source
  of truth for the full panel list; it passes its ids in via
  `static/js/nav-registry.js`. Omitting the param yields an empty list.

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
