# Wellness relaunch — build plan

Source: `docs/reviews/2026-08-28-wellness-readiness.md` (FIX-FIRST verdict;
all §8 decisions resolved 2026-08-28, recorded in that doc). Branch:
`wellness-top-down-review`. Commits reference their stage letter.

Migration numbering: this branch uses **189+**. It first claimed 188, but
flash_themes took 188 on main while this branch was in flight — exactly the
collision the migration-numbering memory warns about — so numbers here are
provisional: recheck against main at every commit and again at rebase time.

## Stage A — Partners cut (decision 1)

Delete the partners surface end to end: `wellness_partners.py`,
`wellness-partners.js`, the service section (~wellness_service.py:1611-1725),
the api routes (request/list/delete + DM view wiring), nav row + dynamic-item
registration, done-embed copy line, purge/export branches in
`privacy_service`. Migration **189**: `DROP TABLE wellness_partners` (empty —
approved by Billy 08-28), plus opportunistic column drops
`wellness_users.cooldown_until`, `wellness_config.crisis_resource_url`.
Same commit: `data_register.md` wellness row amended, manual.html partners
mentions removed, spec §Partners rewritten as retired, nav id
`wellness-partners` retired-not-reused. Tests: delete partner tests; add a
migration test against the prod-schema snapshot recipe.
Also in A (pure deletions): `_SettingsView` (~130 lines, wellness_cog.py) and
the stale "mirrors the slash command" comment.

## Stage B — Defect fixes (D4, D5, D6, D8, D9, D10, flat-PUT hazard)

- D5: `/settings` validates timezone via `ZoneInfo`; panel input becomes a
  datalist fed from wizard choices + stored value.
- D8: `opt_in_user` preserves `opted_in_at` unless the row was opted out
  (CASE expression); twin tests.
- D9: home panel appends the stored enforcement value as a selected option
  when it's not in the enum (label via `enfLabel`).
- D10: exempt-channel upsert preserves label when incoming is the
  placeholder; dropdown filters already-exempt channels.
- D6: pause chips render date/relative ("paused indefinitely" >30d) on member
  + admin panels; admin roster shows paused-until.
- D4: migration **190** adds `wellness_weekly_reports.delivered_at`; set
  after `send_branded_dm` succeeds; admin panel shows undelivered count.
- Flat `PUT /caps/{id}` on a bucketed cap: reject with "edit the sliders".
- `slow_mode_rate_seconds`: hard-code 120s, delete the API parameter (column
  stays; drop rides a future migration).
Each with logic-layer tests (failing-first where it's a bug fix).

## Stage C — Funnel (decisions 2, 3, 6; D2, D11)

- Nav ungate: Wellness **Overview** visible to all members; not-opted-in
  branch grows into the web pitch page. Join-state branches added to Caps /
  Blackouts / History GET payloads + panels (dashboard_ia.md audience row
  updated same commit).
- Wizard: new explicit public-commitment question (default stays off);
  done-embed gains the goal nudge + sets week-one expectations; ~24h one-time
  follow-up DM nudging a first cap/blackout (rides the scheduler tick;
  respects notifications_pref; one-shot flag needed — column on
  wellness_users in migration 190).
- D2: wellness home panel + done embed disclose the economy-🔔 dependency of
  the daily digest line (deeper decoupling deferred).
- D11 + unprovisioned copy: point at "ask the server owner" until D1 ships
  (then at the Activate card).
- Login `return_to` fix (dashboard-wide): **separate commit**, not
  wellness-scoped.

## Stage D — Provisioning Activate card (decision 5)

`wellness-admin.js`: when `role_id`/`channel_id` are 0, show an Activate
Wellness card — role picker with auto-create (role-autocreate stage-1
pattern; wellness role is a safe kind) + channel picker; saved via existing
`upsert_wellness_config`. After activation, show current role/channel with
change buttons. Admin routes gain the two setters (`require_manage_server`).
Guild B's all-zero row becomes reachable instead of dead.

## Stage E — Goal payouts (decision 9; review §6b)

Migration **191**: `wellness_config` payout dials (`payout_clean_week`,
`payout_milestone_json` or per-tier columns, default 0/off);
`wellness_weekly_reports.payout_amount`; `wellness_streaks.paid_badge`.
- Clean-week payout inside the weekly-report pass: qualifying = ≥1 enabled
  cap/blackout all 7 tracked days + compliance 100% + activity floor
  (nonzero counted messages); idempotent via the report row PK.
- Milestone payout on badge crossing via `paid_badge`, decoupled from
  public-commitment celebrations; announced only in the member's own DM.
- Economy award path with a discreet ledger reason; per-guild dials on the
  admin panel; second guild defaults 0.
- Test matrix: no-goal week, goal added mid-week, paused member, activity
  floor, dial=0, re-run idempotency, milestone re-earn after streak break.

## Stage F — Caps simplification

Delete the Manual Caps section (~90 panel lines) + phantom
channel/category/voice scope machinery (~50 lines across api/enforcement/
CAP_SCOPES). Histogram sliders become the sole cap surface.

## Stage G — Docs & pitch (decisions 4, 6)

manual.html §15 restructured against the post-trim surface: value-first
intro, Joining, "Your wellness dashboard" (member panels + Leave, no more
"Admin Overrides" framing), honest For-admins block (defaults, pause/resume,
exempt channels, Activate). Fix the three admin-on-behalf claims. Privacy
notice: wellness clause expanded (streak ledger, cap tallies, weekly
summaries + local model). /help lines carry value + away's enrollment gate;
quickref row added. Spec: partners retired, day-counts non-goal amended
(blessed), provisioning gap closed, payouts documented; INDEX.md caveat
updated. "wellness check-ins" → enrollment state; "channels" plural checked
against Stage 0.

## Stage 0 — Billy's own items (outside the code)

Ask the two members whether the week-34 report DM arrived; check 884…466's
economy-game role in live Discord; confirm whether the wellness role reveals
one channel or a category; after the restart that ships this branch: resume
his own pause (the live enforcement drill) and set the payout dials.

## Sequencing notes

G is written once, after A/E/F settle the surface. C's ungate and the
join-state fixes ship together (critic's interaction warning). One QA card
per branch at `/dk-ship` gathers all Testing: sections.
