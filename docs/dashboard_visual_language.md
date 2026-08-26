# Dashboard visual language (Reference)

What the dashboard is made of, visually, and the rules that keep 141 panels
looking like one tool. `dashboard_ia.md` owns *where a page lives*; this owns
*what it looks like once you're on it*. Everything here is in
`src/web_server/static/app.css`.

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

## Buttons: one filled primary, and never a destructive one

`.act-btn` is the shared button, used by Tickets, Jails, Todo, Announcements,
Docs and Role Menus. It has two meaningful variants:

- **`.ghost`** — transparent, outlined, dimmed. By far the most-used variant
  (13 call sites against two each for the others); it was for a while declared
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

Where a pane's actions operate on more than one object, group them and say
which is which — Tickets splits **This ticket** (state) from **This member**
(a permanent moderation record), using the existing `.section-label`. A member's
display name inside such a label wears `.td-act-who`, which cancels the
uppercase: an eyebrow is uppercase, a name someone chose is not.

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

The sidebar is the one surface on all 141 panels, so it carries the whole
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

## Known-unresolved

- **The collapsed rail cannot identify a page.** Collapsed to 56px the label is
  gone and only the icon remains — but an icon names a *section*, and a section
  holds up to twenty pages, so Moderation collapses to eight identical shields.
  Each item carries a `title` tooltip, which is the current mitigation. No icon
  set can fix this: there are ~176 pages and ten sections. The real fix is for
  the collapsed rail to show *sections* and expand on click, which is a change
  to how navigation behaves and wants a decision, not a patch.
- **Charts are untouched.** The Reports section renders through Chart.js
  (`static/js/charts.js`) and its palette, grid, axis and tooltip styling
  predate this pass.
- **Dark-only.** There is no `prefers-color-scheme` rule in `app.css` and a
  light theme is not planned. With the token system in place it would be a
  contained job rather than a rewrite.
