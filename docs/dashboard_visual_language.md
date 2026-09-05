# Dashboard visual language (Reference)

What the dashboard is made of, visually, and the rules that keep 142 panels
looking like one tool. `dashboard_ia.md` owns *where a page lives*; this owns
*what it looks like once you're on it*. The palette, the type and spacing
scales and the two typefaces live in `src/web_server/static/tokens.css`; the
rules that use them are in `app.css`, with `help-panel.css` for the user
guide's content shapes.

## The palette does not change

The neutrals and the semantic colours are Discord's own values, deliberately.
The dashboard is the config surface for a Discord bot and reads as continuous
with it. Do not "modernise" the greys.

The accent is `--gold-solid` (`#e6b84c`). It had already won by usage long
before it was a rule — 65 uses against blurple's two.

Two accessibility decisions in `:root` are load-bearing and must survive any
future repaint. The comments carry the contrast maths:

- `--ink-mute` is lightened from Discord's `#80848e` to clear WCAG AA 4.5:1 on
  every surface it sits on.
- `--red-text` / `--green-text` exist because the saturated `--red` / `--green`
  measure 3.35:1 and 3.97:1 as 13–14px text. **Saturated for borders, badges
  and fills; the `-text` pair wherever the colour carries meaning as words.**

That two-tier split is **enforced**, not just documented:
`tests/web/test_css_contrast_tiers.py` fails if any rule uses the saturated
token as a text colour, and separately recomputes the contrast arithmetic on
the token pairs so retuning a hex in `:root` can't quietly push a combination
back under the floor. It was added after finding 23 declarations that used the
saturated pair as text — including the Tickets status chips (3.34:1) and
`.error` / `.save-err` / `.num-err`, which is to say the error messages.

**The same sweep now reads panel JS**, which is where the rule was actually
being broken. The original scanned `app.css` and `help-panel.css`; its regex
would have matched the JS offenders verbatim, but inline styles built from
template literals were simply out of scope — and that is where most of this
dashboard's colour decisions are made. Fifteen sites were using the saturated
tier as words: `config-moderation`'s Danger Zone eyebrow (11px uppercase, the
worst case for the split), `live-log`'s ERROR and CRITICAL lines at 4.37:1,
`gender-admin`'s save status, `system-stats`' backup rows, `table.js`, and
four tiles.

The JS half is **strict by default**: any saturated token written in JS is an
offender unless its line declares a fill (`background`, `border`, `fill`,
`stroke`, …). A first draft enumerated the shapes a colour can take instead —
a literal `color:`, an assignment to `.style.color`, a local holding
`"var(--red)"` — and missed `live-log.js` completely, because its map is a
multi-line object literal reached through a destructured loop variable.
Enumerating how a value can travel is a losing game; making the fill declare
itself is not.

Five declarations reach a fill from somewhere other than their own line — a
colour map read later as `background:${…}`, or a helper whose return value is.
Those are listed in `_INDIRECT_FILLS` with a reason each, and a second test
fails if an exemption stops naming a real fill, so an exemption can't rot into
a hole. Where the fill was an inline argument with nothing to name
(`channel-health`'s mini-bar), it was hoisted to a named constant rather than
given a special case.

## Every `var(--x)` must name a token that exists

An undeclared custom property fails silently in both directions.
`color: var(--danger)` with no `--danger` is an invalid declaration the browser
drops, so the element inherits whatever was above it; `color: var(--danger,
#e55)` is worse, because it always renders the literal and therefore looks
deliberate while ignoring the theme entirely. Neither raises a console error,
so panel-load health passes them too.

A sweep found **64 such uses across 19 names**: `--border` (29 uses in 12
files) resolving to a hardcoded `#333` that measures 1.00:1 against `--bg`,
`--ink-muted` — a typo for `--ink-mute` — inside the shell ~10 game panels
render through, and `--danger`, `--warn`, `--ok`, `--surface`, `--fg`,
`--muted`, `--dim` and `--font-mono` shadowing tokens that already existed
under the house names.

`tests/web/test_css_token_hygiene.py` fails on any reference to a token `:root`
does not declare. It reads **panel JS as well as the stylesheets**, because
that is where this concentrates: an inline style built in a template literal is
invisible to a CSS linter. Same gap let the saturated/`-text` rule above hold
in `app.css` and break in JS.

Status colour follows the same two-tier split as red and green:

| job | use |
|---|---|
| text on a page surface | `--red-text` `--green-text` `--yellow` `--ink-dim` |
| a tint behind that text | `--red-soft` `--green-soft` `--gold-soft` `--rule-soft` |
| a **saturated** fill | `--red` `--green` `--yellow` — and then the text on it is `--bg-rail`, never inherited |

Panels used to hand-roll this per file, which is how the health badges shipped
`#9E3B2E` on its own 20% tint at **1.84:1**, the QA chips built
`background:<hex>22; color:<hex>` from one table (three of five under 4.5:1),
and the funnel bar printed its count in `--ink` on solid gold at **1.37:1**.
Reach for `.badge-*` and `.t-chip.*` before inventing a colour.

## Buttons: one filled primary, and never a destructive one

`.act-btn` is the shared button, used by Tickets, Jails, Todo, Warnings,
Announcements, Docs and Role Menus. It has two meaningful variants:

- **`.ghost`** — transparent, outlined, dimmed. By far the most-used variant
  (13 call sites against three each for the others); it was for a while declared
  *twice*, once here and once inside the Docs panel's block but globally
  scoped, with the later one silently winning for the whole dashboard.
- **`.primary`** — the single filled button in a group, and it is always
  *the obvious next step*, which may depend on state. On a ticket that is
  Claim while nobody holds it and Close Ticket once you do.
- **`.danger`** — outlined, in the `--red-text` tier. Destructive actions read
  as destructive without shouting.

**A destructive action is never the filled one.** Jail used to carry a solid
red fill, which made "jail this person" the loudest control on the page while
you were still reading the queue. Solid-red styling belongs on the confirm
dialog's button, where a decision is actually being taken.

That rule reaches both button kits. `.btn-danger` — the `.btn` kit's
destructive variant, and the far more used one, 33 call sites against
`.act-btn.danger`'s three — was still a solid `--red` fill with white text, which
is 3.77:1 as well as too loud. It is now outlined in the same `--red-text`
tier, and the solid treatment is **scoped to `.confirm-box`**, which is the
exception the rule already named. That button's fill is Discord's darker
`#da373c`, because `--red` under white is under AA at `--btn`'s 12.5px.
`tests/web/test_css_contrast_tiers.py` pins both halves.

Where a pane's actions operate on more than one object, group them and say
which is which — Tickets splits **This ticket** (state) from **This member**
(a permanent moderation record), using the existing `.section-label`. A member's
display name inside such a label wears `.td-act-who`, which cancels the
uppercase: an eyebrow is uppercase, a name someone chose is not.

## Sequential ramps are quantized, because continuous ones cannot carry text

The activity heatmap paints each cell as gold over the card at an alpha set by
its value, and prints the count inside the cell. A **continuous** ramp cannot
do both: it necessarily passes through a band of mid luminance where neither
`--ink-bright` nor `--bg-rail` reaches 4.5:1, and the best any ink pairing
achieves at the crossover is **3.85:1**. No swap inside the Discord greys
escapes it — `--bg-floor` gets 4.11, pure black 4.35, and only pure white on
pure black clears it at 4.58.

Five buckets straddle the band instead of walking through it. The stops —
`0.04, 0.25, 0.46, 0.69, 0.96` — were **searched, not chosen**: they maximise
the weakest step between neighbouring buckets subject to every bucket's better
ink clearing AA. That matters because the obvious ramp, optimised for text
alone, reaches 5.26:1 on text and then collapses its top three buckets to
1.28:1 of each other — trading a contrast problem for an encoding one. The
shipped stops give 1.59:1 between every pair, evenly, so the scale reads as a
scale.

Quantizing only helps if a bucket can be read back as a number, and this panel
had **no legend at all**. It has one now. Bucket 0 means nothing happened and
is never a rounding of something that did — an hour with one message must not
look like an hour with none.

The arithmetic is pinned in `tests/web/test_css_contrast_tiers.py`, including
the neighbour separation, so retuning a stop for looks cannot quietly undo
either half.

## Anything you can click, you can reach with a keyboard

Three shapes on this dashboard were mouse-only, each because a plain `<div>` or
`<th>` was given a click listener and nothing else.

**Queue rows.** Tickets, Jails, Warnings and Todo render a list of
`.ticket-item` rows, and each drives its detail pane entirely from which row is
selected — so a keyboard-only moderator could not reach the right-hand half of
the moderation surface at all. The rows carry `tabindex="0"`, `role="button"`
and `aria-current`, and activation comes from `bindRowActivation` in `ui.js`
rather than a fifth copy of the same click/keydown pair. `.active` was
colour-only; `aria-current` is what says "this one" out loud.

**Sortable headers.** `renderSortableTable` backs 15 panels and emitted bare
`<th data-sort>` with a delegated click. `aria-sort` appeared nowhere in the
static tree, so the current sort was carried by a `::after` arrow alone.
Headers are now focusable, answer Enter and Space, and declare
`aria-sort="ascending|descending|none"`. Re-rendering replaces the table, so a
keyboard sort restores focus to the header it was on — otherwise the user is
dropped at the top of the document.

**Comboboxes.** A picker's slot is *replaced* by the widget, so `field()` can
never pair the visible `<label>` with it by id the way it does for a real
input. `mountPicker` now reads the label off the enclosing `.field` /
`.ctrl-field`, so a named control is the default and `label` is the override.
Before that, a caller who forgot `label` shipped a combobox announcing only
"Type to filter…".

The global `:focus-visible` rule means all three get the gold ring for free —
`--blurple` was 2.74:1, under the 3:1 floor WCAG 1.4.11 sets for a focus
indicator, which is why the ring is gold everywhere.

`tests/web/test_a11y_keyboard_rows.py` holds the line on all of it — plus the
one clickable **table row** (policy tickets), which keeps its implicit `row`
role rather than taking `role="button"`: overriding it would cost the
row/column semantics a screen-reader user navigates a table by.

A visible label only names a control when it is paired with it. `field()` does
that by id, but only for fields built imperatively; five panels build the same
`.field` markup as a template literal, so 26 controls had a label contributing
nothing. Fifteen are paired by id now, and the eleven that live in **repeated
row editors** — which have no stable id to pair against — carry `aria-label`
instead. `tests/web/test_panel_contracts.py` accepts either and rejects
neither.

That file also holds two contracts that fail silently in the same way:

- **A class in a `class="btn …"` attribute must exist**, in a stylesheet or in
  a panel's injected `<style>` block. `chat-revive` shipped `btn small`,
  `btn small danger` and `btn primary` — ten controls including a delete — and
  survivor and pen-pals-settings reached for `.btn-small` where the kit spells
  it `.btn-sm`. A class that no rule defines styles nothing and says nothing.
- **`mod-audit`'s action keys must be strings the bot actually writes.** Six of
  twelve were short forms nobody wrote, so the Jail, Unjail, Warning, Warning
  Revoke, Pull and Remove filters each matched zero rows over a log holding
  plenty of each, and every such row rendered its raw key as its own label.
  The test scrapes `action="…"` keyword arguments out of the bot, not any
  quoted token — the looser version passes with the bug still in place, which
  is how the first draft of it was caught.

## Two typefaces, both self-hosted

| Role | Family | Token |
|---|---|---|
| UI text — labels, inputs, nav items, body | Public Sans | `--sans` |
| Display — rail, panel titles, group labels | Archivo | `--display` |
| Data, ids, code | system monospace | `--mono` |

Files and licences: `src/web_server/static/fonts/README.md`. Nothing is fetched
from a third party at page load.

Until this landed the stack began with `"gg sans"` — Discord's proprietary
face, which ships with no browser. There was no `@font-face` anywhere in the
static tree, so **every page ever served actually rendered in Noto Sans or
Helvetica**. Public Sans is the closest openly-licensed match to the register
that was being asked for.

**Archivo's width axis is load-bearing.** It is variable on both weight and
width (`wdth` 62–125), and the nav rail uses width — not colour — to mark the
section you are in. Replacing Archivo with a non-variable face silently
deletes that signal. See below.

## The scales — pick a step, never invent a value

`app.css` had grown 17 distinct font sizes (including 9px, 13.5px and 19px) and
22 spacing values (1px through 60px). That, not the palette, is why panels
built months apart didn't feel related.

```
--t-1: 11px     eyebrows, tallies, rail section headers
--t-2: 12.5px   help text, subtitles, table meta
--t-3: 14px     UI text — labels, inputs, nav items, body
--t-4: 16px     group legends, card titles
--t-5: 20px     panel titles
--t-6: 28px     stat numbers

--s-0..--s-7:   2 4 8 12 16 24 32 48
```

The spacing rule is **compact rows, airy sections**: 8–12px between dials
inside a group, 24–32px between groups, so the whitespace itself carries the
grouping rather than a box or a rule doing it.

**Why `--t-2` is 12.5px and not 12 or 13.** Those were the two most common
sizes in the file — 61 and 59 declarations — sitting half a pixel apart and
doing the same job. A step at 12.5 collapses both into one at a sub-pixel cost.
The scale is fitted to what the dashboard actually used, not imposed on it.

The sweep is **done**, not deferred: `app.css` went from 17 font sizes and 22
spacing values with no tokens at all to 249 rules referencing a type token and
341 referencing a spacing token, with **29** literal font-sizes left — 14 inside
chart rules, 5 inside media queries, and 10 that are dimensions rather than type
(an icon's optical box, a sort arrow, an emoji badge, and the root `html, body`
size everything else inherits from).

Media-query numbers were measured against a real overflow bug and re-rounding
them re-breaks it; chart styling is a separate pass; a dimension is not a type
size. Beware the exemption pattern itself: `hm-` as a chart prefix also matches
`ihm-`, the interaction heatmap, which is how `.ihm-tooltip` and
`.ihm-frame-label` were wrongly skipped on the first pass — they are text.

The goal was never "everything is a token". It is that **nobody invents a type
size**. A value that is really a dimension is not a type size and does not
belong on the scale.

## Section icons

The ten sections are drawn on one grid in `static/js/nav-icons.js`: 16x16,
1.5 stroke, round joins, `currentColor` throughout so the rail's hover, active
and gold-when-current states work with no per-icon rules.

They replaced Unicode dingbats (`⌂ ▤ ⚖ ⚙ ¤ ♥ ⚄ ☺ ⚒ ?`) drawn by different
people for different purposes across nine Unicode blocks, which rendered at
different weights on every platform and — falling outside the latin subsets the
dashboard ships — came out of a system fallback rather than the page's own type.

Each one says what the section holds rather than decorating it: a tile grid for
the overview, ascending bars for Reports, a shield for Moderation (this surface
is no-contact lists and safety gates as much as bans), two sliders for Config
(the codebase calls settings *dials*), overlapping coins for Economy, a sprout
for Wellness, a die for Games, two linked nodes for Social — that section
literally contains a Social Graph — code brackets for Dev, and a question mark
for Help, where convention is the useful thing.

Two rules keep the set a set, both enforced by `tests/web/test_nav_icons.py`:

- **Every section has one.** A new section without an icon silently falls back
  to a dingbat, so nine are drawn and one is not.
- **No literal colours.** An icon that hardcodes a fill looks right in every
  state except the active one, which is the state that matters.

Note on placement: the icon names a *section*, so it renders on the section
header. It used to be stamped on every item, which drew eight identical shields
down Moderation and distinguished nothing. Items keep the element, hidden while
the rail is expanded and shown when it is collapsed — see the caveat below.

**Economy is worth one line of history.** It was first drawn as a vertical
stack of coins, which is also the universal database icon; at 15px in a rail
with no label beside it, that is what it read as. It is two overlapping coins
now.

## The signature: the rail is an index

The sidebar is the one surface on all 142 panels, so it carries the whole
visual identity and everything else stays quiet.

- **`.nav-group.current` sets wider** — 125% against 85% for the other nine
  sections, expressed with `font-stretch` rather than `font-variation-settings`.
  That distinction is load-bearing: the low-level property overrides
  `font-weight` *and* inherits into descendants, so a child that sets its own
  weight is silently ignored. Use `font-weight` and `font-stretch`; they reach
  the same `wght` and `wdth` axes and cascade properly. This is the "you are here" signal. It reads in peripheral
  vision, survives greyscale, and cannot fail on a colour-blind axis — which
  matters because colour is already spoken for by the semantic red/green.
- **`.current` is applied in `app.js`'s `renderNav`**, keyed off the section
  containing the active page. It is deliberately **not** `aria-expanded`:
  several sections can be open at once, so "expanded" and "where am I" are
  different questions.
- **The seam** is a 1px `--gold-seam` hairline painted as a `local` background
  on `.nav-scroll`, so it scrolls with the list instead of staying pinned to
  the scroll container. The whole index hangs off it.
- **The active page thickens a 2px segment of that same seam** rather than
  floating a separate pill beside it, so the accent stays structural.
- **There is no transition on it, deliberately.** `renderNav` clears the rail
  and rebuilds every header on each navigation, so `.current` is always present
  at insertion and a freshly inserted element has no previous computed style to
  animate from. A transition declared here could never fire, and the
  reduced-motion block guarding it guarded nothing — `app.css` already has one
  global `prefers-reduced-motion` rule that covers every transition.
- **The visual signal is not the whole signal.** `aria-current="true"` marks the
  current section and `aria-current="page"` the active item, because width and
  gold are both invisible to a screen reader.

## Charts

`static/js/charts.js` is the one Chart.js surface for the dashboard — every
report under **Reports**, plus a handful of panels elsewhere. It shares the
rules above, not a separate style.

**One y-axis, always.** A dual-axis chart — two independent y-scales on one
plot — is the single most common charting mistake: where a line sits relative
to the bars becomes an artefact of autoscaling, not a fact about the data.
Activity used to plot XP on the left axis and Unique Members on the right;
"members tracked XP" or "they diverged" were stories the chart invented, not
things it measured. Members is its own chart now, sharing Activity's x-axis.
`tests/web/test_chart_conventions.py::test_no_chart_uses_a_second_y_axis`
fails the build on a `y1` scale anywhere in the tree.

**The categorical palette is computed, not chosen.** `ROLE_COLORS` in
charts.js is validated against the dark chart surface (`CHART_SURFACE`,
`#2b2d31`) on five checks — lightness band, chroma floor, colour-vision-
deficiency separation under the Machado-Oliveira-Fernandes model, a
normal-vision floor, and contrast — the same method as the categorical UI
palette described above, because it is the same underlying rule: an all-warm
set cannot separate six series, full stop. `tests/web/test_chart_palette.py`
re-runs the checks in CI; its numbers are cross-checked against the reference
validator (`skills/dataviz/scripts/validate_palette.js`) figure for figure.

`CHART_BAR` and `CHART_ACCENT` (the single-series defaults) are `ROLE_COLORS`
members, not a separate hand-picked pair — that split is exactly what let them
drift out of validation once already: they sat at the old, unvalidated gold
and mauve for one full commit, including Activity's own members line, until
an unrelated audit noticed. One palette, checked in one place.

**`seriesColor(i)`, never `ROLE_COLORS[i % ROLE_COLORS.length]`.** A modulo at
the call site recreates the exact cycling bug the shared builders were fixed
to never do — item 7 silently gets item 1's hue. `seriesColor` folds past the
palette's length to `SERIES_OVERFLOW`, a neutral, instead. Found twice in one
fan-out (two different panels, each colouring an unbounded server-supplied
list by hand) and pinned by
`test_chart_conventions.py::test_no_panel_cycles_role_colors_by_hand`.

**A period in progress is drawn to its live edge and marked there.** The bucket
being lived through is a real count of a fraction of an hour, and drawn like
every settled point it reads as a crash — at 15:05 a roaring hour and a dead one
are the same five minutes of data. Do not blank it: that fixes the misreading by
discarding the one thing the reader opened the chart for. Do not project it
either — scaling by the fraction elapsed multiplies five quiet minutes by twelve,
which is a guess wearing the same ink as a measurement. Mark it: the last segment
**dashed to an open ring**, and the caption says so in words. Two encodings,
because a dash is a convention a reader has to be taught, while an unfilled point
at the end of a line reads as "not closed yet" on its own and survives both
colour-vision deficiency and a phone-width render. `liveEdgeProps` in charts.js
applies it from `data.partial_from`; matplotlib's side is `_plot_live_edge` in
`activity_graphs.py`, and the two are deliberately the same picture. A line that
is *already* dashed (an extra series) takes the ring only — a second dash pattern
beside the first says nothing decodable.

**A bar takes the same idea in its own shape: an outline of itself.** There is no
"end of the line" to leave open, so the still-filling bucket keeps its colour as a
2px border and takes the surface as its fill (`liveEdgeBarProps`), which reads as
"not filled in yet" rather than as a short bar. Shading or fading it would not:
a tint is just a slightly different bar at phone width and under CVD. On a stacked
column every segment is outlined in its own source colour — that column trades the
2px surface separator for a full outline, so the palette's required secondary
encoding is kept, not dropped.

**Which buckets are actually partial is a property of the bucketing, not of the
chart type.** On the Activity panel it is the two overlay views and `hour`, whose
`_hour_buckets` snaps to calendar-hour boundaries; `day`/`week`/`month` are rolling
windows that end at *now* and so close their last bucket by construction, and the
two histograms have no latest bucket at all — hour-of-day is a shape, not a
timeline. Ask the bucketing before adding the mark.

**The words come from whether a mark was drawn, never from the index.** An index
can point past the end of the plotted data — the clock rolls between the query and
the render — and a caption derived from the index alone then explains a mark that
is not on the picture. `_plot_live_edge` returns whether it marked; `marksLiveEdge`
is the same question for Chart.js, and both the dataset props and the caption go
through it. The sentences themselves are `PROVISIONAL_CAPTION` /
`PROVISIONAL_BAR_CAPTION` in charts.js and `PROVISIONAL_NOTE` in
`activity_graphs.py` — a panel never hand-rolls its own wording.

**Canvas draws the plot; HTML draws everything you'd want to select, resize,
or have read aloud.** Chart.js's own title and legend are canvas text: they
cannot use the page's type, cannot be selected, and do not exist for a screen
reader. Every builder in charts.js ships with `title: {display:false}` and
`legend: {display:false}` — the caller renders a `.chart-caption` div and,
for two-or-more series, calls one of:

- `renderChartLegend(host, chart)` — multi-*dataset* charts (line, bar,
  stacked bar, candlestick): one entry per `chart.data.datasets[i]`, click to
  toggle, that series' running total shown beside its name.
- `renderPieLegend(host, chart)` — doughnut/pie charts specifically. These are
  structurally different (one dataset, many labels, one colour per slice), so
  `renderChartLegend` would paint a single meaningless "Series 1" entry
  reading the wrong colour field; `renderPieLegend` shows each slice's share
  of the total and toggles via `chart.toggleDataVisibility(i)`.

A single-series chart gets a caption and no legend — "none for one," the
caption already names it. Every chart, single- or multi-series, still gets
`renderChartTable(host, {labels, datasets, indexLabel})`: a "Show the numbers"
disclosure holding every plotted value as a real `<table>`. Not a nicety —
three of the six `ROLE_COLORS` slots sit under 3:1 contrast against the
surface, which the palette test permits only where visible labels or a table
supply the relief.

**Stacked segments and doughnut slices carry a 2px gap**, painted in
`CHART_SURFACE` rather than a border, so two adjacent colours never share a
hard edge. It is the secondary encoding the palette's weakest pairs (ΔE 6–8
under CVD) are validated *with*, not decoration — removing it would silently
invalidate them.

**A per-bar colour is either categorical or semantic, and only one of those
is this file's problem.** `seriesColor(i)` for an unordered list of *things*
(channels, moderators, XP sources). A fixed, small, named ramp — good/warning/
critical — for a *state* a value can be in, reused from already-validated
`ROLE_COLORS` members rather than invented fresh each time a panel needs one
(health-gini's per-channel participation tier does this: wine for the worst
tier, `CHART_BAR` for the middle, teal for the best). Genuinely bad: an
unbounded per-bar rank colouring past the palette's length, and a value-ramp
painted onto nominal categories that aren't ordered at all — both read as
"more entries this table has ever seen" rather than anything about the data,
and both are still present in a few older per-panel colour choices that
predate this pass (see Known-unresolved).

## The user guide is a surface, not an exception

`static/manual.html` is served two ways: standalone at `/static/manual.html`,
and parsed by `help.js` into the Help panel. For a long time it was neither —
it carried a 428-line inline `<style>` block that redefined ~45 selectors
`help-panel.css` already owned, in a light palette of its own built from 31
hardcoded hexes and a private token set (`--brand`, `--muted`, `--surface`).
Two renderings, two definitions, and the two had drifted apart at every
heading level. (A `.matrix` rule existed in one copy and not the other; it
turned out to be dead in both — nothing in `src/` carries the class — and was
deleted rather than shared, which is the more useful lesson about what a
duplicated stylesheet hides.)

It drifted **because it was inline**. stylelint globs `static/**/*.css` and
the token-hygiene and contrast sweeps read `.css` and `.js` — so every guard
built for exactly this failure skipped the one file most likely to hit it.
That is the general lesson: a style block inside an HTML file is outside the
system, whatever it contains.

The split now is:

| sheet | owns | loaded by |
|---|---|---|
| `tokens.css` | the faces and the one `:root` | both surfaces |
| `help-panel.css` | every *content* shape, scoped under `.dk-help` | both surfaces |
| `manual-standalone.css` | page frame, sidebar TOC, cover, print | the standalone page only |

Two things are worth keeping in mind if you touch it.

**The headings are the one honest divergence.** The panel shows one section
under a header that already prints its title, so it hides the `h2`, hides the
section number and demotes `h3` to an eyebrow label. The standalone page is 32
sections in one scroll with no header carrying anything, so it restores
document-scale headings (20/16/14) and the numbers its sidebar refers to.
Those overrides are scoped to `.content`, which only the standalone page has;
`tests/web/test_manual_stylesheet_split.py` fails if one is written unscoped,
and fails if the standalone sheet restyles any other shared shape.

**Print re-declares the tokens rather than overriding elements.** The guide is
dark on screen because it documents a dark tool, and unreadable that way on
paper. `@media print` in `manual-standalone.css` sets `--bg`, `--ink`, `--rule`
and the rest to light values, and the whole document follows — content shapes
in `help-panel.css` included — without a single per-element print rule. It is
the cheapest demonstration that the token layer does what it claims.

## Known-unresolved

- **The collapsed rail cannot identify a page.** Collapsed to 56px the label is
  gone and only the icon remains — but an icon names a *section*, and a section
  holds up to twenty pages, so Moderation collapses to eight identical shields.
  Each item carries a `title` tooltip, which is the current mitigation. No icon
  set can fix this: there are ~176 pages and ten sections. The real fix is for
  the collapsed rail to show *sections* and expand on click, which is a change
  to how navigation behaves and wants a decision, not a patch.
- **A handful of older per-panel colours are unvalidated single semantic
  picks, deliberately left alone.** channels.js's 5-bucket score-distribution
  ramp, health-sentiment's positive/negative bar colouring, retention's
  activity-drop red, and voice-activity's hour-of-day accent all still use
  pre-migration hex literals. Each is a single deliberate colour choice for
  one meaning, not a multi-series set needing CVD separation from its
  neighbours, so they were judged lower-risk than the categorical drift this
  pass did fix (which produced two measurable collisions — see the Charts
  section above). Worth a pass of their own if the dashboard ever gets a
  proper reserved status palette; today each panel picks its own.
- **Dark-only.** There is no `prefers-color-scheme` rule anywhere and a
  light theme is not planned. With the token system in place it would be a
  contained job rather than a rewrite — the guide's print block (see "The user
  guide is a surface, not an exception" above) is a worked example of the shape
  it would take.
