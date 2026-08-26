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

Older per-panel CSS still holds off-scale values; those are migrated when the
panel is next touched, not swept in bulk. New work picks a step.

## The signature: the rail is an index

The sidebar is the one surface on all 141 panels, so it carries the whole
visual identity and everything else stays quiet.

- **`.nav-group.current` sets wider** — `wdth` 125 against 85 for the other
  nine sections. This is the "you are here" signal. It reads in peripheral
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
- The width transition is the only motion the restyle added, and
  `prefers-reduced-motion` drops it — the active section is simply wide
  immediately.

## Known-unresolved

- **The ten section glyphs** (`⌂ ▤ ⚖ ⚙ ¤ ♥ ⚄ ☺ ⚒ ?`) come from different
  Unicode blocks and were drawn by different people for different purposes;
  they will never look like a set, and they render at different weights per
  platform. They also fall outside the latin/latin-ext subsets, so they render
  in a system fallback rather than Archivo. Replacing them with a drawn icon
  set is an open decision, not an oversight.
- **Charts are untouched.** The Reports section renders through Chart.js
  (`static/js/charts.js`) and its palette, grid, axis and tooltip styling
  predate this pass.
- **Dark-only.** There is no `prefers-color-scheme` rule in `app.css` and a
  light theme is not planned. With the token system in place it would be a
  contained job rather than a rewrite.
