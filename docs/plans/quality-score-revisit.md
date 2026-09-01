# Quality Score → Contributors

**Status (2026-08-31): COMPLETE.** All five stages shipped. The member quality
score was **replaced**, not repaired: a nine-week backtest showed the composite
performs worse at its own stated job than a single line of SQL, and the question
it answered is not the question the server needs answered.

Two things were learned during the build that the investigation had not
anticipated, both recorded in full below: channel baselines must be
**leave-one-out**, and the sample-floor question is really a **reliability**
question, answered by measuring split-half reliability rather than by picking a
threshold.

Source research: `docs/member_quality_score_research.pdf` — the report the
original algorithm was speced from (April 2026). It was never committed at the
time; the code landed in `4afb5f41` ("quality scores") with no accompanying
doc, and `docs/reporting_spec.md` §Member quality score is a post-hoc
description of what the code already did.

---

## Why replace rather than fix

### 1. Three defects, all live since April

| | Cause | Effect |
|---|---|---|
| D1 | Rejoining Discord resets `joined_at`, collapsing the consistency denominator | one member scored **331%** on a component capped at 25% |
| D2 | A 90-day window spans **14** epoch-week buckets (`ts // 604800`) but the denominator is `90 // 7 = 12` | every fully-active member reads ~106% |
| D3 | No `min(1.0, …)` clamp anywhere; consistency is the only unbounded component | both of the above leak into the final score |

`WEEKS_IN_WINDOW = 13` at `member_quality_score.py:34` is **dead code, never
referenced** — it is the research report's own denominator ("10 of the last 13
weeks"), orphaned when the implementation substituted `_window_days // 7`.

Across 969 scored member-weeks in the backtest, the raw consistency term has a
**median of 1.12** and 66.6% of values exceed 1.0. The overflow is the normal
case, not an edge case.

### 2. The composite is beaten by one line of SQL

Nine weekly snapshots (2026-07-06 → 2026-08-31), the scorer run as-of each date
against truncated data. AUC against 30-day forward silence — the module
docstring's own stated purpose, "identifies genuinely disengaged server
members":

```
days since last activity     0.910     ← one line of SQL
recency term alone           0.910     (identical by construction; confirms the pipeline)
Consistency & Recency        0.885
full composite as built      0.845
composite with D3 clamped    0.836
the research report's model  0.824
```

Monotone degradation: every layer added on top of "days since last activity"
makes it worse. **Fixing D3 does not help** — the overflow was accidentally
acting as extra recency weight.

Per-component, same task:

```
Consistency & Recency (25%)   0.885
Posting Activity      (15%)   0.633
Engagement Given      (40%)   0.513   ← coin flip
Content Resonance     (20%)   0.484   ← worse than chance
```

The largest weight in the algorithm carries no signal for the thing the
algorithm exists to detect. In practice, picking the ten lowest-ranked members
each week caught 2–5 of the actually-silent; picking the ten longest-since-active
caught 5–7.

Nothing predicts actual departure — composite 0.534, engagement 0.508,
days-ago 0.517 against real `leave` events. Underpowered (5–10 departures per
cut), so read as "no evidence of signal", not "proven absent".

The score is **stable but not valid**: week-over-week rank correlation is
ρ = 0.92–0.97. It consistently measures something. That something is not
disengagement, and it is not one thing.

### 3. "Who's gone" is already answered elsewhere

The inactivity prune is plain days-inactive plus an exception list, unrelated to
scores (confirmed in `docs/reviews/2026-08-05-health-analytics.md`, and nothing
outside the panel reads `final_score`). A disengagement panel duplicates a tool
that already works better.

---

## What replaces it

**Contributors** — five ranked views, no composite, no overall rank. Route id
stays `quality-score` (frozen per CLAUDE.md; deep links and telemetry key off
it); nav label under Reports → Engagement changes to "Contributors".

Each view shows the **underlying counts beside the score**, per the house
standard for analytics features set by `attention_report.py` (flag-not-verdict,
evidence-over-score, the COMPAS anchoring failure mode called out by name).

| View | Measures | Method |
|---|---|---|
| **Popular Content** | who posts things people respond to | unique reactors + unique repliers per post, as a lift against that channel's own average |
| **Conversation Catalyst** | who restarts a quiet room | a message after ≥3h channel silence followed within 30min by ≥3 messages from ≥2 others, as a lift against the channel's base restart rate |
| **Connectors** | who spreads attention widely | distinct partners, reciprocity (given ÷ received), and top-partner concentration |
| **Welcomers** | who answers newcomers | share of their replies aimed at a member inside their first 14 days, as a lift against the server-wide share |
| **Lifts the Under-Attended** | who engages people few others do | replies weighted by the inverse of how much attention the target usually receives |

Welcomers, Connectors and Under-Attended are the three readings of "interacts
with the people that show up" — newcomers, the room broadly, and the ignored.
All three were wanted.

### Measured on live data, 90-day window, home guild

Selected rows, to fix what each view should look like:

```
Popular Content        Miss Dayss 3.13x (60 posts) · Skittly 2.93x (122) · Jenny 2.14x (380)
Conversation Catalyst  texasboop 2.94x (5/15) · RegalRuffian 2.51x (5/13) · Ivana Dee 1.94x (18/57)
Connectors             Billy 181 partners · Lily 159 · Luciaaaaa 143 (all <14% top-partner)
Welcomers              Mr 2sDae 3.09x · LoafBerry 1.61x (53 newcomers) · Billy 1.41x (95)
Under-Attended         Bearussy Boi 2.22x (190 replies) · Lily 2.22x (5,022) · Ramsay 1.88x
```

**Across 38 leaderboard slots there are four repeat appearances and not one
person appears in all three of the original families.** Popular posters are not
conversation-starters, and neither group is the connective tissue. That is the
substantive case against a composite: there is no set of weights that describes
these roles, because they are held by different people.

---

## Method notes (carry these — two metrics were artifacts before adjustment)

- **Channel-adjust everything.** On raw hit rate, EPically punny topped
  Conversation Catalyst at 72.9% of 70 attempts; channel adjustment drops them
  out of the top twelve entirely — they post into rooms that restart easily.
  Raw Popular Content survived adjustment (Miss Dayss, Skittly hold ~3x), but
  that had to be checked, not assumed.
- **Normalise by opportunity, not volume.** Raw counts of "newcomers answered"
  and "first replies sent" reproduce the Connectors list almost exactly — the
  most active people do the most of everything. Both only became informative as
  a share-of-their-own-replies lift.
- **Tested and rejected: "Icebreakers"** (how often a member's reply is the
  first answer someone got). **85.5% of all replied-to messages get exactly one
  distinct replier**, so the base rate is ~85% and every member clusters at
  1.1–1.25x. The literal "responds to whoever is in the room" reading does not
  yield a discriminating metric; it collapses into Connectors. Do not rebuild it.
- **The quiet-quartile cut is partly tautological** — a group defined by
  receiving little will always receive a small share. The continuous
  inverse-attention weighting in *Lifts the Under-Attended* is the honest
  version and needs no threshold.

## Data caveats

- `reaction_log` starts **2026-04-05**, so a 90-day window is fully covered but
  nothing earlier is. Longer windows silently lose reaction signal.
- `reaction_log` holds **63%** of the `message_reactions` aggregate for recent
  messages — reaction-derived figures are directionally right, not exact.
  Reply-derived figures are complete. Percentile/lift rank should survive if the
  gap is roughly uniform across members, which is **not yet verified**.
- `quality_score_leaves` is **empty** and is the deleted leave-of-absence
  table, not departed members' scores. `docs/reviews/2026-08-05-health-analytics.md`
  G2 mischaracterises it; there is no retention data being kept.

---

## Stages — all complete 2026-08-31

1. ✅ **`contributors_service.py`** — the five metrics over the existing ingest
   tables, one bulk fetch per window, mirroring the current single-pass shape.
   Ships with `tests/test_contributors_service.py` covering each metric's happy
   path, the channel-adjustment denominator, the empty-channel and
   zero-opportunity guards, and the bot/self-interaction exclusions.
2. ✅ **Route** — `GET /api/reports/quality-score` returns the five views,
   `require_perms({"moderator"})` unchanged, `cached_run_query` TTL unchanged.
   Schema in `schemas.py` replaces the four component floats.
3. ✅ **Panel** — `panels/quality-score.js` rebuilt as five sortable tables (or one
   table with a view switcher), counts shown beside every lift, `mountAsync` with
   the rejection reaching the loader. Names resolved via `_resolve_names`.
4. ✅ **Delete** `member_quality_score.py`, its cache warmer in
   `dungeonkeeper/__main__.py:691`, `MemberStandIn`, `tests/test_member_quality_score.py`,
   and the four weight constants. Delete `core/scoring.py` too — a second, older
   scorer carrying the same 40/25/20/15 weights, confirmed to have **zero
   importers** anywhere in `src/` or `tests/`.
5. ✅ **Docs** — `reporting_spec.md` §Member quality score rewritten;
   `docs/INDEX.md` plans row; `manual.html` (the Reports paragraph at ~line 2146
   names "Quality Score" and must name Contributors instead); this plan's status
   header. Commit `docs/member_quality_score_research.pdf` alongside.

**No new table**, so no `docs/data_register.md` row and no privacy-notice line —
every metric is derived at read time from `messages`, `message_attachments` and
`reaction_log`, all already registered.

## Decisions taken

- **Replace, don't repair** — the composite is deleted, not clamped.
- **Strictly mod-only.** Same `moderator` gate, no Discord surface, no member
  self-view. The research report is emphatic ("never create a visible
  leaderboard or public ranking"), and a number nobody can see cannot be
  optimised for. Occasional manual shout-outs are an intended use; irregular
  human recognition does not create the standing optimisation pressure a
  visible leaderboard does.
- **Route id `quality-score` is reused**, per the frozen-id rule.

## Settled during the build

- **Leave-one-out baselines.** A member who dominates a small channel was being
  measured largely against themselves, pulling their lift toward 1.0 and hiding
  the outperformance the view exists to surface. Caught by a test fixture that
  scored two members identically who should have differed 2.5×.
- **Thresholds were the wrong instrument** — the question is reliability, and
  reliability is measurable. Split-half reliability (a member's lift across two
  independent 45-day windows, confirmed on a separate 30/30 split) drove both
  corrections: shrinkage (k = 25 / 0 / 5 / 50) and floors on *expected* events
  (≥1 restart, ≥5 newcomer replies). Each value sits where the two splits agree;
  one tuned on a single split and contradicted by the other is fitted noise.
  Full numbers in `contributors_service.py`'s constants block.
- **Under-attended k = 50 is deliberately not the reliability argmax.** k = 1600
  scores 0.916 by flattening everyone under ~1000 acts to 1.0 and letting raw
  volume carry the correlation; it demotes the member who is the actual finding
  in that view (2.17× over 369 acts → 1.22×).

## Open
- **Window default.** Currently 90 days, matching the old panel and the
  reaction-data floor. 30 days would make the lists more current but thins the
  catalyst counts considerably.
- **Guild B** — the second live guild runs its own economy and norms; the
  channel-relative adjustments should port without tuning, but the lists have
  not been eyeballed there.
- **Billy is the hub** — 181 partners, the lowest top-partner concentration on
  the board, 51% of newcomers answered. The research report flags exactly this
  shape as a fragility risk ("when the admin or a single power user generates
  most of the conversation, the community is fragile"). Not a defect to fix in
  code; recorded because the panel will show it every time.
