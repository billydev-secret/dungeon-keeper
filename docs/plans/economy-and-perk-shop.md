# Implementation plan — Economy & Perk Shop

Spec: `docs/economy_spec.md` (V3.1, repo-grounded). Commits reference stages as
`Economy (stage N): …`. Each stage: built in a worktree, `scripts/gate.py` green,
spec + `docs/INDEX.md` updated in the same commit, merged to main for live testing
before the next stage starts. Restart required after each merge (user pushes that
button). QA cards post automatically from the commit's Testing: section
(TESTING_QUEUE.md retired 2026-07-18).

## Layout

```
src/migrations/06X_economy.sql            # one migration per stage that adds tables
src/bot_modules/economy/                  # pure logic: streaks, conversion, billing planner
    __init__.py  logic.py  quests.py  rooms.py (stage 6)
src/bot_modules/services/economy_service.py   # sqlite CRUD + EconSettings loader + apply_credit/debit
src/bot_modules/services/economy_loop.py      # hourly loop (day/week rolls, billing, expiries)
src/bot_modules/cogs/economy_cog.py           # /bank group, QOTD, listeners' glue
src/bot_modules/cogs/economy_rooms_cog.py     # stage 6: /room group
src/web_server/routes/economy.py              # config + bank-manager + metrics APIs
src/web_server/static/js/panels/economy-*.js  # config, bank manager (member wallet page + role studio are v2)
src/web_server/static/js/tiles/economy-metrics.js
tests/test_economy_*.py  tests/web/test_economy_routes.py
```

Pure math (streak windows, grace, conversion, billing schedules, ISO-week rolls,
overwrite plans) lives in `economy/logic.py` with no Discord/DB imports — the
voice-master `logic.py` testability pattern. Wallet mutations only ever happen inside
`apply_credit`/`apply_debit` (single transaction: balance + ledger row; booster ×1.5
ceil on faucet credits; balance can never go negative — debit fails atomically).

## Stage 0 — Foundation: wallets, ledger, settings, config panel

- Migration: `econ_wallets`, `econ_ledger` (append-only, signed amounts, `kind`,
  `actor_id`, `meta` JSON), `econ_notify_prefs`.
- `EconSettings` frozen dataclass + loader over shared `config` KV (`econ_` prefix,
  `allow_legacy_fallback=False`): enabled, bank/spotlight channels, manager role,
  branding fields, transfers toggle, conversion rate, login bases (text 5 / voice 15),
  streak bonus cap 10, milestone table, reward amounts (QOTD 10, participation 5,
  win 20), all perk prices, booster multiplier 1.5.
- `apply_credit`/`apply_debit` + booster check; `/bank wallet` (balance + last 10
  ledger rows, branded strings, accent color); `/bank grant` (manager/admin).
- Dashboard: Economy section in `SECTIONS` (admin item), `routes/economy.py` with
  `GET/PUT /api/economy/config` (Pydantic body, `require_perms({"admin"})`,
  `get_active_guild_id`, `run_query`), `panels/economy-config.js` — branding + every
  scaling parameter, grouped, with inline defaults shown.
- Register router in `server.py`; append cog to `extension_names`.
- Tests: credit/debit atomicity + booster rounding, settings loader defaults,
  route auth/validation, cog smoke.

## Stage 1 — Faucets: logins, streaks, conversion, reactions, QOTD, game hooks

- Migration: `econ_logins` (PK guild/user/local_day, source, paid), `econ_streaks`,
  `econ_conversions` (PK guild/user/local_day, xp, coins, remainder REAL),
  `econ_qotd` + `econ_qotd_rewards`, `xp_reaction_awards` (dedup for the new source).
- Streak/grace math in `logic.py`: rolling-7 grace, milestone schedule — pure,
  table-driven tests.
- Login hooks: `events_cog.on_message` (after `_counts_as_member_activity`) and the
  voice-XP tick callback once `qualified_since` ≥ 5 min. DM on milestone/grace/reset
  via shared `try_dm` helper (new `economy_service.try_dm`, honors mute prefs).
- **New XP source** `reaction_given` in `xp_system` (constant, coeff
  `xp_coeff_reaction_given_xp` default 0.34) awarded in `on_raw_reaction_add` to the
  reactor (no self/bot; INSERT-OR-IGNORE dedup). Note in `docs/xp_spec.md`.
- Hourly `economy_loop`: per-guild local-day roll detection (`tz_offset_hours`),
  conversion (sum `xp_events` for the local day + carried remainder → coins),
  streak lapse evaluation, QOTD window close. Registered as a startup task factory.
  Set the main guild's tz row as part of rollout.
- `/qotd post <question>` (manager/admin): renders via `render_quote_card`, posts,
  records; reward-on-message listener pays 10 once per member per QOTD.
- Game hooks: participation payout in `game_manager.end_game` (session players);
  winner +20 for **both architectures** (decided): duel cogs with `winner_id`
  (chicken, hot_potato, hp_group, musical_chairs, pressure_cooker, quickdraw) AND a
  per-game-type winner resolver over the `end_game` payload for party games, modeled
  on the best-moment extraction in `games_session/logic.py:85-118`; game types with
  no meaningful winner pay participation only. One shared payout helper so cogs stay
  thin.
- `/bank mute` notification toggle (prefs table ships in Stage 0).
- Tests: conversion idempotency (double-roll replay), remainder carry, login race
  (message+voice same day), streak/grace matrix, reaction dedup, QOTD one-per-member,
  end_game payout, winner payout per duel cog + per party-game resolver.
- **Shipped notes:** (1) conversion is single-day — the loop converts only the most
  recent marked local day and jumps forward, so a multi-day outage never mints a
  backlog when a guild re-enables the economy (§12). (2) participation/win payouts v1
  reach only games with a tracked roster (six duel games; ttl/traditional/legitlibs);
  most party cogs record just the host, so their rosters need enriching before they pay
  participation. **Done in Stage 2** — 11 party cogs enriched (see Stage 2 shipped note 5).

## Stage 2 — Quests

- Migration: `econ_quests`, `econ_quest_claims`, `econ_community_progress`,
  `econ_community_payouts` (reserve-row-before-credit).
- Bank Manager dashboard section (new `SECTIONS` entry gated like `games_editor_role`
  → `economy_manager_role`): quest authoring CRUD, active-slot enforcement
  (1 daily + 5 weeklies), rotation tags, out-of-band amber warning, pending sign-off
  queue, grant form, audit stream view (ledger filter). *(2026-07-13: section renamed
  **Economy** and reorganized — Operations / Quests / Income Sources / Statistics /
  Settings; see economy_spec.md "Manager surface".)*
- `/bank quests` + claim flow; sign-off cards in bank channel as persistent views
  (Approve/Deny; deny reason modal → DM), re-registered on restart; 7-day pending
  expiry in the hourly loop; deny history shown on the card.
- Daily rotation + weekly activation + community settlement on day/week rolls;
  community progress bar embed; payout to `member_activity` 30-day actives.
- Tests: claim state machine (pending/approved/denied/expired/re-claim), one-pending
  rule, settlement exactly-once, rotation, route perms (manager vs admin vs member).
- **Shipped notes:** (1) claims are period-keyed — daily = local day (`YYYY-MM-DD`),
  weekly = ISO week (`YYYY-Www`), community = `once` — with partial-unique
  `(quest, user, period)` indexes as the race anchors (≤1 pending, ≤1 paid per period),
  so re-claimability needs no reset sweep. (2) Sign-off cards are persistent
  `DynamicItem` Approve/Deny buttons (`econ_claim:approve|deny:<id>`) re-registered in
  `cog_load` — restart-safe with no per-message view store; the same claim resolves from
  the card or the Bank Manager panel (dashboard resolution best-effort edits the card +
  DMs over the shared loop). (3) Pending claims expire >7 days with a DM and become
  re-claimable. (4) Community settlement splits on sign-off: plain quests auto-settle on
  the weekly ISO-week roll (`list_settleable_community_quests` excludes sign-off), sign-off
  quests settle only via the dashboard manual Settle. (5) Roster enrichment shipped — 11
  party cogs (ama, clapback, compliment, hottakes, mfk, mlt, nhie, price, rushmore, story,
  wyr) now pass real player rosters into `end_game`, taking participation payouts to 20 of
  23 games; photo, ffa, and fantasies remain excluded by design (anonymous submissions or
  no per-player completion hook).

## Stage 3 — Transfers + sinks part 1 (rental engine, role perks, gifts)

- Migration: `econ_rentals` (state machine: active/grace/lapsed/cancelled,
  `next_bill_at`, `grace_since`, price snapshot, meta), `econ_personal_roles`.
- `/bank pay` with >100 confirmation + transfers toggle enforcement (default on).
- Rental billing in hourly loop: claim-before-side-effect row advance → debit → on
  fail grace (36h, hourly retries, one DM) → revoke. Cancel = runs to period end.
  Leave/ban listener: immediate cancel + cleanup.
- Personal role engine reusing `booster_roles` machinery: create/edit with color,
  `secondary_color` (gradient), icon upload; anchor-role positioning **above** the
  booster swatch band (economy role takes display precedence — decided); name filtered
  via the voice-master blocklist (shared table, its rules respected — decided); ΔE
  check vs staff role colors; role deleted when last role-perk lapses; 200-role
  alert; feature gating (`ROLE_ICONS`, Enhanced Role Styles) with suspend-not-bill
  behavior.
- `/bank shop` (browse + rent, branded prices); `/bank role` subcommands + modals for
  name/color/gradient/icon (Discord is the whole v1 member surface — decided);
  gift-a-color.
- Tests: billing state machine incl. restart replay, grace/revoke timing, gradient
  supersedes solid, role lifecycle + precedence, gift flow, transfer limits.
- **Shipped notes:** (1) **Renewals bill the current guild price at each anniversary** —
  the rent-time price is snapshotted only for week one; a config price change takes
  effect next cycle, never retroactively. (2) Anniversaries are **no-drift** (advance
  `next_bill_at` by exactly one week off schedule) and a multi-week catch-up after
  downtime charges **once**; **suspension** (feature loss) freezes both the billing clock
  and the visual, then auto-resumes clean. (3) **Gift creates the recipient's role
  eagerly** at rent time (the beneficiary, not the payer, holds the personal role). (4)
  Personal-role hierarchy position is set **on create only** — above the "#### Cosmetics"
  booster band; a reconcile never re-hoists a manually moved role. (5) Uploaded role
  icons are stored under the db-parent dir at `econ_role_icons/` (sibling of the SQLite
  file). (6) Guards: **ΔE ≥ 25** vs staff colors (refusal names the clashing role) and
  the **Voice Master name blocklist** (shared table). (7) Dashboard **grace-cancel
  de-projects the role best-effort** post-commit (the loop only walks live rentals) —
  `role_updated` reports whether it ran; an active cancel just sets
  `cancel_at_period_end`.

## Stage 4 — Metrics & tuning surface (admin) — DONE

- `tiles/economy-metrics.js` + widget-registry entry + `econ_metrics_weekly` rollup
  (week roll in loop): median/p90 income, minted vs burned, faucet mix, rental
  uptake/churn, streak health; pricing hints computed from ledger and surfaced next
  to price fields in the config panel.
- Tests: rollup math, hints math, tile route perms.
- **Shipped notes:** (1) The rollup (`economy_metrics_service.compute_weekly_rollup`)
  rides the week-roll branch of the economy loop, in the same transaction as the
  weekly rotation and community settlement, and is **idempotent via the
  `(guild_id, iso_week)` primary key** (`INSERT OR IGNORE`; returns `None` on replay),
  so a crash before the trailing mark update recomputes nothing. (2) **Transfers are
  excluded both directions** — income / minted drop `transfer_in`, burned drops
  `transfer_out` — so the figures measure real mint/burn, not currency movement. (3)
  Churn is counted off the new **`econ_rentals.ended_at`**, stamped on every
  termination path (billing revoke, period-end / immediate cancel, member-leave
  cleanup); `NULL` for live rentals. (4) The home tile rides a **new `"economy"` home
  source** mirroring the existing `health` source (own fetch of `/api/economy/metrics`,
  wired through `home.js` / `widget-grid.js` / `widget-registry.js`), admin-perms,
  Health category. (5) The tile's week-over-week arrow is **net-mint** (minted −
  burned direction, ≥ 2 weeks) — not the design-era ">20% MoM" flag. (6) **Pricing
  hints are advisory only** (`round(median × fixed factor)`, no enforcement); both the
  tile and the hints show an **empty state** (`{}` / "rollup pending") until the first
  guild-local week closes.

## Stage 5 — Soak + tuning pass — ACTIVE (Billy-driven)

No new features. Run the economy live ≥1–2 weeks; use the metrics card to set real
prices/rates for each guild; fix what live testing surfaces (QA cards post
automatically from each commit's Testing: section — TESTING_QUEUE.md retired
2026-07-18). Decision checkpoint with real income data before rooms.

- Shipped the **Statistics page** (Bank Manager) as Stage-5 tuning tooling — live,
  on-demand who-has-what + income-velocity read (supply concentration, distribution,
  7d flow, per-member table, engagement, affordability, top transfer pairs) beside
  the weekly rollup card. See spec §9.

## Stage 6 — Private rooms (spec §8)

- Migration: `econ_rooms`, `econ_room_members`, `econ_room_blocks` (persist across
  re-rentals).
- Generalize: Voice Master overwrite planner + capped list helpers; Pen Pals category
  channel creation; hidden-channels snapshot/restore for the 14-day text archive.
- `/room` group (invite/kick/block/rename/topic/nsfw/slowmode/limit/lock) + dashboard
  room card; owner Manage Messages/Threads inside; mod view retained + disclosed;
  NSFW inherits verification gates; rental lapse → archive/delete; leave/ban cleanup;
  startup reconciliation.
- Tests: overwrite plans, block persistence, archive/restore round-trip, lapse flows.

## Cross-cutting rails

- **Exactly-once money:** every scheduled payout/charge keyed by a dedup row written
  in the same transaction as the balance change; loop replay is a no-op.
- **Integer wallets**, ceil on credits, floor on conversion; remainder carry.
- **JS syntax check** via the `gjs` `Reflect.parse` one-liner (no Node); JS visible
  after service restart only.
- **INDEX.md:** add `economy_spec.md` as Design spec at Stage 0; flip to Reference
  when built. Update `docs/xp_spec.md` (new reaction source) at Stage 1 and
  `voice_master_spec.md` if list helpers move during Stage 6.
- **Coverage floor** must not drop — pure-logic modules keep it cheap.
- Open findings check: `docs/reviews/` touches on `events_cog`/games modules — surface
  any open findings when those files are edited (per working agreement).

## V2 (committed after v1 ships)

Member wallet dashboard page (`require_perms(set())`) · role studio panel with live
preview · spotlight slots (purchase, featured embed, 3/ISO-week inventory, expiry).

(Party-cog roster enrichment for participation payouts shipped early, in Stage 2 — 11
cogs now pass real player rosters into `end_game`; photo/ffa/fantasies excluded by
design.)

## Deliberately deferred

Scheduled/auto QOTD · contributors-only community payouts · jail-fine sinks ·
big-ticket sinks (V3 §13).
