# Dashboard panel review — 2026-08-26

Six notes from a review of the dashboard's report/config panels, handed over
with prod-verified findings. Three design calls were already made by Billy and
are marked **DECIDED** below; nothing here re-litigates them.

Branch: `web-panel-review-fixes`. **One commit per stage.** Stages 2–5 are UI
changes, so each updates `src/web_server/static/manual.html` and carries a
`Testing:` section. Stages 1 and 6 land a test that fails *before* the fix.

Status legend: ☐ not started · ◐ in progress · ☑ shipped.

---

## Stage 1 — ☑ Participation Gini reports "no messages" when there are messages

**Verified bug.** `static/js/panels/health-gini.js:18` guards on
`if (!(d.tiers || []).length)`, but `compute_gini`
(`services/health_metrics.py:589`) returns `tiers` as a **dict**
(`{lurker, light, moderate, active, power}`). `.length` is `undefined`, so the
empty state renders unconditionally — the panel has never shown a number. The
route (`routes/health.py:808`) has no `response_model`, so the raw dict reaches
the browser untouched.

Prod right now: `gini 0.731`, badge `warning`, `top5_share 36.4`, `palma 40.83`,
`weighted 0.805`, `xp_gini 0.699`,
`tiers {lurker:0, light:53, moderate:21, active:34, power:66}`.

The tiers dict has five fixed keys and is never empty, so `Object.keys().length`
would be equally wrong. The honest "no messages" signal is that nobody posted —
no lorenz points / all tier counts zero.

**Second defect, same metric:** the `lurker` tier is **unreachable**. The tier
loop iterates `SELECT author_id, COUNT(*) … GROUP BY author_id`, so every row
has `cnt >= 1` and `wk == 0` can never be true; the "Lurker (0)" doughnut slice
is always zero. A lurker is a member with *no* messages, which means reading
guild membership from `known_users` (`guild_id, user_id, is_bot,
current_member`) and subtracting the posters. `current_member` must be honoured
— counting everyone ever seen would inflate lurkers with people who have left.

Prod sanity: main guild `known_users` holds 438 rows, 25 bots, 236
`current_member=1`; 218 non-bot current members against 187 distinct posters in
the last 30 days.

**Test:** the bug is a frontend guard against a backend shape, so pin the
contract where it can actually break — a route/shape assertion that `tiers` is a
mapping with the five known keys, plus a panel-level check of the empty-state
guard. Write it first, watch it fail.

## Stage 2 — ☐ Community goals belong under Quests, not Operations

`refreshCommunity()` in `panels/economy-bank-manager.js:199` (Economy ›
Operations › Bank) is a quests feature living on the bank page: community goals
are rows in the quests table with `qtype = 'community'`, the card fetches
`/api/economy/quests` and filters on that, and it calls
`/api/economy/quests/{id}/progress` and `/settle`.

Move it to `panels/economy-quests.js` (Economy › Earning › Quests). No route id
changes, so the frozen-id rule holds. Drop "community goals" from the Bank
entry's nav `keywords` in `app.js:203`; add it to the Quests entry. While
moving, check whether the Bank panel still needs its `[data-sec='community']`
wrapper and the `members` argument, and whether the settle handler still needs
`refreshLedger` once it lives on another page.

## Stage 3 — ☐ Drop the Health Score page

**DECIDED: remove it.** The composite score's whole surface:

- nav entry (`app.js:56`)
- `panels/health-composite-score.js`
- Home tile `tiles/composite-score.js`, its `widget-registry.js` entry, and its
  slot in `DEFAULT_ADMIN` (`"health-composite"`)
- `/health/composite-score` route (`routes/health.py:1075`)
- the `composite` slot in the `/health/tiles` aggregate
  (`routes/health.py:542-620`), including the `composite_deps` machinery that
  precomputes the other tiles purely to feed it
- `compute_composite_health` (`health_metrics.py:1428`)
- `docs/reporting_spec.md`, `manual.html`, and tests in
  `tests/test_health_metrics.py`, `tests/web/test_health_routes.py`,
  `tests/web/test_health_degraded_cache.py`

Precedent for this exact shape of removal, ripple included: `5b4cd71d`
("Reports: remove 11 experimental panels + dead endpoints"). Stale saved Home
layouts filter through `WIDGET_MAP` and degrade silently, so dropping a widget
id is safe.

Grep for `composite` before finishing — `core/scoring.py`,
`emoji_stealer/dedupe.py`, `quote_renderer.py`, `guess_crop_renderer.py` all use
the word for unrelated things. **Do not touch those.**

## Stage 4 — ☐ Suggested setup rows become clearable

`tiles/setup-suggestions.js` renders `/help/suggestions`
(`routes/advisor.py:172`) → `advisor_gaps.suggestions()` →
`[g for g in scan_guild(conn, guild_id) if g.is_gap][:limit]`. Recomputed live;
there is **no dismissal concept anywhere**.

**DECIDED: dismissal is guild-level and permanent.** Dismissing means "this
server has decided not to use this feature" — a property of the server, not of
the admin who clicked. Guild-keyed only, no per-user column, so it needs **no
`docs/data_register.md` row**. (If the landed schema stores the dismisser's user
id for audit, that reasoning breaks: it then needs a register row *and*
`SUBJECT_ID_COLUMNS` handling.)

Needs: a migration (next free is **183**; `main` agrees, but re-check for
parallel-session collisions before committing), a store keyed
`(guild_id, feature_key)`, filtering in `suggestions()`, a dismiss endpoint, an
un-dismiss path so a cleared row can come back, and the control on the tile. The
same suggestions surface in the `config-advisor` panel — keep both consistent.
Dismissal is an admin action; gate it accordingly.

Complex: investigate, bring the approach and open questions back before building.

## Stage 5 — ☐ Bring back the social network visualizer

It was **Connection Graph** — a force-directed canvas, 1,186 lines — deleted
2026-07-27 in `5b4cd71d` with the rationale "same endpoint as Interactions".

    git show 5b4cd71d^:src/web_server/static/js/panels/connection-graph.js

**The backend was never removed.** `/api/reports/interaction-graph`
(`routes/reports.py:510`) still accepts `include_metrics=1`, `resolution`,
`days`, `limit`, and still returns nodes with `cluster_id`, edges, `top_pairs`,
and a metrics block (clustering coefficient, network density, reciprocity,
isolates, bridge users, avg path length, small-world quotient, cross-cluster
matrix — `schemas.py:247`). The deleted panel called exactly this one endpoint.
**Frontend-only restore.**

**DECIDED: its own page, Reports › Social Graph**, alongside Interactions and
One-Sided Attention. Old route id was `connection-graph` — reuse it so old deep
links resolve.

Refresh, not revert. The file predates conventions that have since landed: it
hardcodes dark hex colors (`#2b2d31`, `#dbdee1`) that break the light theme, and
it should use the shared chart palette, `states.js`, `table.js` (which escapes
cells now) and theme CSS tokens. It carries a disabled "Edge Type" control whose
tooltip says it needs a schema migration — check whether
`user_interactions_log` distinguishes replies from mentions now; if not, drop
the dead control rather than shipping a disabled one. Give it a browser layout
scenario (`scripts/mobile_layout_scan.py`) — a 1,186-line canvas panel with a
control strip is what the mobile gate exists for.

Complex: investigate, bring the approach and open questions back before building.

## Stage 6 — ☐ Rebuild the one-sided attention gate

**DECIDED: rebuild — not retune, not retire.**

`services/attention_report.py` is **structurally incapable of firing**. Measured
on 30 days of live prod (bots excluded via `known_users.is_bot`; 6,410 directed
pairs, 41,414 text + 41,240 reaction + 910 voice-follow events):

| combined-weight floor | asym≥0.75 | ≥0.80 | ≥0.85 | ≥0.90 | ≥0.95 |
|-----------------------|-----------|-------|-------|-------|-------|
| 5                     | 138       | 75    | 34    | 26    | 6     |
| 10                    | 52        | 17    | 6     | 5     | 1     |
| **15 (shipped)**      | 20        | 5     | **1** | **0** | 0     |
| 30                    | 5         | 1     | 0     | 0     | 0     |

Among pairs clearing the shipped `VOLUME_FLOOR = 15`, the **99th percentile** of
asymmetry is **0.75** — `ASYM_CUT = 0.85` sits above the entire empirical
distribution. Exactly one pair server-wide passes both gates, and it is a clear
false positive: 26 reactions + 6 replies, the target *did* reciprocate,
concentration 2% spread across 87 distinct targets, and its own cautions read
"contact eased off" and "mostly reactions — can read as ordinary support".

Three distinct defects:

1. **The two gates are anti-correlated.** Sustained one-sided pursuit is
   *low-volume and unanswered*; a floor of 15 combined weighted events excludes
   precisely that region. Only **one** pair server-wide has zero reciprocation
   and out-weight ≥ 8. Requiring both extremes selects almost nothing.
2. **Asymmetry has no base-rate normalization.** `asym = w_out / (w_out +
   w_back)` is measured pair-locally, so a member who posts a lot and rarely
   reacts back reads as "non-reciprocating" toward everyone who reacts to them.
   That is a fact about the *target's* habits, not the initiator's behaviour.
   Consider comparing observed reciprocation against that target's *typical*
   reciprocation rate across all their partners.
3. **`_escalation` compares unequal windows — a correctness bug independent of
   tuning.** The pivot is the target's last reciprocal action and the "after"
   window is a fixed `ESCALATION_HALF_DAYS = 14` regardless of how much of it
   has elapsed. A target who last replied 3 days ago gets 3 days of "after"
   against 14 days of "before", so the ratio is structurally depressed and the
   "contact eased off — trend is cooling" caution fires as an artifact of
   recency. Either truncate both windows to the same available span, or return
   `None` when the after-window hasn't fully elapsed.

Also weigh: reactions are half-weight but dominate volume (26 reactions alone
nearly clears the floor), and reacting is not conversation — an unanswered
reaction is much weaker evidence than an unanswered reply. And `concentration` /
`distinct_targets` currently only produce *cautions*; a 2%-concentration pair
spread across 87 targets is strong evidence *against* fixation and arguably
shouldn't surface at all.

Hold the module's stated design constraints while changing it — they're in its
docstring and they're good: a flag is not a verdict, expose the evidence rather
than a black-box score, never infer gender, never automate action. The goal is a
handful of genuinely lopsided pairs a mod can judge — not zero, not a wall.

Why rebuild rather than delete: an always-empty safety report reads as "nothing
concerning here", which is a false assurance on a surface where that matters.

**Re-measure against prod before and after.** Read-only:

    sqlite3.connect("file:/home/ben/discord-bots/dungeon-keeper/dungeonkeeper.db?mode=ro", uri=True)

Main guild `1469491362444480666`. A second live guild (`1476…484`, "nut") is run
by someone else — sanity-check any new threshold against both rather than tuning
to one server's shape. Show Billy the before/after numbers for any threshold
before committing to it.

Complex + a safety surface: investigate, bring numbers and open questions back
before building.
