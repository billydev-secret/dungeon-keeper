# Pools: its own dashboard config page

**Status:** Complete 2026-07-28. Written as a design doc while blocked on
sequencing, then built in the same session once Ben settled the three open
questions and chose to proceed ahead of the two sibling sessions. The
[Sequencing](#sequencing) section is kept as-is because the merge risk it
describes is still live and still has to be handled at rebase time.

## Goal

Move the four Pools settings off the shared **Economy → Casino** page onto their
own admin-gated page, so the daily prediction market stops sharing a pane with
nine instant-settle house tables.

## Why Pools is not a tenth table

From `docs/casino_spec.md:142-145`, the three ways Pools differs from everything
else on that page:

- **It ships off** (`pools_enabled` defaults `false`); the nine tables default on.
- **It wants its own channel** (`pools_channel_id`) because a round runs a full
  day, rather than living above the casino hub panel.
- **Its takeout is burned outright.** This is the sharp one: `pools_takeout_pct`
  destroys currency, while the adjacent `jackpot_cut_pct` skims per lost stake
  into a pot that *re-mints* it. Two percent knobs, one card apart, with
  opposite effects on the money supply. That adjacency is an argument for the
  split on its own.

Pools is a deflationary sink with a day-long lifecycle. The rest of the page is
instant-settle gambling.

## Current state (verified 2026-07-28, not assumed)

**Pools is live in prod.** The brief for this work said there were 0 rounds, 0
bets, and no config keys set on any guild. That is wrong, and it changes the
risk profile:

```
casino_pools_rounds : 1 row  — guild 1469491362444480666, local_day 2026-07-28,
                               status open, line 4853.5, closes 18:00
casino_pools_bets   : 0 rows
config              : casino_pools_enabled=1, casino_pools_channel_id=1531785381769515239,
                      casino_pools_close_hour=18, casino_pools_takeout_pct=5
```

So there is a real, configured, currently-open round on the primary guild. The
settings are in use, not dormant.

This is still **not** a data migration: the four keys keep their existing
`casino_pools_*` names in the `config` table, and `casino_service.load_casino_settings`
keeps reading them. Only the *panel* that edits them moves. But it does mean the
change is not risk-free — a save from the new page hits the same writer and the
same live cog, and it should be tested against a round that is actually open.

**Telemetry cannot inform this decision yet.** `usage_events` holds 16
`panel_view` rows total (the feature shipped ~2 days ago, commits `dedce955` /
`de20b392`). `config-casino` appears among them. There is no volume to argue
either direction from, so this stays a judgment call about what the settings
*are*, not about who opens which page.

## Sequencing

Two sibling sessions edit `static/js/app.js`'s nav array right now:

| session | state | what it does to `app.js` |
|---|---|---|
| `config-page-organization` | 1 dirty file, +11/−33, **waiting on Ben** | Deletes the seven "Prompts & AI" studio sub-pages and the `gt` query param. Games section only — does not touch the Casino line. |
| `panels-into-feature-pages` | 23 files dirty, staged | Moves the seven post-panel controls onto feature config pages. Edits the **Economy** section around L185-189 — adjacent to `config-casino` — and adds a `MOVED_PAGES` redirect map. |

**Decision (Ben, 2026-07-28):** initially "wait for both to merge", then
revised to build immediately once the findings below showed the change was
smaller than expected (no backend work, no new route, light test surface).
`config-page-organization` is blocked on Ben rather than on itself, so waiting
had no fixed end date.

**Therefore this branch must be rebased onto main after both siblings land**,
and the `app.js` nav hunk re-checked by hand. The Pools entry is inserted
directly after `config-casino` in the Economy section — `panels-into-feature-pages`
edits the `economy-config` line four rows below it, which is inside the same
diff context, so expect git to want help there.

The mechanical conflict risk is small — three narrow, mostly non-overlapping
hunks. The reason to wait is editorial, not mechanical:

> `config-page-organization` is not applying a blanket "fewer pages" doctrine.
> It is deleting pages that were **stubs** — the studio sub-pages had almost
> nothing left on them once the AI prompt studios were removed. A Pools page
> would be one checkbox, one channel picker, and two number inputs. That is
> stub-shaped, which is the same silhouette that session is currently removing.

If the four controls are judged too thin to carry a page, the fallback is a
clearer visually-separated section on the Casino page rather than a new route.
That question should be settled before implementation starts, not during.

**Reuse from `panels-into-feature-pages`:** its `MOVED_PAGES` map is exactly the
mechanism a Pools split wants for anyone who bookmarked the Casino page for the
Pools settings. Pools does not retire `config-casino`, though — no redirect
entry is needed. What is needed is a `related: ["config-pools"]` cross-link on
the Casino nav entry and the reverse, so the two pages point at each other.

## Implementation

### Backend — no change required

This is the part worth getting right, because the brief anticipated a new route
and the authz sweep that comes with it. None of that is necessary.

`CasinoConfigUpdate` (`routes/config.py:4156`) is `extra="forbid"` with **every
field optional**, and the handler builds its update from
`model_dump(exclude_unset=True)` filtered for `None` (`config.py:4218`). A
payload of just the four Pools keys is already a valid, correct partial save:

```js
apiPut("/api/config/casino", {
  pools_enabled: fd.has("pools_enabled"),
  pools_channel_id: poolsChanPicker.getValue() || "0",  // string — snowflake rule
  pools_close_hour: n.pools_close_hour,
  pools_takeout_pct: n.pools_takeout_pct,
});
```

Keeping this route means, for free:

- `pools_channel_id` stays stringified on read (`config.py:497`) and is
  `int()`-coerced on write alongside `channel_id` (`config.py:4220`) — the
  snowflake rule holds with no new code to get wrong.
- Bounds stay enforced server-side at `config.py:4181,4183` (`ge=0 le=23`,
  `ge=0 le=50`).
- The save still dispatches `casino_config_change` (`config.py:4242`), which
  `cogs/casino/cog.py:629` listens for to re-ensure the panel without a restart.
  A new `/api/config/pools` route would have to remember to dispatch this;
  forgetting it means a channel change that silently doesn't move the panel.

**Recommendation: do not add a route.** A new page does not require a new
endpoint. If a `pools` section is later wanted for tidiness, it should be a
follow-up with its own dispatch test, not smuggled into this move.

The only backend-adjacent risk: `_casino_section` (`config.py:472`) still
returns the pools fields, and the new panel reads them from `config.casino`.
That is fine and should be left alone — moving them to a sibling `config.pools`
section would break the existing Casino panel for no gain.

### Frontend

**New file:** `src/web_server/static/js/panels/config-pools.js`

`config-casino.js` is 348 lines and its local `field()` / `numInput()` /
`checkbox()` helpers (L12-48) are private to it. The new panel needs the same
three. Duplicating ~35 lines is the cheap option; the better one is lifting them
into `config-helpers.js` alongside the existing `buildField` and importing from
both. Prefer the lift — `buildField` already lives there, and `field()` exists
only to wire the `htmlFor`/`id` pair for screen readers (W-A7), which every
panel wants. Flag this as the one piece of collateral refactor in the change.

**Remove from `config-casino.js`:**

| lines | what |
|---|---|
| 158-199 | the entire `cardPools` block (checkbox, channel picker, both numbers) |
| 304-305 | two rows from the numeric validation table (`pools_close_hour`, `pools_takeout_pct`) |
| 336-337 | `pools_enabled` / `pools_channel_id` from the PUT payload |

Nothing else in that file references pools — `poolsChanPicker` is local to the
removed block. Deleting L158-199 leaves `cardTables` (Games) adjacent to
`cardJackpot` (Progressive Jackpot), which reads better than it does today.

**Nav entry** (`app.js`, Economy section, immediately after `config-casino` at
L185):

```js
{ id: "config-pools", label: "Pools", module: "./panels/config-pools.js",
  adminOnly: true, keywords: "prediction market daily over under parimutuel takeout",
  help: "help-pools", related: ["config-casino"] },
```

…and add `related: ["config-pools"]` to the `config-casino` entry. Both pages
stay `adminOnly`. The `keywords` matter more than usual here: "Pools" is a
weak search term against `confession_pools` / `pen_pals_pool` in the same
product, so the market-specific terms carry the search.

**Page copy.** The subtitle should carry the two facts an admin needs before
touching the knobs, because they are no longer visible next to the jackpot card
for contrast: the round is a full day long, and the takeout is *burned*. Most of
the hint text can move verbatim from `config-casino.js:166-199` — it is already
written to that standard.

### Help — the one real complication

`help-sections.js:32` maps `help-casino` → the manual's `economy-casino` anchor.
A `help-pools` entry needs an anchor of its own, and this is where it gets
awkward: Pools is currently an **`<h4>`** in the manual (`manual.html:955`,
inside the `economy-casino` `<h3>`).

`extractSectionContent` (`help.js:144-165`) only treats `H2` and `H3` as section
starts. An `h4` id falls through to `heading = start.closest("h2")`, which would
render the *entire Economy chapter* in the Pools help panel. So:

- **Option A — promote to `<h3 id="pools">`.** Correct rendering, one-line
  `help-sections.js` addition, but it lifts Pools out from under Casino in the
  manual's hierarchy, which is a user-facing restructure of the manual's table
  of contents. Consistent with the dashboard split, and probably right.
- **Option B — no `help-pools`; point the new page at `help: "help-casino"`.**
  Zero manual restructure. Costs the reader: the help "?" from the Pools page
  opens the whole Casino chapter and they scroll to find Pools.

Recommend **A** — a page that is its own page should have its own help section,
and the manual already gives Pools ~10 paragraphs. Either way `tests/web/test_help_links.py`
enforces that every anchor referenced actually exists, so a half-done job fails
the default suite rather than shipping.

### Docs to update in the same commit

- `docs/casino_spec.md` — the "Dashboard: **Economy → Casino**" paragraph
  (~L147) must name the new page for the four pools keys and keep
  `PUT /api/config/casino` as the writer for both.
- `docs/INDEX.md:109` — the plan row for `casino-classics-and-prediction-market.md`
  mentions Pools "ships disabled"; add this doc's row.
- `src/web_server/static/manual.html` — per CLAUDE.md, UI change ⇒ user-facing
  docs in the same commit. Under Option A this is the `h4`→`h3` promotion plus
  a heading-number pass.
- **Not README.md** — a settings page moving is not a change to what the bot is.

### Tests

Per CLAUDE.md the unit under test is the logic/service layer, and **the logic
layer does not change at all here** — `pools_logic.py` / `pools_service.py` /
`casino_service.py` are untouched. This is a panel move. So:

- **No new `*_logic.py` file** ⇒ the scoped gate's hard-fail for unmapped
  logic-layer files does not apply.
- `tests/web/test_casino_routes.py` — add a `pytest.param` row (not a new test
  function) asserting a **pools-only partial PUT** to `/api/config/casino`
  persists the four keys and leaves `min_bet` / `max_bet` / the nine table
  toggles untouched. This is the one behavior the split newly depends on, and
  it is currently only exercised implicitly by full-payload saves.
- The **authz sweep** covers the page for free — no new route means no new entry
  point to allow-list.
- The **browser suites** (`tests/web/test_mobile_layout.py`,
  `tests/web/test_panel_console.py`) enumerate panels from the live nav
  (`_panel_ids` → `enumerate_panels`, `test_mobile_layout.py:142`), so
  `config-pools` is picked up automatically once the nav entry exists. Run
  scoped with `PANEL_SCOPE=config-pools,config-casino`.
- `tests/web/test_help_links.py` covers the manual anchor.
- Measure phone width with `scripts/mobile_layout_scan.py`. The layout risk is
  low — the copied cards already use wrapping flex rows
  (`display:flex; flex-wrap:wrap`, `config-casino.js:160`), which is the pattern
  CLAUDE.md asks for.

### Commit

Subject: `Pools: move the daily market's settings to their own page`

Body covers: why Pools isn't a tenth table (burned takeout next to a re-minting
jackpot cut), that no new route was added and why, the help-anchor promotion,
and a `Testing:` checklist. Testing items must include, given prod has a live
open round:

- [ ] Open **Economy → Pools**; the four settings show today's live values
      (enabled, the configured channel, 18, 5).
- [ ] Change the takeout and save; confirm the Casino page's own save still
      works afterward and has not reset the Pools values.
- [ ] Move the Pools channel and confirm the market panel actually relocates
      without a restart (the `casino_config_change` dispatch).
- [ ] Confirm the Casino page no longer shows a Pools card and its save still
      persists every remaining field.
- [ ] Open the Pools page's "?" help link and confirm it renders only the Pools
      section, not the whole Economy chapter.
- [ ] Check the open round still settles at the day roll after the config save.

## Questions settled (Ben, 2026-07-28)

1. **Pools gets a real page** — `#/config-pools`, admin-only, under Economy
   directly after Casino. Not a section on the Casino page.
2. **Help: Option A** — Pools promoted to `<h3 id="pools">` in the manual, with
   a `help-pools` row in `help-sections.js`. One wrinkle found during the edit
   that the design missed: three paragraphs of general *Casino* text sat
   **after** the Pools `<h4>` (payout tables, table showmanship, play-again
   buttons). Promoting the heading in place would have swallowed them into the
   Pools section, so the block was **moved** below them rather than merely
   retagged. The manual's sidebar TOC is generated from `h2[id], h3[id]`, so
   Pools now appears there automatically — no numbering to maintain.
3. **Helpers lifted** into `config-helpers.js` (`field`, `numInput`,
   `checkbox`), imported by both panels. The id prefix went from `cc-field-` to
   `dk-field-` now that it is not casino-specific; nothing referenced the old
   prefix.

## What shipped

| file | change |
|---|---|
| `static/js/config-helpers.js` | +`field` / `numInput` / `checkbox` exports |
| `static/js/panels/config-pools.js` | **new** — the page |
| `static/js/panels/config-casino.js` | 348 → 265 lines: pools card, its two validation rows, its two payload keys, and the three now-shared helpers all removed |
| `static/js/app.js` | `config-pools` nav entry; `related` cross-links both ways |
| `static/js/panels/help-sections.js` | `help-pools` → anchor `pools` |
| `static/manual.html` | Pools moved below the Casino tail paragraphs and promoted `<h4>` → `<h3 id="pools">` |
| `docs/casino_spec.md` | records the split, and that there is deliberately no `/api/config/pools` |
| `docs/INDEX.md` | this plan's row |
| `tests/web/test_casino_routes.py` | `test_pools_and_casino_pages_save_past_each_other` |

Backend unchanged, as designed.
