# Website UX deep review — 2026-07-22

**Scope:** the whole dashboard UX — navigation/IA, config-panel workflow, data/analytics
panels, accessibility, mobile layout, help & onboarding. Branch `website-ux` @ eaeb831b.

**Method:** real-browser sweep of all 179 panel routes at phone viewport
(`scripts/mobile_layout_scan.py`, throwaway server + temp DB) + five parallel review
agents, each instructed to verify rather than re-report the 2026-07-01 deep-review
findings. Every finding below was read from current code with `file:line` evidence.

**Verdict:** the July a11y/responsive push worked — of the prior review's UX findings,
**U1a–U1k are all fixed** (toast/dialog/combobox/slider/nav ARIA, contrast token),
U2a/U2b/U2f fixed (table scroll, 16px inputs, stat grid), and 173/179 panels are clean
at phone width. The remaining problems cluster around **five systemic themes**, not
scattered polish: (1) unsaved-edit and fetch-failure data-loss traps in config panels,
(2) app state that never reaches the URL or the focus system, (3) empty/error states
that exist as shared helpers but were adopted by ~10% of panels, (4) one widget concept
rendered many ways, and (5) a handful of real bugs (inert canvases, duplicate
listeners, a gating intersection that locks moderators out).

No S1 blockers. Severity scale matches the July review: S2 major / S3 minor / S4 nit.

---

## Prior-finding scoreboard (2026-07-01 → today)

| Prior ID | Status | Evidence |
|---|---|---|
| U1a toasts aria-live | **fixed** | ui.js:8-10, error toasts `role="alert"` (ui.js:19) |
| U1b confirm/prompt dialog roles + focus trap | **fixed** | ui.js:38-68, 77, 106 |
| U1c transcript modal | **fixed** | transcript-modal.js:25-84 |
| U1d invalid partial tablist | **fixed by demotion** | strips now `role="group"`; but selected state still visual-only → W-A4 |
| U1e combobox ARIA/keyboard | **fixed in shared widget** | filter-select.js:97-199; two pre-fix clones survive → W-A5 |
| U1f/g/h nav headers, guild picker, rules-watch rows keyboard | **fixed** | app.js:561-620, 700-745; rules-watch.js:373-380 |
| U1i nav filter label / U1j slider valuetext | **fixed** | index.html:30; slider.js:26-53 |
| U1k --ink-mute contrast | **fixed** | #979ba3: 4.53–5.91:1, documented in app.css:17-19 |
| U2a table clipping / U2b 13px inputs / U2f mod-stats grid | **fixed** | app.css:2765-2801, 2788 |
| U2c 100vh shell | **fixed** (residue: connection-graph.js:49 inline `100vh`) |
| U2d sub-44px tap targets | **still open** → W-A6 |
| U2e safe-area insets | **half-shipped** — `viewport-fit=cover` added, zero `env()` padding → W-A9 |
| U3e tab-strip extraction | **fixed**, adoption 5 → 8 panels |
| U3f api() dedup | **fixed** — single `request()` core (api.js:54-91) |
| U3g loading/empty/error consolidation | **helpers exist, adoption ~10%** → W-D1 |
| U3a inline styles | **backslid**: 631 → 883 occurrences |

---

## Top priorities (cross-dimension, ranked)

1. **W-C1 + W-C2** — config data-loss traps: no dirty tracking anywhere; meta-load
   failure can zero saved channel/role settings on the next save.
2. **W-D2/W-D3** — Connection Graph & Interaction Heatmap go permanently inert after
   any empty state (canvas destroyed, listeners never rebound); Connection Graph has no
   loading or error state at all.
3. **W-A1** — focus, document.title, and announcement are all lost on every panel
   switch; no skip link. The single biggest remaining keyboard/screen-reader gap.
4. **W-M1** — six panels with real phone overflow (worst: help-overview's quick-ref
   table 1195px past the edge, clipped unreachable).
5. **W-N1** — sub-page state (tabs, filters, selected ticket) never reaches the URL:
   not deep-linkable, not refresh-safe, skipped by back button.
6. **W-N2** — Guess Who / Whisper config panels are unreachable for the moderators
   they're scoped to (section-gate × item-perm intersection).
7. **W-D4** — the 13 health-* panels have zero empty states; quiet/new servers see
   blank grids and a nonsense "∞× difference" insight.
8. **W-H1** — the full staff manual is served unauthenticated and embeds one guild's
   role name.

---

## Mobile layout scan (real browser, phone viewport)

**W-M1 (S2, aggregate).** 179 routes swept; 173 clean; 6 with genuine faults — in each
case `div.panel`'s `overflow-x: hidden` makes the clipped content unreachable:

| Panel | Overflow | Offender |
|---|---|---|
| help-overview | **1195px** | `.qr-table` quick-reference table — needs its own scroll container |
| health-mod-engagement | 235px | `.home-grid`/`.home-card` stat cards drag a data table off-screen |
| qa-tracker | 96px | `.ctrl-group[data-filter-group]` fixed flex row of filter buttons |
| wellness-caps | ~24px | `.w-histo-slider-cell` histogram slider cells clip their value labels |
| config-ai | 28px | `.btn-primary[data-action]` buttons stick out (×6) |
| help-setup | 120px | one long unwrapped link |

Fixes are the conventions we already have: wrap/scroll flex rows, `overflow-x:auto` on
wide tables, `overflow-wrap:anywhere` on link-bearing prose.
Data: scratchpad `mobile_scan_phone.json` (regenerate with
`.venv/bin/python scripts/mobile_layout_scan.py --viewport phone`).

---

## Navigation & information architecture

Strengths: single-source `SECTIONS` registry; layered perm filtering never leaves dead
headings; filter reveals through collapsed groups and matches section+subgroup+label;
accordion + guild picker have correct ARIA/keyboard; import/mount errors render a
visible message; Home is a perms-filtered, persistable widget grid; help search is
genuinely good. Panel titles match nav labels (15-panel spot check clean).

- **W-N1 (S2)** Sub-page state never in the URL. Zero panels write `location.hash`
  for tabs/filters; router supports params (app.js:501-506) but only `gt` and help
  `?focus` use them. Tabs reset on refresh; can't link a colleague to a ticket queue
  tab. Analytics panels already do this right (activity.js:241,
  connection-graph.js:1001, xp-leaderboard.js:57) — extend that convention to
  mod-tickets/jails/rules-watch/qa-tracker/todo via `history.replaceState`.
- **W-N2 (S3, functional bug)** Guess Who / Whisper configs unreachable for their
  audience: items carry `perms:["moderator"]` (app.js:257-262) but sit in the Games
  section gated to admin ∨ game-host (app.js:318-322); a moderator without game-host
  never sees them, a game-host without moderator has them filtered. Move under
  Moderation or exempt mod-visible items from the section gate.
- **W-N3 (S2)** Collapsed sidebar rail is a column of identical `#` icons — no
  per-item glyphs, no `title` tooltips, labels/filter/subgroups hidden
  (app.js:525-527; app.css:382-384, 288, 220, 323). Minimum fix: `btn.title = label`.
- **W-N4 (S3)** Unknown/forbidden hash silently falls back to Home keeping the stale
  URL (app.js:650). Render "not found or not available to you" instead; the
  known-but-forbidden case is statically distinguishable.
- **W-N5 (S3)** Admin gating is invisible-only — moderators see a 7-item Config rump
  with no hint the rest exists (app.js:364-382). Lock glyph + tooltip, or a Help note.
- **W-N6 (S3)** Games section: 20 single-item subgroups mostly labeled just "Config"
  (app.js:201-271) — three nav levels wrapping one link; ten indistinguishable
  "Config" entries. Flatten single-item subgroups to items named after the game.
- **W-N7 (S3)** Guild switch = silent state loss: remounts current panel with no
  dirty guard (app.js:770-786); primaryOnly pages vanish → silent Home fallback.
  Pairs with W-C1's dirty tracker; add a toast on fallback.
- **W-N8 (S3)** Sidebar collapse + accordion open-state don't persist (no
  localStorage in app.js; renderNav rebuilds and collapses on every navigation).
- **W-N9 (S3)** Home tiles for plain members click through to moderator-only pages and
  silently bounce back Home (widget-grid.js:123-129 unconditional; targets live in
  `perms:["moderator"]` sections). Only attach the handler when target is visible.
- **W-N10 (S3)** Misfiled panels: Docs (a channel-publishing tool) under Config ›
  Moderation & Safety (app.js:142) — move to Channels & Messages or rename "Channel
  Docs"; Grant Audit alone under Reports › Member Lists while five sibling audits live
  in Moderation › Audit Logs (app.js:79 vs 95-101); read-only Birthday Calendar
  sandwiched among config forms (app.js:129).
- **W-N11 (S3)** "Inactive" ×4: Inactive Role / Inactive Members (Reports) vs
  Auto-Remove Role (Inactive) / Inactive Sweep (Config) — filter "inactive" gives four
  undifferentiated hits, and inactive-role's h2 doesn't match its nav label. Rename to
  pair report↔config explicitly and cross-link.
- **W-N12 (S3)** Reports › People is a 12-item catch-all (app.js:51-64) — split
  Engagement vs Social Graph. **W-N13 (S3)** Nav filter matches labels only — no
  keywords/aliases ("prune", "thread reply" find nothing), no Enter-to-open, no clear
  button (app.js:469-472). **W-N14 (S4)** Panels are islands: ~15 cross-links total
  outside help; paired report↔config panels should link both ways. **W-N15 (S4)**
  single-guild chevron still renders (no `.single-guild` CSS); dead `sec.direct`
  branch (app.js:550-555); route-id vocabulary inconsistent (freeze current ids, adopt
  convention for new).

---

## Config panels (41 files)

Strengths: one HTTP core with readable 4xx normalization; `showStatus` save feedback in
41/41 with no silent save failures; snowflake string discipline; shared combobox is
solid and 12 panels migrated; confirmDialog/promptDialog with focus traps; standout
patterns worth copying — config-prune's "who would be removed" preview,
config-confessions' not-yet-configured state, config-moderation's purge intercept,
economy-config/economy-sinks per-heading cards with consequence-stating hints,
config-casino's named-field validation.

- **W-C1 (S2)** No unsaved-changes protection anywhere: 38/41 explicit-save forms,
  0/41 dirty tracking, 0 `beforeunload` — any sidebar click or guild switch discards
  edits silently. One shared dirty-tracker in config-helpers (form `input` sets flag;
  router + beforeunload prompt; save clears) fixes all 41.
- **W-C2 (S2)** Meta-load failure can zero saved settings: `/api/meta/channels`
  failure is swallowed to `[]` (config-helpers.js:20-24, 42-46); legacy `channelSelect`
  panels then render "(disabled)" and a save on an *unrelated* field posts `"0"` for
  every channel/role. filterSelect panels are immune (setValue keeps the id). Surface
  the failure + disable Save, or keep saved ids as synthetic options.
- **W-C3 (S3)** Welcome "Preview" previews the *saved* config, not on-screen edits
  (config-welcome.js:195-206 → GET; routes/config.py:1294-1331 reads stored values).
  Users verify copy they didn't type. POST current field values instead.
- **W-C4 (S3)** Six renderings of "pick a channel/role": searchable combobox (12
  files), legacy plain select (14), Ctrl/Cmd-click multi-select (4 — mobile-hostile),
  checkbox wall (5), hand-rolled member search (config-prune.js:16-75), raw
  comma-separated IDs (config-global.js:36-38). Finish the mount*Picker migration,
  then delete `channelSelect`/`roleSelect`.
- **W-C5 (S3)** Blank numerics → `NaN→null` → raw "422: Input should be a valid
  integer" with no field name (economy-sinks.js:370-372, economy-config.js:320-322,
  config-xp.js:168-181; 53 `parseInt` sites with no fallback). `required` + client
  validation naming the field (config-casino.js:150-170 is the model).
- **W-C6 (S3)** Feature-off states not communicated: sinks/quests/income panels render
  fully live against a disabled economy; pen-pals' enabled dial gates nothing below
  it. Generalize confessions' two-state pattern into a shared "feature is off —
  enable on <link>" banner.
- **W-C7 (S3)** Wall-of-knobs panels: config-voice-master (17 flat fields, 3 toggle
  styles, negated "Disable saves" dial), config-welcome (12 fields × 4 concerns),
  config-moderation (irreversible purge visually identical to a log-channel picker),
  config-global, config-booster-roles. Apply the economy-config card treatment.
- **W-C8 (S3)** Toggle idiom ×4 (checkbox / On-Off select / Yes-No select / radio) and
  mixed auto-save-vs-explicit rows inside single panels (games-config.js:131-139 vs
  183-213; config-bios.js:282-291 vs 486-497) — users can't form a model of when
  changes commit. One idiom + one feedback channel per surface.
- **W-C9 (S3)** Unconfirmed destructive delete: icon-catalog Delete destroys a curated
  icon + upload in one click (economy-sinks.js:417-428) while every sibling flow
  confirms; native `prompt()` for emoji-denial reason (economy-sinks.js:345) despite
  `promptDialog` existing. Same native-dialog issue in docs.js:231-241.
- **W-C10 (S4)** confirmDialog drops the `title` option callers pass (ui.js:70-71 vs
  config-moderation.js:92-95); save-button label drift ("Save/Apply/Save Defaults");
  "colour" leftovers vs the US-spelling ruling (economy-sinks.js:15); terse jargon
  labels ("(s)", "Image Path", "Sort Order" direction unstated).

---

## Data / analytics / mod-workflow panels

Strengths: charts.js centralizes palette, dark theme, tooltips, zoom+reset across ~25
chart panels; the health family shares one spinner/error pattern; activity.js is the
exemplar (states.js + tz label + window slider); mod-tickets triage (count badges,
AND-search, confirm+reason, race-guarded detail cache); rules-watch label→auto-advance
flow; analytics panels persist controls to the hash; guild switch fully remounts.

- **W-D1 (S2, systemic)** Loading/empty/error is four coexisting idioms: states.js
  adopted by 13/136 panels (~10%), `.panel-loading` spinner ×14, `withLoading` ×23,
  hand-rolled `class="empty"` ×77, `renderError` ×5. states.js's own header admits
  "only the highest-traffic panels were converted". Finish the U3g consolidation.
- **W-D2 (S2, bug)** Connection Graph: main `fetchData()` has no loading and no error
  path (connection-graph.js:968-985, 1083-1095) — failure = blank panel forever. And
  the empty state replaces the canvas's parent innerHTML, after which mouse listeners
  are never rebound (`// Rebind mouse events` comment with no code,
  connection-graph.js:1009-1019) — drag/zoom/hover/fullscreen permanently dead.
- **W-D3 (S2, bug)** Same canvas-destruction in Interaction Heatmap
  (interaction-heatmap.js:278-281 vs 76-82): a later refresh draws into a detached
  canvas — panel appears permanently empty until re-navigation.
- **W-D4 (S2)** All 13 health-* panels lack empty states: quiet servers get headerless
  blank tables (health-churn-risk.js:24) and an all-zero grid yields "∞× difference"
  (health-heatmap.js:46-48). Threshold-check → `renderEmpty("Need ~7 days of data…")`.
- **W-D5 (S2)** Mod queues never refresh: single fetch at mount (mod-tickets.js:642),
  "2h 10m left" countdowns computed once and frozen (mod-jails.js:46-51, 306-316), no
  data-as-of stamp, no refresh button. 30–60s poll or client-side countdown re-render.
- **W-D6 (S3)** Wheel-zoom hijacks page scroll over every chart (charts.js:44-49, no
  `modifierKey`). Set `modifierKey:"ctrl"` and say so in the reset button title.
- **W-D7 (S3)** Errors dressed as empty states (rules-watch.js:433-462,
  games-logs.js:120-161 grey `.empty` "Failed to load"); economy-stats error replaces
  only the summary strip leaving 7 sections "Loading…" forever (economy-stats.js:
  237-245); mod-tickets detail-fetch failure is console-only (mod-tickets.js:480-483).
- **W-D8 (S3)** Five time-range selector variants (rangePicker ×10 vs custom selects
  vs "Period" vs raw "Lookback (days)" input vs fixed-30d-no-control). Standardize on
  rangePicker.
- **W-D9 (S3)** Workflow panels don't persist filter/tab/selection state (ties to
  W-N1). **W-D10 (S3)** connection-graph redraws at 60fps forever even for static
  layouts (connection-graph.js:654) — real battery cost. **W-D11 (S3)** serial fetch
  waterfalls on the two heaviest economy panels (economy-stats.js:87-92,
  economy-quests.js:44-52) — `Promise.all`. **W-D12 (S3)** games-logs shows raw UTC
  ISO slices while every fmtTs panel shows local (games-logs.js:136, 174-175).
  **W-D13 (S3)** silent truncation: rules-watch limit 100/200, mod-jails "last 200" —
  show "first N of M". **W-D14 (S3)** table.js renders nothing on empty data
  (table.js:18) and xp-leaderboard builds an unbounded DOM table.
- **W-D15 (S4)** three palettes across analytics (charts.js vs economy-stats vs
  activity); live-log filter only applies to new lines (live-log.js:56-58);
  message-search has dead `applyFilters` with a latent UTC bug and no unmount
  (message-search.js:178-217, 338); mod-tickets loads full closed history unpaginated
  (mod-tickets.js:397); canvas-drawn tooltip not copyable (connection-graph.js:627-649).

---

## Accessibility (beyond the fixed prior findings)

Strengths: five dedicated a11y commits fixed the entire shared-widget layer; 62 panels
inherit the accessible combobox; honest `role="group"` over cargo-cult tablist; global
`:focus-visible` ring; contrast math documented in the stylesheet; modal traps filter
to visible+enabled; keyboard walkthrough now mostly passes.

- **W-A1 (S2)** Panel switches destroy focus and context: focus falls to `<body>` (a
  keyboard user re-tabs ~100+ sidebar stops), no announcement, no skip link,
  `document.title` never changes (app.js:647-668, 545). Fix in `mountPanel`: set
  title, focus panel root (`tabindex="-1"`), add a skip link.
- **W-A2 (S3)** Focus ring `--blurple` = 2.74:1 on `--bg` (fails 3:1), and 9 `:focus`
  rules override it with `outline:none` leaving a 1px border swap
  (app.css:54-57; 652, 680, 696, 715, 734, 751, 2935, 3365). Use `--gold-solid`
  (6.82:1) and re-assert under `:focus-visible`.
- **W-A3 (S3)** Semantic status text fails AA: `--red` 3.35:1, `--green` 3.97:1 at
  13-14px — error text is the thing you must read in sunlight (app.css:1048, 764-765,
  945). Add lighter text-tier tokens; keep saturated ones for borders/badges.
- **W-A4 (S3)** Strip selected state is class-only — `makeFilterStrip.setActive`
  toggles `.active`, no `aria-pressed` (tab-strip.js:15-27); a 2-line change fixes all
  8 adopter panels; migrate config-bios/gender-admin hand-rolls.
- **W-A5 (S3)** activity.js:39-72 and connection-graph.js:20-52 still ship the
  pre-a11y combobox clone: keyboard users **cannot pick an option at all** (also
  bloats connection-graph). Delete clones, import the shared widget.
- **W-A6 (S3)** Touch targets broadly sub-44px with no mobile bump: `.ctrl-group`
  buttons ≈25px/21px (app.css:2856-2865), sidebar-toggle 20×20, chip-remove ≈14×22,
  14px slider thumbs on a `pointer-events:none` track, hamburger 36×36. Bump in the
  768px block.
- **W-A7 (S3)** ~272 sibling-style `<label>` with zero `for=` attributes anywhere in
  the panel tree (vs ~200 fine wrap-style) — screen readers announce placeholders,
  label taps don't focus. Pick the wrap idiom; generate id/for in a field helper.
- **W-A8 (S4)** No `prefers-reduced-motion` block (29 transitions + spinners + Chart.js
  animations). **W-A9 (S4)** `viewport-fit=cover` shipped with zero
  `env(safe-area-inset-*)` — toasts/hamburger can now sit under notch/home-indicator;
  the half-fix makes the missing half actively needed (app.css:3258, 2728).
  **W-A10 (S4)** closed mobile drawer stays in tab order (translateX only, no
  `visibility:hidden`/`inert`), no Escape-to-close, no focus move on open, and no body
  scroll-lock behind the backdrop (app.css:2750-2760; app.js:413-437). **W-A11 (S4)**
  guild-picker menu stacks a duplicate keydown listener per guild switch
  (app.js:728-745 inside populateGuildPicker) — Enter fires switchGuild N+1 times.
  **W-A12 (S4)** toasts not keyboard-dismissable; all pickers share the accessible
  name "Type to filter…" — thread a `label` option through mountPicker.

---

## Help, onboarding & docs

Strengths: help-sections.js single-source map — all 40 anchors verified resolving;
manual full-text search with deep links; Ask Billy-bot with graceful failure copy;
login error codes exactly match login.html's messages; **manual discipline is real: 8/8
recently-shipped features documented**; US-spelling sweep complete in the manual; zero
broken internal manual links.

- **W-H1 (S3)** Manual served unauthenticated (`/static/*` bare StaticFiles mount,
  server.py:407) — the full 162KB staff/mod guide is world-readable while every panel
  requires OAuth — and it embeds "(in this server, the spicy-access role)"
  (manual.html:933), a guild-specific fact in a shared doc. Decide public-vs-gated
  deliberately; make the copy guild-neutral either way.
- **W-H2 (S3)** No contextual help from panels: plumbing exists (`#/help-*`,
  `?focus=`) and the manual links *to* panels, but zero panels link back. Add a "?"
  affordance to panel headers driven by a `panelId → help page` map.
- **W-H3 (S3)** No first-run experience: new guild's Home is zero-value stat tiles;
  the manual's First-Time Setup checklist is unlinked bold text whose names have
  drifted from the nav ("Roles"→Role Grants, "XP"→XP Logging, "Welcome"→Welcome &
  Leave). Link the checklist steps; consider a dismissible "New here?" Home banner.
- **W-H4 (S4)** Login page says "Moderator Dashboard" while the product onboards
  ordinary members, and never says what the dashboard is (login.html:99). **W-H5
  (S4)** brand spelled three ways ("Dungeon Keeper" / "DungeonKeeper") across
  login/manual/help. **W-H6 (S4)** Help panel shows two nonidentical titles per page
  (nav label + manual h2 with section number); voice feature answers to three names.
  **W-H7 (S4)** quest-board freeze documented in the panel but absent from the manual
  (one clause fixes it).

---

## Suggested fix batches

Each batch is a coherent commit-sized unit; **bold** = highest leverage.

1. **Safety net batch (W-C1, W-C2, W-N7):** shared dirty-tracker + beforeunload +
   guild-switch guard in config-helpers/app.js; meta-load failure banner + Save
   disable. Kills both data-loss traps in one pass.
2. **Bug batch (W-D2, W-D3, W-A11, W-N2):** canvas empty-state overlays + listener
   rebind; guild-picker listener dedup; re-home Guess Who/Whisper configs.
3. **Mobile batch (W-M1 ×6, W-A6, W-A9, W-A10):** six overflow fixes, tap-target bump,
   safe-area insets, drawer inert/Escape/scroll-lock — one `app.css` + small JS pass;
   re-run the scan to verify zero findings.
4. **States batch (W-D1, W-D4, W-D5, W-D7):** finish states.js adoption (mechanical),
   health-family empty states, mod-queue polling + as-of stamps, error-vs-empty
   distinction.
5. **Focus & URL batch (W-A1, W-N1/W-D9, W-N4):** document.title + focus-on-mount +
   skip link; tab/filter state → hash convention; not-found page.
6. **Consistency batch (W-C4, W-C7, W-C8, W-D8, W-A4, W-A5):** picker migration,
   card-layout for the worst 5 config panels, one toggle idiom, rangePicker
   everywhere, aria-pressed in tab-strip, delete combobox clones.
7. **Help & copy batch (W-H1–W-H7, W-N11, W-C10):** manual gating/neutralizing,
   contextual "?" links, linked setup checklist, naming sweeps.
