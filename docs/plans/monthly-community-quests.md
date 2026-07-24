# Monthly quests → guild-wide community-measured cadence

## Context

Monthly quests today are a **per-user board cadence**: each member draws them
onto a personal board, accrues per-user progress (`econ_quest_progress`), and
self-claims. The product decision is that **all monthly quests should be
community-measured** — a single guild-wide counter everyone contributes to,
paid out in milestone tiers at month-end, exactly like the existing weekly
`community` quests but on a calendar-month cadence.

Design decisions (owner-confirmed):
- **Rotate a pool** — exactly **one** monthly community goal active per calendar
  month, advancing to the next pool member each month, **no gap month**.
- **Reuse community tiers** — 40/70/100% milestone tiers, each crossed tier pays
  the flat reward to every 30-day-active member, plus the top-3 contributor
  bonus (suppressed for `ANON_COMMUNITY_KINDS`). Settled at month-end.
- **Auto-tracked only** — a monthly quest MUST carry a `trigger_kind`; no manual
  manager-cranked monthly, no sign-off, no trigger words, no per-user target.
- **Preserve carefully** — a data migration reconciles in-flight current-month
  per-user progress into the seeded guild-wide counter; no clawback of already
  paid per-user claims.

Key enabler: the community progress/settlement tables
(`econ_community_progress`, `econ_community_contrib`,
`econ_community_tier_payouts`, `econ_community_progress_snapshots`) are keyed by
`quest_id`, **not** by qtype or period, so a monthly quest reuses them
verbatim. Keep `qtype='monthly'` as a distinct qtype (separate digest heading,
reward band) but reclassify it from a *board* cadence to a *guild-wide measured*
cadence. The weekly `community` path stays byte-for-byte unchanged except two
shared touch-points (the `_bump_community_kind` SELECT and the sizing default),
both regression-tested.

**Status:** all five stages implemented and tested (2026-07-23).

## Stages (each independently shippable + tested)

### Stage 1 — Cadence-aware target sizing (pure, inert)
- `quests.community_auto_target(trailing_total, *, periods_in_window=4.0)` —
  weekly keeps the 4.0 default (28d ≈ 4 weeks); monthly passes `1.0` (28d ≈ one
  month → target reflects a full month of activity / 0.75).
  `src/bot_modules/economy/quests.py:223`
- `auto_size_community_target(..., *, cadence="weekly")` — picks the divisor;
  weekly callers unchanged. `economy_quests_service.py:2322`
- Tests: `test_economy_quests_logic.py` (weekly default unchanged; monthly ≈4×),
  `test_economy_quests_service.py` (cadence="monthly" path).

### Stage 2 — Schema + rotation/selection plumbing (inert)
- Migration `src/migrations/125_econ_monthly_community.sql` **schema only**:
  `ALTER TABLE econ_day_marks ADD COLUMN last_community_month TEXT;`
  (qtype CHECK already allows `monthly` since migration 070.)
- `_next_community_run(conn, guild_id, qtype)` core +
  `next_community_weekly`/`next_community_monthly` wrappers
  (ORDER BY `last_run_week` ASC, id ASC — least-recently-run; `''` leads).
- `list_active_community_kind_quests(..., qtype="community")` gains a qtype param.

### Stage 3 — Guild-wide measurement + qtype-branch move + validation
Move monthly OUT of board/claim, INTO the community lane:
- `quests.py`: drop `"monthly"` from `BOARD_CADENCES` (35) and
  `PERSONAL_BOARD_SIZE` (58); `can_activate("monthly")` → `True` (uncapped,
  scheduler owns the single lane); keep `_REWARD_BANDS['monthly']` (flat tier
  reward).
- `economy_quests_service.py`: drop `"monthly"` from `_CLAIMABLE_TYPES` (49),
  `list_trigger_quests` (403), `board_sizes` (442); `spotlight_kind` excludes
  `('community','monthly')`; `_bump_community_kind` SELECT →
  `qtype IN ('community','monthly')`; `fire_trigger_quests` early-skips monthly
  in the per-member loop; `load_member_quest_board` renders monthly as the
  guild-wide bar (`state="community"`); `_check_trigger_config` monthly arm
  requires a kind, forbids sign-off/words; `_check_target_count` forbids a
  per-user target/band on monthly (removes the earlier weekly/monthly one-shot
  rule for the monthly half — the rule now only guards weekly).
- After Stage 3 a monthly kind quest accrues a guild-wide counter and renders a
  bar; not yet rotated/settled.

### Stage 4 — Month-roll settlement + rotation (moves money)
- `_roll_community_monthly(...)` in `economy_loop.py`: single lane, no gap —
  settle the active monthly via `settle_community_weekly` (reused as-is,
  period-agnostic) on a genuinely closed month, then activate
  `next_community_monthly` with `auto_size_community_target(cadence="monthly")`
  and `activate_community_weekly(week="YYYY-MM", slot=1)` (both reused).
- Month-roll block in `run_guild_day_roll`: read `last_community_month`, compare
  to `quests.month_for(today)`, roll on change, advance the marker in the final
  `UPDATE econ_day_marks`.
- `community_hourly_beats` iterates `qtype IN ('community','monthly')`; add
  `_seconds_to_next_month_start` for the monthly final-24h nudge.

### Stage 5 — Data migration reconciliation + surfaces
- Append reconciliation DML to migration 125: seed `econ_community_progress.current`
  from `SUM(econ_quest_progress.current WHERE period = this month)`; size
  `community_target` from trailing-28d `econ_kind_activity`/0.75; stamp
  `last_run_week=YYYY-MM`, `community_slot=1`; collapse to lowest-id active auto
  monthly per guild; deactivate kindless monthly (rows/claims preserved — **no
  clawback**, disjoint reservation tables mean no double-credit).
- Web `economy_manager.py`: `/progress` & `/settle` already 422 non-community
  (monthly correctly rejected); confirm messages; read-back attaches
  `community_*`.
- Panel `economy-quests.js`: `TYPE_HINTS.monthly` rewrite; `updateCommunity`
  forces game completion + auto-target note for monthly, hides per-user target;
  submit sends `trigger_kind`, `community_target=null`; drop `quest_board_monthly`
  from `BOARD_FIELDS`.
- `quest_digest.py`: monthly heading kept; blocks render the bar automatically
  once `state=="community"`.
- `economy_stats_service.py` / `leaderboard.py`: move monthly out of the
  per-cadence board pulse into the community hero (display-only).

## Critical files
- `src/bot_modules/services/economy_quests_service.py`
- `src/bot_modules/services/economy_loop.py`
- `src/bot_modules/economy/quests.py`
- `src/migrations/125_econ_monthly_community.sql` (new — re-verify number at ship)
- `src/web_server/routes/economy_manager.py`, `static/js/panels/economy-quests.js`
- `src/bot_modules/economy/quest_digest.py`
- `docs/economy_spec.md` §4, `src/web_server/static/manual.html`

## Verification
- Per-stage: `rtk proxy .venv/bin/python -m pytest <file> -o addopts="" -q` on the
  named suites (loop, service, logic, register, quest_views, digest).
- Weekly-community regression suite must stay green at every stage (proves the
  shared path is untouched): `test_week_roll_rotates_weekly_and_settles_community`,
  `test_community_weekly_gap_week_lifecycle`.
- Lint: `npx eslint` + `stylelint` on the panel; `ruff` + `pyright` on Python.
- Full gate before push (CI backstops).
