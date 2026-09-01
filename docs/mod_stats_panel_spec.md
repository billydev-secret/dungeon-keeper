# Moderator Stats Panel — Feature Spec

**Status: Reference** (built 2026-09-01).

A sticky panel in a mod-only channel showing the server's day so far: today's
message traffic hour by hour against a band over the previous 8 days, the same
day against a band over the previous 30, and three figures underneath. Read-only.

The reason it exists is distribution, not analysis. The Activity report on the
dashboard has drawn this chart since 2026-08-27; a chart nobody opens the
dashboard for is a chart nobody reads, so this puts it where the mod team
already is.

## Commands

None. The only setting is which channel it goes in, and that lives on the
dashboard (**Reports → Activity**, "Post this to a mod channel") through the
shared panel registry — see `services/panel_registry.py`, key `mod-stats`.

## Behavior

### What it draws

One PNG carrying two stacked charts, rendered by
`activity_graphs.render_overlay_panel`:

| Chart | Subject | Comparison |
|---|---|---|
| Top | Today, hour by hour | p25–p75 band + median over the previous **8** days |
| Bottom | The same day | p25–p75 band + median over the previous **30** days |

Both come from `query_activity_overlay(period="day")`, the same call the
dashboard's *Today vs Recent Days* view makes — so the panel and the report can
never disagree about what a day looked like. Messages mode only; bots excluded,
matching the Activity report's own default.

**One image, not two.** Discord gives an embed a single image slot. Stacking the
charts here is what lets a reader drop their eye from one band to the next on a
shared x-axis, rather than comparing two pictures Discord laid out on its own
terms. Each chart keeps its **own** y-axis: sharing one would let the wider
30-day band set the scale and flatten the tighter one, which is the comparison
the panel exists to make.

The palette is the dashboard chart's, validated in
[plans/weekly-activity-comparison.md](plans/weekly-activity-comparison.md)
against a dark surface — amber for the day in progress, teal for the band, with
a dashed median so identity is never carried by colour alone.

### The figures underneath

```
Messages today    1,204  ▲ 12%
Members talking      87  ▼ 3.3%
On track for     ~1,650  usual 1,480
```

**Every comparison is part-day against part-day.** Today at 09:00 has lived nine
hours; a day of history has lived twenty-four. Comparing the two would report a
collapse in activity every morning and a recovery every evening, both artefacts
of the clock. So "usual" sums the 8-day band's median over *only the hours today
has lived*, and the members figure truncates every comparison day at the same
local hour.

*On track for* is the one number that deliberately reaches past now: today's
actual total plus what the remaining hours usually hold. Anchored on what today
has done rather than scaled up from it, so a busy morning does not multiply
itself across a quiet night.

A percentage is printed only when there is something to divide by. Below
`MIN_BAND_PERIODS` (3) comparable days the overlay suppresses the band, and
every "vs usual" figure goes with it — the panel says
"No comparison yet — needs 3 past days of history." rather than comparing
against nothing. A median of zero (04:00 on a quiet server) prints no percentage
either.

Days with no rows at all are dropped from the member median rather than counted
as zero: a day predating the archive would otherwise drag the baseline down for
a reason that is a fact about when logging started.

### Placement and refresh

Built on `core.sticky.StickyPanel`, so it re-posts to the bottom of its channel
when someone talks beneath it. `restick_on_bot` is off — it only chases human messages, so
`sticky_registry` records it as a **warn**, not a block, for anything else
sharing the channel.

**Deleting the message is how it is removed.** The shared post control carries
no Remove button, so the refresh runs with `repost_if_missing=False`: a panel
that is gone is retired (its stored ids cleared) rather than healed back into
place at the top of the hour. Posting again from the dashboard is the way back.
Nothing is lost by retiring instead of healing — this is a read-only chart, so a
deleted one costs discoverability rather than a capability the way a
button-carrying board would.

It redraws **on the hour**, driven by `tasks.loop(time=…)` with all 24 UTC hours
rather than an `hours=1` interval: an interval loop fires an hour after boot, so
a restart at 14:20 would leave the panel repainting at :20 past forever. The
charts bucket by hour, so this is the finest cadence that changes anything.
Guilds on a whole-hour UTC offset — the ones this bot serves — see the repaint
land on their own hour boundary. There is one extra pass on boot, so a restart
does not leave stale numbers up until the next hour.

A sticky repost rebuilds the panel, and a busy mod channel can do that every few
seconds. The rendered PNG is therefore cached per guild against the data
signature, so a repost re-uploads the picture it already has instead of putting
another matplotlib run on the process-wide render lock.

### Images through the sticky machinery

`PanelContent` gained an optional `PanelImage` for this. It holds **bytes**, not
a `discord.File`: a `File` wraps a single-use stream, and a panel's content is
built once but may then be posted, retried, or edited — `place` can even run to
completion after its caller was cancelled. A fresh `File` is built per request.

Edits pass `attachments` explicitly, including an empty list for an imageless
panel: omitting the argument keeps whatever the message already carries, so a
panel would otherwise show last hour's chart under this hour's numbers forever.

## Permissions

Reading it is whoever can see the channel — put it in a mod channel. Posting or
moving it is **admin**, enforced by `POST /api/panels/{key}/post` like every
other channel panel.

## Configuration

| Key | Meaning |
|---|---|
| `mod_stats_panel_channel_id` | Where the panel lives. Unset/0 means not posted |
| `mod_stats_panel_message_id` | The panel's current message |

Both are read **strictly guild-scoped** (`allow_legacy_fallback=False`): the
config table's `guild_id=0` fallback would otherwise hand a second server the
home guild's message to edit, in the home guild's channel.

## Stored data

None. No new tables, no per-user rows, nothing for
[data_register.md](data_register.md) — every number is derived at read time from
`processed_messages`, which is already registered.

## Non-goals

- **XP mode.** Offered on the dashboard, not here: XP is capped by the 90-day
  raw retention (the overlay cannot read hour-of-day out of the daily rollup)
  and answers a different question from "how busy is the room".
- **Stats *about* moderators.** Who is around and who takes action are
  [mod_coverage](reporting_spec.md) and Mod Workload, which are deliberately
  measured over different circles of "moderator". This panel adds no third
  definition — the mod team is the audience, not the subject.
- **A same-weekday band.** Considered for the 30-day chart, since weekday rhythm
  dominates this server and a month of history mixes weekends into a weekday.
  Left as consecutive days: the two bands are meant to read as "recently" and
  "this month", and it is a one-parameter change if the wide band proves to be
  noise on real data.
