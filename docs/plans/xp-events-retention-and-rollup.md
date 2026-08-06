# xp_events retention — the rollup the readers can union

**Status: design, 2026-08-06.** Closes the deferral on item 9 of
[../reviews/2026-08-06-review-synthesis.md](../reviews/2026-08-06-review-synthesis.md)
(dbperf P1 in [../reviews/2026-08-06-sweep-reliability-dbperf.md](../reviews/2026-08-06-sweep-reliability-dbperf.md)).
Nothing here is built yet. Stage 1 is safe to build and ship on its own;
Stage 3 is the one that deletes rows and must not run until Stage 2 has
been correct in prod for a while.

## Why this was deferred rather than swept

Every other retention item in the 2026-08 review was a sweep: find rows
past a cutoff, delete them, done (`greeting_watch`, `rules_events`
dismissals, `games_external_messages` — all shipped in 49a02867). This
one looked the same and is not, because `xp_events` is not a log nobody
reads. It is the *only* source for every XP question that isn't "what is
this member's current total", and several of its readers reach back
further than any retention window worth having.

Delete first and the failure is silent: an all-time leaderboard quietly
starts meaning "leaderboard since the cutoff", and nothing errors.

## What is actually there (prod, 2026-08-05)

Measured against a `backup()`-API snapshot of the live 746 MB DB.

| | |
|---|---|
| Rows | 1,022,477 |
| Span | 2026-02-07 → 2026-08-05 (~6 months) |
| Table bytes | 63.5 MB |
| `idx_xp_events_lookup` | 45.4 MB |
| `idx_xp_events_channel` | 35.7 MB |
| **Total** | **~145 MB — about 19% of the whole database** |
| Distinct users, all time | 475 |
| Guilds | 7 (one holds 928k of the 1.02M rows) |
| Rows older than 90d | 526,622 (51.5%) |
| Rows older than 180d | 0 |

By source: text 425,755 · reply 274,124 · image_react 175,900 · voice
102,893 · reaction_given 42,441 · quest 1,327 · grant 37.

Note the span. The table is six months old, so today "last 365 days" and
"all time" return the same numbers — which is exactly why a mistake here
would not be noticed for another six months, and then would look like a
bug in the leaderboard rather than a retention decision.

## The readers, and which ones a naive delete would break

36 `FROM xp_events` queries across ten modules — `core/xp_system.py`,
`services/activity_graphs.py`, `services/reports_data.py`,
`services/health_metrics.py`, `services/economy_loop.py`,
`services/inactive_report_service.py`, `services/pools_metrics.py`,
`cogs/jail_cog.py`, `web_server/routes/home.py`,
`web_server/wellness_routes/api.py`. They fall into three groups.

### Safe — windowed at or under 90 days

- `activity_graphs.query_xp_activity` / `query_xp_histogram` (+ the
  `_with_breakdown` pair) at `hour` (24h), `day` (30d) and `week` (12w =
  84d) resolutions.
- `xp_cog` `/xp` leaderboard at the `hour` / `day` / `week` / `month`
  timescales — all pass `since_ts` ≤ 30 days.
- `reports_data.get_xp_leaderboard_data(days=N)` for the windowed case.
- `activity_graphs` line 882's per-member XP-by-source split (bounded by
  the caller's window).
- `health_metrics` (7d voice-active, 30d voice intervals, 30d XP by
  user), `economy_loop` (an explicit `created_at >= ? AND < ?` day
  range), `pools_metrics` (the daily market's XP line, `>= :cut`),
  `home.py` (dashboard tiles, `>= ?`), `wellness_routes/api.py`
  (`now - days*86400`). All bounded well inside 90 days.

### Already immune

- `reports_data.get_xp_leaderboard_data(days=None)` — the all-time
  dashboard leaderboard reads cumulative `member_xp`, not events. This is
  the pattern the rest of this plan copies: aggregate answers come from
  an aggregate table.
- `count_xp_events` — one `log.debug` line in `events_cog`. It counts what
  is in the table; after pruning it will count what is in the table. No
  fix needed, but see Stage 3 for the log line's wording.

### Broken by a 90-day delete

1. **`/xp` at `year` (365d) and `alltime` (`since_ts=None`)** —
   `get_xp_leaderboard`, `get_xp_distribution_stats` and
   `get_user_xp_standing`, each per source. This is the reader the
   original design note meant. Today it needs per-user-per-source sums
   over an arbitrary window.
2. **`activity_graphs` at `month` resolution** — `_month_buckets` builds
   twelve **30-day rolling** buckets from `now - 360 days`. Every row
   older than 90 days sits inside that reach, i.e. 51.5% of the table.
   These queries also filter on `channel_id` and `user_id` and compute
   `COUNT(DISTINCT user_id)` per bucket, so the rollup has to keep both
   dimensions or the graphs lose their filters.
3. **`get_time_to_level_details`** (`reports.py:849`, `days=None` → the
   "All Time" card) — a window function over individual events computing
   when each user's cumulative XP crossed the level-5 threshold, minus
   their first-ever event. Needs event ordering, not sums.
4. **`jail_cog:1637`** — all-time XP-by-source for one member on the mod
   profile. Per-user-per-source sums, unbounded.
5. **`has_any_xp_events`** (`xp_cog:220`) — the existence check that
   decides whether `/xp` renders leaderboards at all. A guild whose
   activity is entirely older than the cutoff would fall through to the
   empty-state embed. It degrades gracefully rather than lying (the
   fallback checks `member_xp` and says "existing XP totals predate the
   event ledger"), but it would be wrong: the ledger did have them.
6. **`inactive_report_service.channel_activity_map`** — `MAX(created_at)`
   per (user, channel) with **no time filter**: "when was this member last
   active in this channel", feeding the inactive/prune report. This is the
   reader most directly opposed to retention — its entire purpose is
   surfacing people whose last activity was long ago, so pruning old rows
   erases exactly the timestamps it exists to report. It is also why the
   rollup carries `last_at`, and a second reason `channel_id` stays in the
   key.

## The design

One rollup table, keyed at the finest granularity any reader filters on,
and readers that **union** it with the raw tail.

```sql
CREATE TABLE xp_daily (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    source     TEXT    NOT NULL,
    channel_id INTEGER,              -- NULL preserved, see below
    day        TEXT    NOT NULL,     -- 'YYYY-MM-DD', UTC
    xp         REAL    NOT NULL,
    events     INTEGER NOT NULL,
    first_at   REAL    NOT NULL,
    last_at    REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id, source, channel_id, day)
);
```

### Why this key, and what it costs

Cardinality of the candidate keys, measured on the real table:

| Key | Rows | vs raw |
|---|---|---|
| guild, user, source, day | 33,801 | 3.3% |
| **guild, user, source, channel, day** | **87,266** | **8.5%** |
| guild, user, source, channel, month | 23,174 | 2.3% |
| guild, source, channel, day | 13,311 | 1.3% |

Dropping `channel_id` would be 2.6× smaller and would break every
channel-filtered graph and the channel/user exclusion lists
(`_append_exclusions`). At 87k rows the full-fidelity key is cheap enough
that trading a reader's correctness for it would be a bad deal. Day
granularity over month for the same reason: months cannot answer "last
365 days" without splitting a bucket, and the difference is 87k rows
against 23k.

`channel_id` is nullable and stays nullable — 273,419 rows (27%) have no
channel, a pre-existing gap from when the column was added and backfilled
only where `processed_messages` had a match (`xp_system.py:506`). Note
SQLite lets NULLs coexist in a PRIMARY KEY, so the rollup writer must
`GROUP BY` in a way that folds NULL channels into one row per
(user, source, day) rather than relying on PK conflict resolution.

### Expected saving

After a 90-day retention: ~496k raw rows + 87k rollup rows against
1,022,477 today, so roughly **43% fewer rows** and, since the two indexes
scale with the table, something like **145 MB → ~85 MB**. Worth doing,
but less than the "halves the largest object" the sweep estimated — that
figure assumed events could be replaced by aggregates outright, and the
360-day graphs are why they cannot.

### The one thing a daily rollup cannot do exactly

`activity_graphs`' `day`/`week`/`month` buckets are **rolling windows
anchored to `now`**, not calendar periods — the bucket edge for a
360-day-wide `month` view falls at an arbitrary instant. A rollup day
that straddles an edge has to be attributed whole to one side.

Error is bounded by one day per bucket edge on a 30-day bucket, and only
for the pre-retention portion (everything inside 90 days is still served
from raw events, exactly). `COUNT(DISTINCT user_id)` stays exact because
`user_id` is in the key.

**This is the decision Ben has to make**, because it changes what a report
shows:

- **(a) Accept the bounded skew.** Month-resolution graphs stay 360 days
  wide; buckets older than 90 days can misattribute up to one day of XP
  at each edge. Nobody reads a 12-month trend line to the day.
- **(b) Snap the pre-retention buckets to UTC day boundaries.** Exact
  against the rollup, at the cost of bucket edges that no longer line up
  with "exactly 30 days ago".
- **(c) Keep 360+ days of raw events and only roll up beyond that.** No
  skew at all, no saving today either — nothing in the table is older
  than 180 days, so this defers the whole problem for six months.

Recommendation: **(a)**, with the skew documented in the graph's own
subtitle. (c) is defensible as a "do nothing yet" answer and is worth
saying out loud: the table is 145 MB and the disk is not full. The reason
to build now rather than in six months is that the readers are easier to
fix while the data still fits in one design's head.

### Time-to-level

The one reader that genuinely needs event ordering. It is also the one
whose *output* is already coarser than its input: `reports.py` reports
`mean_days`, `median_days`, `stddev_days`, `mode_days` — days, from a
query that computes seconds.

So the daily rollup is sufficient in principle: recompute the running sum
over `xp_daily` and resolve each level crossing to the day it happened in.
Then `seconds_to_level` becomes day-resolution for the pre-retention
portion and stays exact inside 90 days. The route's arithmetic does not
change.

`get_time_to_level_seconds` — the sibling that returns bare seconds — has
**no callers at all**, not even a test, and should be deleted rather than
ported. `get_oldest_xp_event_timestamp` has no production callers either
(only its own test in `test_xp_system.py`); it would have needed a union
arm too, so decide whether it earns one before writing it.

## Stages

**Stage 1 — the rollup, additive and inert.** Migration creating
`xp_daily`; a `xp_rollup_service.py` that (re)builds a day's rows from
raw events idempotently; a backfill for the whole history; a daily job
that rolls up completed days. Nothing reads it, nothing is deleted. Ships
on its own and can sit in prod for a week proving the numbers.

**Stage 2 — readers union.** Each of the six broken readers gains a
rollup arm: raw events for `created_at >= boundary`, `xp_daily` for days
strictly older, added together. Boundary is one setting, and it is *not*
the retention cutoff — it is the same value so the union is seamless, but
naming it separately is what lets Stage 3 change retention without
touching read paths. Tests assert reader output is **identical** before
and after the union with a full raw table (the rollup arm returns nothing
when nothing is pruned, so equality is exact and testable).

**Stage 3 — retention.** Only now does anything delete: raw rows older
than the boundary, swept from the existing XP loop, with the rollup
proven to cover them. A `PRAGMA wal_checkpoint(TRUNCATE)` after the first
big prune (the erasure runbook's note applies — 526k deletions will not
shrink the file on their own), and the `count_xp_events` debug line
reworded so "XP event rows" is not mistaken for "XP events ever".

**Stage 4 — GDPR.** `xp_daily` is per-user data and joins
`purge_user_data` with the rest of the XP family, plus a register row in
`../reviews/2026-08-05-gdpr-register.md`. Cheap, but it is the exact
mistake the register exists to catch: a new per-user table that nobody
decided about. Do not let Stage 1 land without at least the register row.

## Testing

Per CLAUDE.md the unit under test is the service layer, so:

- Rollup correctness: a day of synthetic events rolls to one row per
  (user, source, channel); re-running is a no-op; NULL channel folds to
  one row rather than many.
- Union equality: for each of the six readers, seed events across the
  boundary, then assert the unioned reader matches the pre-union reader
  run against un-pruned raw data. This is the test that would have caught
  a silent all-time leaderboard.
- `channel_activity_map` specifically: a member whose only activity in a
  channel predates the boundary still reports that activity's real
  timestamp, not "never".
- Retention: a row inside the boundary survives, one outside is deleted,
  and the rollup covering the deleted row is present *before* the delete
  runs (order matters — the sweep must refuse to delete a day it has not
  rolled up).
- The existing `has_any_xp_events` guard: a guild whose only activity is
  older than the boundary still reports XP data.
