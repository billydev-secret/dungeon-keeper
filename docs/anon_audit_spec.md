# Anonymous Features Audit

**Status: Reference** — describes what runs today.

A DB-backed audit trail for the anonymous surfaces in the games suite, with an
admin-gated dashboard panel and a configurable retention sweep.

## Why it exists

The anonymous games audited through `games/utils/audit.py`, which posted an
embed to the channel in `games_audit_channel` and wrote **nothing** to the
database. That channel is unset by default, and `log.txt` is truncated on every
boot — so an anonymous AMA question could leave no trace anywhere. Clapback's
anonymous mode, WYR anonymous votes and posed questions, and Spin the
Compliment had no audit at all.

The channel mirror is still there; it is just no longer the only record.

## What is and isn't covered

| Feature | Trail |
|---|---|
| AMA, Free For All, Hot Takes, Fantasies, Clapback, WYR, Compliment | `anon_audit_log` (this spec) |
| Confessions | `anon_audit_log` under the `confessions` slug, surfaced on its own Confessions Audit panel |
| Whisper | `whispers` + Whisper Audit panel |
| Guess (`/guess confess`) | `guess_audit_log` via `guess_cog._do_audit` + Guess Who Audit panel |

Whisper and Guess are deliberately **not** migrated onto this table. Their
tables are load-bearing for the features themselves — whisper state, round
history — so putting them under a retention purge would break them.

Confessions was originally excluded for the same reason, and that reason still
holds: `confession_threads` carries thread identity and reply routing, and is
**not** migrated here. Instead confessions *also write* an audit row (migration
`151`), which decouples the two lifetimes. This matters because
`confession_threads` is purged at a seven-day operational TTL, so while the
Confessions panel read it, the moderation record was a rolling week — tolerable
only while a Discord mod-log channel held the permanent copy. That channel is
now optional and off by default (it de-anonymises every confession to whoever
can read it), so the audit row is the durable trace.

Confessions therefore shares this table's guild-wide retention dial rather than
having its own. One window for every de-anonymising record is the intended
posture; a per-feature column is the change to make if that ever stops holding.

## Schema

Migration `145_anon_audit_log.sql`.

`anon_audit_log` — `id`, `guild_id`, `feature`, `event`, `actor_id`,
`target_id`, `game_id`, `message_id`, `channel_id`, `extra` (JSON),
`created_at`. Two indexes: `(guild_id, created_at)` for the default listing and
the purge, `(guild_id, feature, created_at)` for the filtered view. No
`(guild_id, actor_id)` index — see the migration's note on why migration 139's
third index was a mistake.

`anon_audit_config` — `guild_id`, `retention_days` (default 90, `0` = forever).

### There is no content column

This is the central design decision and it **matches Confessions exactly**: the
table stores a `message_id` pointer, and the dashboard `LEFT JOIN`s `messages`
to recover the text. Consequences, all accepted knowingly:

- Content appears only at guild message-storage level `all`
  (`message_store.guild_retains_content`; the default is `none`). The live
  server runs at `all`.
- Content disappears if that level is lowered or `purge_guild_message_content`
  runs.
- Surfaces that never produce a guild message have **no recoverable text at
  all**: a screened AMA question the host rejects (DM'd, never posted), a WYR
  question that is queued rather than posted, Hot Takes and Fantasies entries
  held in the game payload until the reveal. Those rows are who-and-when only.

The audit log is therefore a record of *who and when*, with *what* available
opportunistically through the same content-retention setting that governs every
other message on the server.

### Privacy posture

`actor_id` is the real member behind an anonymous post — that is the point of
the table, and what makes it sensitive. Admin-gated on the dashboard; there is
no member-facing read path. Rows are cleared by
`privacy_service.purge_user_data` (matching both `actor_id` and `target_id`),
the out-of-band hard-erasure path, in addition to the routine retention sweep.

## Events

| Feature | Events |
|---|---|
| `ama` | `question_asked` (extra: `mode` unfiltered/screened, `question_idx`, `delivered_to_host`), `question_approved`, `question_rejected`, `question_answered`, `question_passed`, `hot_seat_skipped`, `hot_seat_changed` |
| `ffa` | `reply_posted` |
| `hottakes` | `take_submitted` |
| `fantasies` | `entry_submitted` |
| `clapback` | `answer_submitted` — **only when the game's `anonymous` option is on**; with attribution on there is no anonymity to account for |
| `wyr` | `vote` (only while the round is anonymous), `question_posed`, `voters_revealed` |
| `compliment` | `pairings_generated` |

On moderation events (`question_approved`/`question_rejected`,
`voters_revealed`, `hot_seat_skipped`) `actor_id` is the **moderator**, not an
anonymous poster, and `target_id` is the member the action concerned.

Note that AMA's screened approve/reject handlers run in the host's DMs, so
`interaction.guild` is None there — the guild comes from the game channel.

## Write path

`games/utils/audit.py::audit_anonymous` is the single entry point. It writes the
DB row (via `anon_audit_service.record_event` on a thread) and then mirrors to
the audit channel **only when `content` is passed** — metadata-only events like
a vote or a pass skip the mirror so the channel doesn't fill with contentless
embeds.

`record_event` swallows and logs every DB error. An audit failure must never
take down the member-facing flow it observes: a member should not lose the
question they typed because the table was locked. Same contract as
`guess_cog._write_audit`.

Call sites pass `message_id` **after** the message is posted — that is why the
AMA audit call moved below `mark_question_approved`.

## Retention

`anon_audit_purge_loop` (registered as a `startup_task_factory` in `__main__`,
alongside the other owner-less background loops) runs `purge_expired` every 6
hours. Guilds with no config row use the 90-day default, so a server that never
opens the panel is still bounded; a guild set to `0` is skipped. The cutoff
comparison is strict (`created_at < cutoff`), so a row landing exactly on the
boundary survives.

`purge_expired` is a single statement correlating each row against its own
guild's window. A per-guild loop needed a `SELECT DISTINCT guild_id` over the
whole table first, and SQLite has no loose index scan — that walked every index
entry four times a day just to rediscover a handful of ids.

WYR votes dominate the row count. The panel's feature filter is how a mod
narrows past them.

## Dashboard

**Moderation → Audit Logs → Anonymous Features** (`mod-anon-audit.js`,
admin-only).

- `GET /api/moderation/anon-audit` — `limit`, `offset`, `feature`, `actor_id`.
  Reads through `list_events`/`count_events` rather than its own SQL, so the
  filter logic has one definition. Returns entries plus the filter options,
  built from `KNOWN_FEATURES` and labelled from `games.constants.GAME_NAMES` so
  the panel names each game the way the rest of the dashboard does.
- `GET|PUT /api/moderation/anon-audit/retention`.

Each entry carries `is_mod_action`, derived server-side from `MOD_EVENTS` — the
panel badges on that flag rather than keeping its own copy of which event names
mean "a mod acted on someone else's anonymous post". Event names are
`EVENT_*` constants in the service, not string literals at the call sites, for
the same reason. All snowflakes are returned as strings.

`limit` is clamped at both ends: SQLite reads a negative `LIMIT` as unbounded,
so `?limit=-1` would otherwise dump every de-anonymising row in one response.

## Tests

- `tests/test_anon_audit_service.py` — write path including the error-swallow
  contract, filtered reads, and the retention boundaries (keep-forever,
  exactly-at-cutoff, default-applies-without-a-config-row, per-guild isolation).
- `tests/web/test_anon_audit_routes.py` — the content join (present, null when
  no message, null when the guild stores no content), snowflake strings,
  filters, pagination, guild scoping, retention round-trip, auth.
