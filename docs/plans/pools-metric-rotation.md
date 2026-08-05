# Pools — a roster of metrics, one drawn per day

**Status: built 2026-08-03.** Extends the market shipped in
[casino-classics-and-prediction-market.md](casino-classics-and-prediction-market.md)
Stage 2, which bet a single hardcoded metric.

## The problem

Pools ran one market: over/under on the day's net change in the economy.
It works, but it is the same question every day, so the only thing that
varies is the number. The ask was for "different things to rotate
through".

## What shipped

Eleven metrics in `services/pools_metrics.py`. One is drawn uniformly at
random each guild-local day, never the same one two days running. The
round row records which metric it bet (migration 148), because settlement
*recomputes* the outcome from history and the draw has moved on by then.

| Metric | key | per-member cap | line on 2026-08-03 |
|---|---|---|---|
| Economy net change | `economy_net` | — (structural exclusion) | 5,329.5 |
| Messages sent | `messages` | 30 | 1,186.5 |
| Members who posted | `posters` | 1 (inherent) | 78.5 |
| Media posted | `media` | 10 | 120.5 |
| Happy messages | `joy` | 20 | 630.5 |
| Reactions added | `reactions` | 20 | 703.5 |
| Members who reacted | `reactors` | 1 (inherent) | 65.5 |
| XP earned | `xp` | 100 | 2,435.5 |
| Cats caught | `cats` | 10 | 110.5 |
| QOTD answers | `qotd` | 5 | *sits out* |
| Casino handle | `handle` | 1,000 | 6,297.5 |

## Why these, and why the caps

Manipulation resistance is the design axis, inherited from the original
metric. The economy metric was safe structurally — pools' own stakes and
payouts are excluded from it, so betting cannot move the thing being bet
on. **No count metric has that defence.** "Messages sent today" is
farmable by anyone willing to type.

So every count metric carries a **per-member cap applied inside its own
series query**, and that is what makes it bettable. Measured on prod: one
member has posted 432 messages in a day through ordinary use, so an
uncapped metric hides a farmer completely. Capped at 30, a bettor's entire
ceiling is under 3% of a ~1,190 line — more expensive to coordinate than
the bet can return.

Caps are **code constants, not guild config**. Changing a cap
retroactively changes what every past day measured, so a round opened
under one cap could settle under another. Same reasoning that freezes the
line onto the round row.

The two distinct-member metrics (`posters`, `reactors`) need no cap: one
member moves them by exactly one, so shifting them means recruiting other
humans — which is an incentive worth having rather than one to defend
against.

`handle`'s cap is deliberately independent of the admin-tunable
`daily_wager_cap`: an admin turning that off must not silently disarm the
metric's guard.

## Validation

Every metric was backtested against real history by replaying the rolling
line (median of the 7 days before each day, +0.5) and counting how often
the day went over. A metric that is not close to 50/50 is not a market.

| Metric | settled days | over% |
|---|---|---|
| `messages` | 113 | 46% |
| `posters` | 113 | 48% |
| `joy` | 113 | 49% |
| `media` | 93 | 44% |
| `xp` | 113 | 42% |
| `reactions` | 101 | 41% |
| `reactors` | 101 | 41% |

`cats` (7 settled days), `handle` (3) and `qotd` (2) had too little
history to judge. They are in the roster anyway because the eligibility
rule holds them out until they earn their way in — see below.

## Eligibility: two ways to sit out

1. **Fewer than 7 completed days.** Inherited — no line can be derived.
2. **A zero (or negative) day in the trailing window**, count metrics only.

The second rule is new and does real work. A zero day means the activity
did not happen at all, which nearly always means the feature behind it was
dormant. A line drawn across dormancy prices *whether the bot ran*, not
how members behaved.

It is what keeps `qotd` out of the roster today: QOTD answers are zero on
days no mod posts a question, so that market is really "did someone post a
QOTD" — which the person who posts it already knows. That is insider
information rather than a manipulation cost, and no cap fixes it. If QOTD
becomes a reliable daily ritual, the metric joins the rotation on its own
with no code change.

The economy metric is exempt: a net change of zero is a real reading of a
busy day whose flows balanced, not a silent one.

## Interaction with quests

Five metrics already have active quests pointing at them (`message_sent`,
`reaction_given`, `media_post`, `cat_catch`, `qotd_reply`). That overlap
is benign: a quest target of 5 media posts against a line of 120 is noise,
and the member earns it whether or not they bet.

**`handle` is the exception, and it is a live constraint on quest design.**
House edge is the only thing making casino handle expensive to move. A
"wager N Petals" quest refunds that cost, so a bettor could take *over*,
farm the handle, and be paid by the quest for doing it.

> **Casino quests must key on wins, losses or sessions played — never on
> wager volume or stake size.** If a volume-based casino quest ships,
> `handle` must come out of the roster in the same change.

## Shape of the code

- `pools_metrics.py` — the registry. Each `MetricSpec` carries its key,
  label, member-facing question, unit, chart kind and a `series()`.
- Count metrics fill `DayMetric` as `open=0, close=value`, so `body ==
  value`. That is what lets `derive_line`, `median_band` and the chart's
  bar/volume panels work on every metric without a branch. `volume`
  becomes the distinct-contributor count, which is the first time that
  panel has meant anything real.
- `plan_tick` reads one series per enabled metric on a working tick (once
  a day) and caches per key. Idle ticks still short-circuit on two indexed
  lookups and read nothing.
- Charts: candlesticks are drawn **only** for a cumulative level. A count
  metric is one reading per day, so drawing an open/high/low/close would
  invent three numbers the data does not contain. Those get the bar panel.

## Cost

Measured against prod, one series each: 1–457 ms, ~1.3 s for all eleven.
Every count metric is a windowed (60-day), index-covered aggregate —
cheaper than the full ledger scan the economy metric already needed. Runs
once a day.

## Never delete a spec

A settled round's outcome is recomputed from its metric's series, so a
round naming a key the code no longer knows cannot be settled at all.
`plan_tick` marks those `unsettleable` and the round is **voided and
refunded** rather than guessed at, with its own wording on the card.
Retire a metric by unticking it on the dashboard, which stops it being
drawn while leaving history settleable.

## Dashboard

Economy → Pools grows a "What the Market Bets On" card: one checkbox per
metric, rendered from a catalogue the server sends (`pools_metric_catalog`
on `GET /api/config/casino`), so adding a metric in Python adds its
checkbox. All-ticked stores as `""` — the roster default — rather than
freezing today's list into config. Unknown keys are rejected on save and
dropped on read.
