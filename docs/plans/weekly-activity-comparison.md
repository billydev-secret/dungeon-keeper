# Weekly activity comparison — this week against a band of the last N

**Status: built 2026-08-27.**

Billy's ask: "compare this week's server activity to historical data overlaid
per week … another histogram view with configurable windows. Maybe I want to
compare this week's server activity against the last 6, or 12."

This extends the existing **Activity** report rather than adding a second
report: the route (`GET /api/reports/activity`), the moderator gate, the
member/channel/exclude/bots filters and the caption+legend+table furniture all
carry over unchanged. Only the bucketing and the chart form are new.

## The three decisions (asked and answered before designing)

1. **Shape: this week vs a band.** One bold line for the current week, plus a
   shaded p25–p75 envelope and a median line over the last N weeks. Not N
   overlaid lines and not grouped bars.
2. **Within-week axis: hour of week (168 points).** Shows the daily rhythm as
   well as the weekly one.
3. **Window: a preset picker (4 / 6 / 8 / 12 / 26), capped at 12 in XP mode.**
   Not deep-linked — it defaults fresh each time (12 weeks / 28 days).
4. **A daily view too**, asked for mid-build: *Today vs Recent Days*, the same
   band one period down, x = hour of day (24 points), presets 7 / 14 / 28 / 90.
   The period is a parameter (`day` | `week`) rather than two code paths — the
   query, the percentiles and the chart are shared, and only the anchor
   (local midnight vs local Sunday midnight) and the point count differ.

### Why the band, and why it dissolves the palette problem

A twelve-line overlay needs twelve identities, and `charts.js` deliberately
stops at six (`ROLE_COLORS`) because "an all-warm palette cannot separate six
series; that is arithmetic, not taste". Recency is a *sequential* quantity, so
the alternative was a one-hue ramp — but a ramp of twelve steps against a dark
surface cannot hold both the lightness band and the chroma floor at every step,
and the palest steps fall under the 3:1 contrast line.

Collapsing history into an envelope removes the problem instead of managing it.
The chart has **two identities** — "this week" and "the last N weeks" — so it is
an ordinary 2-slot categorical assignment:

| Mark | Colour | Role |
|---|---|---|
| This week (line, 2px, no fill) | `ROLE_COLORS[0]` amber `#B58030` | the subject |
| Median of last N (line, 2px, dashed) | `ROLE_COLORS[2]` teal `#00A29C` | the comparison |
| p25–p75 of last N (fill) | teal at `1f` alpha | the spread |

Validated with the dataviz validator against the dashboard's dark surface
(`CHART_SURFACE` `#2b2d31`):

```
[PASS] Lightness band     all 2 inside L 0.48–0.67
[PASS] Chroma floor       all 2 >= 0.1
[PASS] CVD separation     worst adjacent ΔE 13.9 (protan) · 21.8 (tritan)
[PASS] Normal-vision      worst adjacent ΔE 19.2
[PASS] Contrast vs surface  all 2 >= 3:1
```

Amber+teal was chosen over amber+slate (`#2167A1`), which passes the separation
checks but WARNs on contrast at 2.31:1. A neutral gray band
(`SERIES_OVERFLOW` `#6b7076`) was rejected outright: it FAILs the chroma floor
and WARNs on contrast, so the median line would have read as a grid line.

Dashed median + solid current is the secondary encoding, so identity is never
carried by colour alone. The existing `renderChartLegend` and
`renderChartTable` cover the legend and table obligations.

## The data floor — this is the part that bites

**Messages mode** reads the full `processed_messages` archive. **XP mode** reads
`xp_events`, which migrations 186/187 put on a 90-day raw retention with a daily
rollup (`xp_daily`) below the boundary.

`activity_graphs._xp_row_source()` already unions the rollup, so the *existing*
time-bucketed XP resolutions reach past 90 days correctly. **This view must not
use it.** The rollup stamps each synthetic row at its UTC day's midnight, which
carries no hour-of-day information at all — every pre-boundary row would land in
one hour-of-week bucket, producing a fake spike at midnight on each weekday and a
fake trough across the other 23 hours. And because the main guild runs at
**UTC−7**, that midnight lands at 17:00 the *previous* local day, so the spike
would be misplaced by a day as well.

So the hour-of-week query clamps `since_ts` to the retention boundary and reads
raw `xp_events` only. This is the same call the XP hour-of-day/day-of-week
histograms already made — `XP_HISTOGRAM_WINDOW_DAYS = RAW_RETENTION_DAYS = 90`,
with `xp_histogram_window_label()` saying so in the caption. This view follows
that precedent rather than inventing a second policy.

90 days = 12.85 weeks, which is why **12 is the XP cap** for the weekly view and
why 26 is offered in Messages mode only. The daily view's 90-day preset sits
exactly on the boundary, so no daily window is out of XP's reach. The picker
disables the out-of-reach options and says why, and `overlay_period_cap()`
enforces it on the route — the greying-out is a courtesy to the reader, not the
enforcement, so a hand-built URL gets a shortened window rather than a silently
truncated band.

Reach available today (prod, both archives start 2026-02-07): **~29 weeks**. 52
weeks does not exist yet in either mode and is not offered.

Note that prod has not yet applied 186/187 (`xp_daily` and `xp_rollup_state` are
absent), and deletion ships OFF behind a per-guild dial — so raw XP currently
reaches all 29 weeks. The cap is still correct: it is built against the retention
policy, not against today's un-pruned table, or the view would start lying the
first time the pruner runs.

## Week boundaries

Weeks are guild-local, from `get_tz_offset_hours(conn, guild_id)`, exactly as the
existing resolutions do. "This week" starts at the most recent local Sunday
00:00. Bucket index is `(local_dow * 24) + local_hour`, 0–167.

**The current week is partial.** At Wednesday 14:00 the subject line covers 87
of 168 buckets and must *stop* there — filling the rest with zeros would draw a
cliff to the floor and make every remaining hour read as a collapse in activity.
The band still spans all 168, so the reader sees where the week is going. Buckets
after "now" are `null`, which Chart.js leaves unplotted.

The comparison weeks are the N *complete* weeks before this one; the current
partial week is never part of its own baseline.

## Percentiles

Per bucket, over the N historical weeks: `p25`, `p50`, `p75` by linear
interpolation on the sorted values. Weeks with no data at all (before the archive
starts) are excluded from the sample rather than counted as zero — otherwise the
band is dragged toward the floor by weeks that never happened, and the median of
12 weeks where only 6 exist is meaningless. If fewer than 3 complete weeks are
available the band is suppressed and only the current-week line is drawn, with
the caption saying so.

## What this view drops, and why

- **The XP per-source breakdown** (`series`). The series axis is now weeks-vs-now;
  source stacking would need a third dimension. `series` comes back empty and the
  panel's `hasSeries` path is not taken.
- **The unique-members sub-chart.** `show_members` is False — a per-hour distinct
  count over a partial week against a band of medians is not a number worth
  drawing.
- **The time slider.** The x-axis is within-week, not a timeline; the slider is
  hidden for this resolution.

## Build

**`services/activity_graphs.py`**
- `OverlayPeriod` (`day` | `week`), `MIN_BAND_PERIODS`, `OVERLAY_MAX_PERIODS`.
- `overlay_period_start(now, utc_offset_hours, period)` — the local anchor, and
  the single place the timezone is reasoned about. Because the window starts on
  a local period boundary, `created_at - since_ts` measures elapsed time from
  it, so **both** the period index and the hour within the period fall out of
  one subtraction — no `strftime`, no offset arithmetic in SQL.
- `overlay_labels`, `_percentile`, `overlay_period_cap`.
- `query_activity_overlay(...)` → `OverlayResult`, one function for both modes
  and both periods, reusing `_append_exclusions` unchanged.

**`services/reports_data.py`**
- `get_activity_data` grows `compare_periods: int = 12` and an early branch to
  `_get_overlay_data`, returning the new `ActivityData` keys (`band_low`,
  `band_mid`, `band_high`, `periods_sampled`, `x_label`). Existing keys keep
  their meaning: `counts` is the current period. `counts` widens to
  `Sequence[float | None]` — covariant, so the timeline branches still hand
  over a plain `list[float]`.
- `ActivityResolution` is a separate alias from `Resolution`, which keeps
  meaning "something `_BUCKET_BUILDERS` can build".

**`routes/reports.py`**
- `resolution` Literal gains `week_overlay`; new `compare_weeks: int = 12` query
  param, validated and clamped server-side (the UI cap is a courtesy, not the
  enforcement); added to the `cached_run_query` key.
- `ActivityResponse` gains the optional band fields.

**`static/js/panels/activity.js`**
- Two `RESOLUTIONS` entries, a `Compare to` picker shown only for those, and
  `_makeOverlayChart` — the band as a p75/p25 dataset pair with `fill: '+1'`
  between them.
- The caption's like-for-like figure: the legend shows the current period's
  total against *the median truncated to the same hour*, because comparing a
  partial Wednesday against a whole typical week is exactly the misread this
  chart exists to prevent.

**`static/js/charts.js`** — two small, general additions to `renderChartLegend`:
`skipLegend` (a band is a dataset *pair*; only one should speak for it, or you
can toggle off half a band) and `legendValue` (summing a dataset is right for a
count and wrong for a percentile).

**Tests** (`tests/test_activity_graphs.py`, `tests/test_reports_data.py`)
- Bucket index correctness at a non-zero offset — the UTC−7 case, asserting an
  event at 23:00 local Saturday lands in bucket 167 and not bucket 6 of Sunday.
- Partial current week trails `null`, not 0, past "now".
- Percentiles over a known fixture; absent weeks excluded from the sample.
- Fewer than 3 complete weeks ⇒ band suppressed.
- XP mode never reads `xp_daily`: with a rollup boundary present and N=12, the
  emitted SQL targets bare `xp_events` and `since_ts` is clamped to the boundary.
- Route clamps `compare_weeks` above the mode's cap.

**Docs**: `manual.html` (Help → Reports → Activity) gets the new view and states
the 12-week XP reach; `docs/INDEX.md` unchanged (no new spec); no new table, so
no `data_register.md` row.
