# DAU Page: Day-of-Week Fixes + Voice/DAU% Graphs

**Date:** 2026-04-26
**Scope:** `web/static/js/panels/health-dau-mau.js`, `services/health_metrics.py`, `web/routes/health.py`, `web/static/js/panels/health-heatmap.js`

## Problem

The "Average DAU by Day of Week" chart on the DAU/MAU panel is wrong in three ways, and the user wants two new daily charts on the same page (count of voice-active users, DAU/MAU % over time). While we are fixing the DOW arithmetic in `compute_dau_mau`, the same bug exists in `compute_heatmap` and is in scope.

### Bug 1 — Day labels are off by one

`services/health_metrics.py:207` uses:

```sql
CAST(((ts % 604800) + 345600) / 86400 AS INTEGER) % 7 AS dow
```

The `+ 345600` (4 days) shifts `ts=0` (Thursday Jan 1, 1970 UTC) into bucket `4`. Combined with the labels array `["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]` (which assumes Mon=0), this maps Sunday's data to "Mon", Monday's to "Tue", and so on — every weekday is rendered one day ahead of its true label.

The same formula and the same labels appear in `compute_heatmap` (`services/health_metrics.py:251-265`, `:289-298`, `:282`) and the matching frontend (`web/static/js/panels/health-heatmap.js:5`), so the heatmap is shifted the same way.

### Bug 2 — DOW averaging method is incorrect

`services/health_metrics.py:206-216`:

```sql
SELECT ..., COUNT(DISTINCT author_id) AS cnt
FROM messages WHERE ts >= :30_days_ago GROUP BY dow
```

then

```python
"avg_dau": round(dow_map.get(i, 0) / 4.3, 1)
```

`COUNT(DISTINCT author_id)` grouped by weekday counts unique users across **all four-or-five Mondays combined**, then divides by ≈30/7. If the same 50 people show up every Monday, this reports ≈12 instead of 50. True average DAU is "DAU per day, averaged across the Mondays in the window."

### Bug 3 — No timezone alignment

The query buckets on raw UTC seconds. Reports already plumb `ctx.tz_offset_hours` for the same need (`web/routes/reports.py:80`); the health route does not. Per the user's standing preference (auto-memory `feedback_daily_graphs_start_6am.md`), daily graphs should anchor a "day" to 06:00 local, not midnight.

### Missing graphs

The DAU panel only shows a single 7-day voice-active number (`services/health_metrics.py:131-136`) and a 30-day daily DAU sparkline. The user wants:
- A 30-day daily series of unique voice-active users (mirrors the existing DAU sparkline).
- A 30-day daily series of DAU/MAU % (the headline stickiness number, plotted over time).

## Design

### Conventions

- **Day-of-week indexing:** Sun=0..Sat=6 throughout, matching SQLite's `strftime('%w')` and `services/activity_graphs.py:22`. The off-by-one bug is fixed by switching to this convention rather than by patching the existing arithmetic. Backend `dow_names` and frontend label arrays are reordered to `["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]`.
- **Day boundary for daily aggregations:** 06:00 local (per user preference). Implemented as a `+ tz_offset_hours*3600 - 6*3600` shift before bucketing by day. So a "Monday" runs 6am Mon → 6am Tue local.
- **Day boundary for the heatmap:** midnight local (no 6am shift). The heatmap is a 7×24 hour grid; shifting the day boundary by 6h would place "Mon 12am" data in a cell labeled "Sun 11pm", which is strictly worse than the bug. Hours within the heatmap are independent buckets and don't aggregate across the boundary.

### `compute_dau_mau` rewrite (`services/health_metrics.py`)

New signature accepts `tz_offset_hours: float = 0.0`.

**1. Calendar-day-aligned 30-day DAU sparkline** (replaces the current rolling-24h sparkline at lines 119-129). Buckets each message's `ts` into a local-6am-aligned day index, counts DISTINCT authors per day, and emits 30 values ending with "today" (the current local-6am-to-now partial day).

**2. Voice-active sparkline** — same shape as DAU sparkline, sourced from `xp_events WHERE source='voice'`, counting DISTINCT `user_id` per local day. Emitted as `voice_sparkline: int[30]`.

**3. DAU/MAU % sparkline** — for each of the last 30 days `D`:
- `DAU(D)` = distinct authors active on local day D
- `MAU(D)` = distinct authors active in the trailing 30-day window ending at the end of day D
- ratio = `DAU(D) / MAU(D) * 100`

Computed in Python from a single 60-day pull of `(ts, author_id)` rows. We bucket each row into its local day, then for each target day D compute DAU(D) from a single bucket and MAU(D) from the union of buckets `[D-29..D]`. Avoids 30 separate SQL round-trips. Emitted as `dau_mau_sparkline: float[30]`.

**4. DOW averages — correct calculation:**

```sql
WITH daily AS (
  SELECT CAST((ts + :shift_secs) / 86400 AS INTEGER) AS day_idx,
         COUNT(DISTINCT author_id) AS dau
  FROM messages
  WHERE guild_id = ? AND ts >= ?
  GROUP BY day_idx
)
SELECT (day_idx + 4) % 7 AS dow,   -- epoch day 0 = Thursday = 4 in Sun=0 system
       AVG(dau)        AS avg_dau,
       COUNT(*)        AS day_count
FROM daily
GROUP BY dow
```

Where `:shift_secs = int(tz_offset_hours*3600) - 6*3600`. Returned as `day_of_week: [{day, avg_dau}]` with the new Sun-first labels.

### `compute_heatmap` fix (`services/health_metrics.py:245-324`)

- Add `tz_offset_hours: float = 0.0` parameter; compute `offset_secs = int(tz_offset_hours*3600)` (no 6am shift).
- Replace the dow expression in the server-wide query (`:251-260`) and the per-channel query (`:290-299`) with:
  ```sql
  CAST(strftime('%w', datetime(ts + :offset_secs, 'unixepoch')) AS INTEGER) AS dow
  ```
- Replace the hod expression with the same shifted-time approach so peak/quiet slots are local time:
  ```sql
  CAST(strftime('%H', datetime(ts + :offset_secs, 'unixepoch')) AS INTEGER) AS hod
  ```
- `dow_names` (`:282`) → `["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]`.

### `web/routes/health.py` plumbing

Both `health_dau_mau` (`:623`) and `health_heatmap` (`:646`) read `tz_offset_hours = getattr(ctx, "tz_offset_hours", 0.0)` (matching the pattern in `web/routes/reports.py:80`) and pass it into the compute functions.

### `web/static/js/panels/health-dau-mau.js` updates

- DOW chart: render the new `day_of_week` payload as-is (now Sun-first).
- Add a "30-Day Voice-Active Trend" line chart card below the existing DAU trend, using `d.voice_sparkline` and the mauve palette color `#B36A92`.
- Add a "30-Day DAU/MAU %" line chart card. Y-axis label `%`; tooltip formatted with one decimal.

### `web/static/js/panels/health-heatmap.js` updates

- `DOW` constant (`:5`) → `["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]`.
- `computeInsights` weekday-vs-weekend calc: in the new ordering, weekdays are rows `[1..5]` (Mon-Fri) and weekend is rows `[0, 6]` (Sun, Sat). Update accordingly:
  ```js
  const wdAvg = (dayTotals[1]+dayTotals[2]+dayTotals[3]+dayTotals[4]+dayTotals[5]) / 5;
  const weAvg = (dayTotals[0] + dayTotals[6]) / 2;
  ```

### Tests

Add a deterministic fixture-based test under `tests/web/test_health_routes.py` (or a sibling unit test on `compute_dau_mau` / `compute_heatmap` directly):

- Insert messages at known UTC timestamps that fall on a known local Sunday and a known local Wednesday (with `tz_offset_hours = 0` for simplicity).
- Assert: `day_of_week[0].day == "Sun"` and its `avg_dau` reflects the Sunday inserts; `day_of_week[3].day == "Wed"` reflects the Wednesday inserts.
- Assert: heatmap `grid[0][h]` (Sunday row) reflects Sunday inserts; `grid[3][h]` (Wednesday row) reflects Wednesday inserts.

This proves the off-by-one fix and the labels are aligned.

## Out of scope

- The `incident_detection.py` baselines (`:35`, `:137-143`, `:246`) are stored under their own `(hour_of_day, day_of_week)` key. They use a separate code path and aren't user-facing labels — fixing them is out of scope.
- `services/health_service.py:65-69` defines the same column shape; not a UI concern.
- Behavior of the rolling-24h sparkline change: the new calendar-day sparkline will shift "today's" value slightly compared to the old rolling-24h ending-at-now value. This is the intended consequence of fix-3 (calendar-day alignment).
