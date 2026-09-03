# Ping Response — spec

**Classification: Reference** (built 2026-08-30). See [INDEX.md](INDEX.md).

Answers one question: **when the server pings a role, does anybody turn up?**

## Why it needed new capture

Nothing in the schema recorded a role ping. `message_mentions` is
`(message_id, user_id)` — it records *user* mentions only, and a role ping has
no `user_id`, so `<@&role_id>` left no trace anywhere. That is the whole reason
this feature has an ingest change and a new table rather than being a report
over existing data.

The *response* side needed nothing new: `messages` and `reaction_log` are
already retained and already indexed for the scan
(`idx_messages_guild_channel_ts`, `reaction_log`'s primary key).

## Design: record the stimulus, compute the response

`ping_events` (migration 198) stores only that a ping happened. Turnout is
**not** a stored column — it is computed at read time.

| Column | Meaning |
|---|---|
| `message_id` | PK. The pinging message. |
| `guild_id`, `channel_id`, `author_id` | Where, and who pinged. |
| `role_ids` | JSON array — one message can ping several roles. |
| `everyone` | `@everyone` / `@here`, which have no role id. |
| `source` | `member` \| `bot` (Dungeon Keeper) \| `external` (another bot) \| `game_start`. |
| `ref` | The `game_id`, for `game_start`. |
| `ts` | When. |

Two consequences, both deliberate:

* **The response window is a live control**, not a constant frozen into a
  column by whatever sweep filled it. A game start ("now") and an announcement
  ("today") want very different windows, and the panel can ask for both.
* **Backfilled and live pings are measured by the same code.** History and the
  present are never counted two different ways.

The cost is a join per report instead of a lookup. Measured on 60 days of prod
data (3,271 pings, 297k messages): **0.11s** to build the whole report. The
report cache absorbs the rest.

An alternative — a sweep loop filling `follow_msgs` / `follow_authors` /
`success` columns, as `revive_events` does — was considered and rejected for
those two reasons. `revive_events` predates the retained `reaction_log` scan
this relies on.

### Why `source` splits bots three ways

Measured on 60 days of prod — 3,271 pings, backfilled:

| Sender | Pings | Median turnout | Ignored |
|---|---|---|---|
| Members | 873 | 5 | 12.9% |
| Dungeon Keeper | 967 | 0 | 62.2% |
| One other bot (all in its own channel) | 1,431 | 1 | 43.0% |
| **Blended** | **3,271** | **1** | **40.7%** |

Only 27% of role pings are sent by a person. A two-way member/bot split would
still have merged this bot's own announcements with an unrelated bot's wordle
notifications, and the blended row is actively misleading in both directions:
it reports members' pings as far less effective than they are (13% ignored, not
41%) and this bot's as far more (62%, not 41%). The three-way split is what
makes either number readable.

`SOURCE_FILTERS` names the groupings the panel's **Sent by** control offers, so
the frontend never hardcodes source strings. Classification uses
`ctx.bot.user.id`, never a name — the bot's account is named per guild
("Poppy" on the main server), so any name-matching heuristic would silently
mislabel every one of its own pings as external.

## Capture

**Live** (`message_store.store_message`, called from all three `on_message`
persistence paths in `events_cog`): reads Discord's structured
`message.role_mentions` / `message.mention_everyone`, **never the text**. Two
reasons this matters:

* it works at storage level `none`, where there is no content to parse — this
  is the "derive metadata at ingest" rule in CLAUDE.md doing real work;
* Discord only populates those fields when the ping actually fired, so a member
  typing `@everyone` without the permission to use it is correctly *not*
  recorded as having pinged the server.

**Game pings** (`scheduled_games_service`): after the launch announcement
sends, `record_game_start_ping` ties the ping to its `game_id`. It is
write-then-stamp because the launcher and the ingest path race — whichever
arrives first, the row ends with `game_start` and the id. Losing that race
would cost the report its only column that says whether anyone *played*.

**Backfill** (`backfill_ping_events`, run from `scripts/backfill_ping_events.py`;
it was a button on the Admin Backfill panel until that panel was retired
2026-09-03):
parses `<@&id>` / `@everyone` out of `messages.content`. Idempotent. It sees
strictly less than live capture — only channels where content was retained, and
it cannot tell a real `@everyone` from someone typing the words. Both the panel
and the manual say so.

## Measurement

Turnout for a ping is `|posters ∪ reactors|` within the window:

* **posters** — distinct authors of messages in that channel with
  `ping.ts < m.ts <= ping.ts + window`;
* **reactors** — distinct reactors on the ping message itself.

Excluded: the pinger (never their own response), the ping message itself, and
bots by default (`include_bots` opts in, like every other dashboard metric).

Deduped across the two, so posting *and* reacting counts once, and posting ten
times counts once. Raw message volume rides alongside as a separate column,
because "one person said forty things" and "forty people said one thing" are
different nights.

Implemented as two joined queries, not a correlated subquery per ping: SQLite
has no LATERAL, so `COUNT(*) FROM (… UNION …)` cannot reference the outer row.
Joining and deduping in Python also keeps the union in one readable place.

For `source='game_start'`, `players` is the roster —
`games_game_history.player_count` for a finished game, the live lobby payload's
roster for one still running. Absent (rendered `—`, never `0`) when neither
exists: "we don't know" must not read as "nobody came".

## Surface

Report panel `ping-response`, under **Reports → Engagement**. Route id is the
bare feature name per `dashboard_ia.md`.

Controls: range, response window, **Sent by** (anyone / Dungeon Keeper /
members / other bots), and the standard include-bots opt-in — which is a
different question from *Sent by* and applies to the *responders*.

The endpoint answers **200 with zero pings**, never 404, when nothing has been
recorded. "Nothing has been pinged yet" is a legitimate answer for a report
whose table starts empty; the panel renders it as an empty state naming the
backfill. Reporting it as an error is what put `greeter-response` on the
browser sweep's `KNOWN_LOAD_ERRORS` allowlist, and this deliberately doesn't
repeat that.

Tiles (pings, % that got a response, median turnout, ignored entirely); a
single-series line chart of average turnout per day (one axis — ping volume is
a different scale and lives in the numbers table, never a second y-axis);
by-role and by-channel breakdowns; and the recent-ping table with a jump link.

A ping naming two roles counts **once** in the totals and appears under **both**
roles in the by-role breakdown, so those counts can exceed the headline. Both
roles really were pinged; the by-role table answers "does pinging this role
bring anyone", which is per-role by construction.

## Privacy

`ping_events` names the **pinger**, not the pinged. `author_id` is in
`SUBJECT_ID_COLUMNS`, so the export finds it; `purge_user_data` deletes by
`guild_id` + `author_id`. Purged, not preserved — there is no Art 17(3) ground
for an analytics table, and erasing a member's pings does not disturb anyone
else's turnout, which is derived at read time and never denormalized.

**No per-responder rows exist.** "Who personally answered which ping" was
deliberately scoped out: counts answer the question, and storing the responder
list would be a materially heavier footprint than was asked for. If that
changes, it needs its own `data_register.md` row and its own decision.

Register row: `docs/data_register.md`. Member-facing disclosure:
`manual.html` §Your Data & Privacy.

## Tests

`tests/test_ping_tracker_logic.py` (79 cases) — both extractors (structured and
text), the ping predicate and window clamp, sender classification and filter
resolution, insert idempotency, the stamp's unknown-source guard, the
write-then-stamp race in both orders, dedup across posts/reactions, the pinger
and bot exclusions, window boundaries, channel scoping, the source filter
reaching *both* read queries, roster lookup and its "unknown ≠ zero" case,
backfill idempotency and its refusal to downgrade a live-captured row, the
purge, and the report aggregation including the multi-role double-count and
guild-local day bucketing.
