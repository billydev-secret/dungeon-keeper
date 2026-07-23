# Website UX cleanup — execution plan

Implements every finding in
[reviews/2026-07-22-website-ux-review.md](../reviews/2026-07-22-website-ux-review.md)
plus a full user-facing copy pass. Branch `website-cleanup` (off `website-ux`, which
carries the review doc). Decisions taken with the user: **manual gated behind login**;
brand standardized to **"Dungeon Keeper"**; US spelling & Title Case per the July
style rulings.

## Wave 1 — five parallel agents, strict file ownership (no overlaps)

| Agent | Owns | Findings |
|---|---|---|
| core-shell | app.js, index.html, app.css, help-panel.css (overflow only), widget-grid.js, panels/home.js | all W-N*, W-A1/2/3/6/8/9/10/11, W-M1 (CSS side), W-H2 (mechanism), W-H3 (Home banner) |
| widgets | config-helpers.js, ui.js, tab-strip.js, states.js, table.js, charts.js, filter-select.js, api.js, slider.js | W-C1 core, W-C2, W-C10 (dialog title), W-A4, W-A12, W-D6, W-D14 |
| panels-data | panels/: connection-graph, interaction-heatmap, health-*, activity, mod-tickets, mod-jails, mod-warnings, rules-watch, qa-tracker, todo, games-logs, live-log, message-search, economy-stats, xp-leaderboard | W-D2/3/4/5/7/8(partial)/9/10/11/12/13/15, W-A5, W-A7 + copy pass in owned files |
| panels-config | panels/config-*, economy-config/sinks/income-sources/quests/claims, games-config, announcements; routes/config.py | W-C3–C10, W-A7, W-D11 (quests) + copy pass in owned files |
| copy-help | manual.html, login.html, help.js, help-sections.js, docs.js, server.py; tests/web (gating) | W-H1/4/5/6/7, checklist links half of W-H3 + full manual/login copy pass |

Cross-agent contract (dirty tracking, W-C1): config-helpers exports `guardForm(form)`
(input/change ⇒ dirty; successful `showStatus(..., ok)` ⇒ clean) and publishes
`window.__dkDirty()` / `window.__dkDirtyReset()`; app.js consults them before
hash-navigation and guild switch, plus `beforeunload`. Panels opt in via `guardForm`.

Help-link contract (W-H2): nav items in app.js `SECTIONS` gain an optional
`help: "help-<page>"` field; core-shell renders the header "?" affordance from it.

## Status log

- **Wave 1 first run:** widgets agent completed; the other four were killed
  mid-flight by an API credit exhaustion. Salvaged state: app.js fully
  rewritten (verified), 3 config panels done (welcome, voice-master,
  moderation) + routes/config.py + tests, manual gating in server.py + authz
  test, report-helpers.js. All files parsed clean — no truncated writes.
- **Main session completed** (core-shell's other half): index.html hooks
  (skip link, filter clear, `#panel-root[tabindex]`), the full app.css block
  (W-A2/A3/A6/A8/A9/A10, W-M1 ×6, and styling for every new class app.js
  emits), widget-grid.js W-N9, home.js W-H3 banner.
- **Browser-verified** via scratchpad/boot_smoke.py: zero page errors; 179 nav
  items with real glyphs + tooltips; per-panel `document.title`; focus moves to
  panel root; unknown route renders the notice; keyword filter resolves
  "thread" → Auto-Thread; as a moderator, 35 admin pages render locked
  (disabled + aria-disabled + "— Admin only") and Guess Who / Whisper are
  reachable despite the Games gate (W-N2), with no duplicate nav ids.
- **Wave 1 second run:** four agents relaunched (analytics panels, config
  panels ×2 split, help/manual). Help/manual agent completed: 40/40 anchors
  resolve, label↔heading mismatches 15 → 0.

### Open questions raised by the work (need a decision, not in the review)

- **Bump Tracker**: manual.html §22 documents a "Bump Tracker" dashboard panel
  that does not exist (no nav entry, no panel file), though
  `routes/config.py:2785-2872` exposes `PUT/DELETE /config/bump-tracker*`.
  Either build the panel or rewrite the manual paragraph — currently the manual
  promises an unreachable surface.
- ~~"Billy-bot" and "Golden Meadow" are product names baked into code that also
  read as guild-specific~~ — **resolved**: both are now per-guild branding
  (`branding_config.assistant_name` / `casino_name`, migration
  `115_branding_product_names.sql`), edited on Config → Branding and defaulting
  to today's values. Remaining static copy: the sidebar nav label for the
  assistant's config panel (`static/js/app.js`) still reads "Billy-bot".

## Wave 2 — integration & verification (sequential, main session)

- gjs `Reflect.parse` syntax check on every touched JS file; `gate.py --quick`.
- Re-run `scripts/mobile_layout_scan.py --viewport phone` → expect 0 findings.
- Repo-wide sweeps that couldn't run in parallel: brand spelling, UK-spelling
  leftovers, "Inactive" renames referenced across files.
- Manual/README sync check for every UI change (CLAUDE.md rule), logical commits
  with Testing: checklists.

### Verification results (main session)

- **Phone layout sweep: 0 findings across all 179 routes** (was 110 viewport +
  6 clipped). Took three passes: the first-pass `.qr-table { min-width:
  max-content }` made the quick-reference tables *worse* (the real `.qr-table`
  rules live inside manual.html and there is no wrapper to scroll), and the
  last 15px came from `.dk-help .steps li` being a flex row whose text sits in
  an anonymous flex item that can't shrink below min-content.
- Boot smoke (scratchpad/boot_smoke.py): no page errors; 179 nav items with
  glyphs + tooltips; per-panel `document.title`; focus to panel root; unknown
  route notice; keyword filter resolves "thread" → Auto-Thread. As a moderator:
  35 locked entries (disabled + aria-disabled + "— Admin only"), Guess Who /
  Whisper reachable past the Games gate, no duplicate nav ids.
- Static checks: 0 broken cross-module imports across 136 panels; every panel
  still exports `mount()`; 40/40 help anchors resolve with 0 label↔heading
  mismatches; all 54 manual deep-links resolve; 27 `help:` and 12 `related:`
  nav targets resolve.
- Python: ruff + pyright clean on config.py/server.py;
  test_authz_sweep + test_config_routes + test_help_links pass (87 + 4).

## Round 2 — follow-ups after the review commits

Triggered by the user's questions: is the Bump Tracker in use, pull the
hardcoded names, fix anything remaining.

### Bump Tracker: it is live, so the panel got built (not the manual watered down)

Checked the production DB read-only: `enabled=1` for **two** guilds, 9 sites
across them (disboard, discadia, discord home, discords.com, discordus,
discord.me, unfocused), each with a reminder channel, ping role and a live
widget message; most recent logged bump was ~3h before the check. The only
slash commands are `/bump log` and `/bump status` (member self-service, which
is where CLAUDE.md wants them) — every admin knob had **no surface at all**, so
the live guilds were configured by editing the database by hand.

- New `panels/config-bump-tracker.js` under Config › Server, covering the whole
  existing API: reminder channel + ping role + master toggle, per-site cooldown
  and detector bot, add/remove a site, record a bump, live per-site status with
  countdowns that re-tick each minute.
- Four route tests added (`test_config_routes.py`) — these endpoints had **zero**
  coverage despite being the only way to configure a feature running in prod.
- `docs/bump_tracker_spec.md`, `docs/INDEX.md` and manual.html updated.

### Hardcoded product names → per-guild branding

Migration `115_branding_product_names.sql` adds `casino_name` and
`assistant_name` to `branding_config` (NULL = built-in default), resolved
through `branding_service`, editable on Config › Branding, with 23 tests.
Defaults preserve today's text for the existing guild.

Deliberately **not** renamed: the `golden_meadow` quote *theme* is a colour
preset whose label lives in globally-registered slash-command choices, so it
cannot be per-guild — label is now the neutral "Golden", storage key untouched.
Docs that genuinely describe the production guild (`server_map.md`, deploy
notes, the applied 113 migration header) keep their names.

### Bugs found while closing out

- `table.js` attached a **new** click listener per render, each closure holding
  its own `sortKey`/`sortAsc`; panels that re-render on filter change stacked
  them, so a header click sorted several ways at once. Handler is now tracked
  per container and detached first.
- `/api/reports/time-to-level-5` seeded `display_name` with `str(user_id)`.
  `resolve_names()` only fills a *falsy* name field, so that truthy placeholder
  defeated both its known_users lookup and its "User <id>" fallback — members
  outside the live cache rendered as raw snowflakes. Seed is now `""`.
  Regression test written first and confirmed failing on the old code.
- `mod-policy-tickets` never re-fetched after mount (the W-D5 treatment had not
  reached it); now polls on the same 45s/visibility rule as Tickets and Jails.
- Nav still hardcoded "Billy-bot" as a label → "AI Assistant", old name kept as
  a search keyword.
