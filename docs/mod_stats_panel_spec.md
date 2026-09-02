# Moderator Stats Panel — Feature Spec

**Status: Reference** (built 2026-09-01).

A sticky panel in a mod-only channel showing the server's day so far: today's
message traffic hour by hour against a band over the last **8 matching
weekdays**, moderator presence across those same hours, and XP broken down by
source over 30 days and over the guild's whole history. Four figures sit above
them. Read-only.

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

One PNG carrying three stacked rows, rendered by
`activity_graphs.render_mod_stats_panel`:

| Row | Subject | Comparison |
|---|---|---|
| 1 | Today hour by hour, **+ moderators present** | p25–p75 band + median over the last **8 matching weekdays** |
| 2 | XP by source, last **30** days | — |
| 3 | XP by source, **all time**, weekly | dotted rule where each source began |

Row 1 comes from `query_activity_overlay(period="day", same_weekday=True)`, the
same call the dashboard's *Today vs Recent Days* view makes — so the panel and
the report can never disagree about what a day looked like. Messages mode only;
bots excluded, matching the Activity report's own default.

**Why matching weekdays and not the last N days.** Weekday rhythm dominates this
server: a Wednesday drawn against "the last 8 days" is drawn against five
weekdays and two or three weekend days, so a server that gets busy at weekends
reports a crash every Monday and a boom every Saturday, both artefacts of the
calendar. Eight matching weekdays reaches back 56 days — far enough for a band,
near enough that the server it describes is still recognisably this one.
`overlay_stride_days` already stepped a week at a time for this; the panel had
simply never asked for it.

The member median strides to match (`query_partial_day_members(stride_days=7)`).
Both halves of the panel have to compare today with the *same* past, or they
disagree for a reason no reader can see.

**One image, not three.** Discord gives an embed a single image slot. The figure
is deliberately **narrow** (6in) rather than short: Discord scales an embed
image to the message column, so its displayed width is fixed at roughly 400px on
a phone whatever we render, and only the ratio of type size to figure width
survives that. 11pt on 6in lands at ~10px on the phone; the 9in-wide 8pt this
panel used before landed at ~5px. Height is free — the reader can scroll — so
rows are given room instead of being compressed. Each row keeps its **own**
y-axis.

**Mod presence is a second line on row 1, sharing its zero baseline — not a
second y-axis.** A dual-scale chart lets the author put any two series into any
relationship just by choosing the scales, so its crossing points carry a meaning
nobody put there. But moderators peak at a handful an hour against hundreds of
messages, so an unscaled line would lie flat on the floor and say nothing. The
line is therefore rescaled to share the axis and **the scaling is named in the
legend** ("Mods around (0-5, rescaled)"). What the reader is invited to read off
it is the *shape* — when were mods around, against when was it busy — while the
magnitudes are printed as words in the block above the picture, where they need
no scale at all.

### Who counts as a moderator, and what counts as present

Members holding any of the guild's configured `mod_role_ids` **or**
`admin_role_ids`, read **live from Discord** rather than from `role_events`,
which is an append-only log of grants and so cannot answer who holds a role now.
Bots are dropped.

This is a **narrower** circle than the Mod Coverage report's, which counts anyone
with Manage Messages. The two will not agree, and that is the intended reading:
this panel answers "was one of the people we appointed around?", not "could
anyone present have deleted a message?".

Present means **posted or reacted** in that hour — the two tables are `UNION`-ed
on `(hour, user)`, so a mod who does both inside one hour counts once and a mod
who only reacts still counts. A moderator reading a channel and reacting is
watching it; counting only messages reports the quiet half of a mod team as
absent. `reaction_log` begins 2026-04-05, which is a hard floor on any
reaction-derived figure, but presence is a *today* measure and never reaches it.

Hours the day has not reached are `None`, not 0, so the row stops at the live
edge instead of drawing a cliff to the floor. An unconfigured moderator role
returns all-`None` with `configured=False`, which is deliberately
distinguishable from a day on which no moderator showed up: "nobody was
watching" and "we were never told who the moderators are" want different
responses from whoever reads the panel.

### The XP stacks

Row 3 is `query_xp_activity_with_breakdown(resolution="day")` — the dashboard's
own call. Row 4 is `query_xp_all_time_with_breakdown`, added for this panel: the
only graph in `activity_graphs` whose window does **not** roll, starting at the
guild's first XP event and bucketing weekly to now, so the bar count grows with
the server. Weeks rather than days because a year is 365 marks in a 400px-wide
picture; weeks rather than months because the shape this exists to show
disappears into a monthly average. It reads through `_xp_row_source`, so it
stays correct once raw `xp_events` below the 90-day retention boundary have been
pruned to `xp_daily`.

**The dotted rules are what make row 4 honest.** XP sources did not all exist for
the whole period — on the home guild `text` and `reply` run from 2026-02-07,
`image_react` and `voice` from 2026-03-03, and `quest` and `reaction_given` only
from mid-July 2026. Without a marker, the stack gaining two colours in one week
reads as a surge in activity when it is the bot changing what it pays for. Each
rule is drawn in its own source's colour, so it needs no separate legend, and
sources folded into "Other" get no rule — a rule in a colour that appears
nowhere in the legend is one the reader cannot attribute to anything.

**Six colours and no seventh.** `static/js/charts.js` states the rule: past six
categorical slots, adjacent classes blur whatever hue you pick, so the tail folds
into "Other". `grant` is the tail (41 events in the guild's whole history).
Fixing this exposed a defect on *both* surfaces: `quest` and `reaction_given`
have paid XP since July and had no palette slot on either the dashboard or the
bot, so the 3rd- and 5th-largest sources were rendering as the same anonymous
grey. They now take the last two slots, in `activity_graphs` and in
`panels/activity.js` together. The Python palette had also drifted out of the
lock-step its own comment claimed — it still carried Discord's brand hues, which
fail the palette validator's lightness band — and is now the shared `ROLE_COLORS`
the dashboard uses. Stacked segments carry a 2px surface gap (`edgecolor=_BG`);
that is the secondary encoding the palette's one weak pair depends on, and it is
load-bearing, not decorative.

### The figures underneath

```
Messages today    1,204  ▲ 12%
Members talking      87  ▼ 3.3%
On track for     ~1,650  usual 1,480
Mods around           6  peak 5 in an hour
```

The mod row is omitted entirely when no moderator role is configured. The peak
is not decoration: a bare "6" invites the reader to decide for themselves
whether six is a lot, and the house standard set by `attention_report.py` and
followed by the Contributors panel is that a count comes with its denominator.

**Every comparison is part-day against part-day.** Today at 09:00 has lived nine
hours; a day of history has lived twenty-four. Comparing the two would report a
collapse in activity every morning and a recovery every evening, both artefacts
of the clock. So "usual" sums the band's median over *only the hours today
has lived*, and the members figure truncates every comparison day at the same
local hour.

*On track for* is the one number that deliberately reaches past now: today's
actual total plus what the remaining hours usually hold. Anchored on what today
has done rather than scaled up from it, so a busy morning does not multiply
itself across a quiet night.

A percentage is printed only when there is something to divide by. Below
`MIN_BAND_PERIODS` (3) comparable days the overlay suppresses the band, and
every "vs usual" figure goes with it — the panel says
"No comparison yet — needs 3 past Wednesdays of history." rather than comparing
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

None. No new tables and no per-user rows, so nothing for
[data_register.md](data_register.md). Every number is derived at read time from
`processed_messages`, `reaction_log` and `xp_events`, all of which are already
registered. The panel stores no moderator identity: presence is counted and the
identities discarded inside the query.

## Non-goals

- **Stats *about* moderators.** Row 2 says how many were *around*, which is a
  coverage question. Who takes action, and how much, is
  [mod_coverage](reporting_spec.md) and Mod Workload; this panel adds no third
  definition of the work itself. It does add a second definition of *moderator*
  (appointed roles, vs Mod Coverage's Manage Messages) — named on the chart and
  above, because two panels silently counting different circles is worse than
  two panels openly counting different circles.
- **Naming individual moderators.** A count per hour, never a leaderboard. Who
  was around is a coverage signal; who was around *least* is a performance
  review, and a sticky panel in a shared channel is the wrong place for one.
- **XP in the overlay.** Row 1 stays messages-only: XP cannot answer
  hour-of-day past the 90-day raw retention, and "how busy is the room" is a
  different question from "what is the bot paying for".
- **Moderation *load*.** Warnings, jails and tickets were considered and left
  out: on the home guild that is 5 warnings and 9 jails across five months, so
  any trend line is flat at zero and a panel that says nothing for weeks is a
  panel nobody keeps reading.
