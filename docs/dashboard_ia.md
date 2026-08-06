# Dashboard information architecture

Reference. How the dashboard's sidebar is organized, where a feature's settings
live, and the naming rules new panels follow. The nav itself is defined in one
place — `SECTIONS` in `src/web_server/static/js/app.js` — and the Help nav is
generated from `static/js/panels/help-sections.js`.

## Where settings live

CLAUDE.md's rule is "configuration lives on the web dashboard, not Discord".
That is about the *surface*, and it still holds. Within the dashboard the rule
is narrower, and until 2026-08 it was written nowhere a user could see it:

> **Settings live with the data they produce.** A feature that already has a
> report or a queue keeps its dials at the bottom of that page, one pane. A
> feature with no such page keeps them under **Config**.

Six features are on the first side of that line — their settings pages were
merged into their report/queue panels, with the settings half rendered
read-only for non-admins (`lockUnlessAdmin`), because two nav entries with the
same label were the clearest possible sign the split was noise:

| Feature | Where its settings are |
|---|---|
| XP & Leveling | Reports → Engagement → XP & Leveling |
| Voice | Reports → Engagement → Voice |
| Birthdays | Reports → Member Lists → Birthdays |
| Intake | Reports → Greeter → Intake |
| Rules Watch | Moderation → Rules Watch |
| Policy Tickets | Moderation → Policy Tickets |

Roughly thirty other features keep their settings under **Config**, grouped by
theme (Server, Roles, Members, Moderation & Safety, Channels & Messages, Voice,
AI & Maintenance).

The user-facing statement of the same rule is the **Where a Setting Lives**
subsection of `manual.html` §27 Configuration Reference (help page
`help-config`) — keep the two in step.

## Sections

| Section | Gate | Contents |
|---|---|---|
| Dashboard | everyone | Home, Quick Reference |
| Reports | moderator | Moderation / General / Engagement / Social Graph / Greeter / Bot Usage / Member Lists |
| Moderation | moderator | Queues (todo, jails, tickets, warnings, policy tickets, rules watch, message search, no-contact) + Audit Logs |
| Config | moderator (most pages admin-only, shown locked) | Themed config headings |
| Economy | admin **or** the economy-manager role | Bank ops through Settings |
| Wellness | opted-in members, plus manage-server/admin | Member-facing wellness surface |
| Games | admin **or** the game-host role | Operations / Live Games / Question Banks |
| Social | moderator | Guess Who, Whisper, Pen Pals, Confessions (admin-only) |
| Help | everyone | Generated from `help-sections.js` |
| Dev | admin | Live log, system stats, QA tracker, owner tools |

**Games** was a 23-item flat list until 2026-08; it now has three subgroups:

* **Operations** — Overview & Logs, Scheduling, Global Config, External Tracking.
* **Live Games** — one page of dials per game that runs live in a channel
  (LegitLibs, Risky Rolls, Pressure Cooker, Quickdraw, Hot Potato, Hot Potato
  (Group), Chicken, Musical Chairs, Photo Challenge — which had been a
  top-level section with a single item under the same gate).
* **Question Banks** — the nine prompt banks (WYR, NHIE, Most Likely To,
  Rushmore, Price, Clapback, AMA, FFA, Traditional ToD).

**Social** is new in the same pass. Guess Who, Whisper, Pen Pals and Confessions
are not games — they are ongoing social surfaces a moderator runs, and
Confessions' audit trail already lives under Moderation. They sat in the
host-gated Games section only because that is where they were built, and each
needed an explicit `perms: ["moderator"]` marker purely to stay reachable when
that gate failed. In a moderator-gated section those markers are unnecessary and
are gone. Visibility is otherwise unchanged for every audience, with one
deliberate exception: Confessions (`adminOnly`) now renders for a plain
moderator as the standard locked entry instead of vanishing — the same
treatment Confessions Audit under Moderation has always had. It is not openable,
and every Confessions endpoint is admin-gated server-side.
`tests/web/test_nav_visibility.py` pins the whole table per audience.

## Naming and route ids

* **Route ids are frozen.** Deep links, bookmarks, the `help:` mappings and the
  never-opened list in usage telemetry all key off them. Regroup and relabel
  freely; never rename an id.
* **New route ids are the bare feature name** — `pen-pals`, `role-menus`,
  `chat-revive`, `photo-challenge` — with no `config-`/`games-`/`mod-` prefix.
  The prefixes on older ids are historical (they encoded the section a page
  happened to sit in, which is exactly the thing that moves).
* **Labels name the thing, not its implementation.** "AI Models" rather than
  "AI (Local LLM)", which only disambiguated it from the adjacent "AI Assistant"
  for someone who already knew the architecture. Old names live on in
  `keywords`, so the sidebar filter still finds a page by what it used to be
  called.
* **No hardcoded branding in a label.** The assistant's name is per-guild
  (Config → Branding). The Help nav entry carries `brand: "assistant"` and is
  re-labelled from `/api/help/advisor/name` at boot and on every guild switch;
  the static label is only the fallback, and it must keep matching the manual's
  own `ask-guide` heading so the help panel still de-duplicates the title.

## Panel-local URL state

Route convention: `#/<page-id>?key=val&…`. Panel state that a user would expect
a refresh (or a pasted link) to preserve — tab, filter, search, selected row —
belongs in the query part, written with `syncHash()` from `report-helpers.js`
(`history.replaceState`, so the panel is never remounted for its own updates)
and read back from the `initialParams` argument to `mount()`.

Every enumerated param is validated against its own value list on read, and id
params are parsed as numbers that simply match no row when they're junk — a
stale or hand-edited URL must fall back to the default view, never error.

Adopted by the sixteen analytics panels and by all five mod workflow panels
(tickets, jails, rules-watch, qa-tracker, todo). Tests:
`tests/web/test_mod_queue_deeplinks.py` (round trip in a browser) and the
`syncHash`-id sweep in `tests/web/test_frontend_wiring.py`.

## Command palette (Ctrl/Cmd+K)

Additive to the sidebar filter, which is unchanged. `Ctrl`/`Cmd`+`K` opens an
overlay (`app.js`, "Command palette") that searches:

1. **Pages** — over `ALL_PAGES`, which `rebuildIndex()` has already narrowed to
   what this viewer may open, so the palette can never surface a page the nav
   wouldn't. Results are a flat ranked list showing the label over its section,
   ranked label-prefix → label-substring → keyword-only.
2. **The guide** — headings from `manual.html`, via `manualHeadings()` exported
   from `panels/help.js`. Loaded lazily on the first query and imported with the
   router's own `?v=` specifier so it shares one module instance and one parse
   of the manual with the help panel.

Combobox semantics: focus stays in the input, options are referenced by
`aria-activedescendant`, arrows move the selection, `Enter` opens, `Esc` closes
and returns focus to wherever it came from. Tests live in
`tests/web/test_nav_visibility.py`.
