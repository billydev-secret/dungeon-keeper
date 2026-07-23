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
- "Billy-bot" and "Golden Meadow" are product names baked into code that also
  read as guild-specific; genericizing either is a code-wide rename, not a
  manual edit.

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
