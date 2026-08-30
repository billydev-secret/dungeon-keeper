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

One feature remains on the first side of that line: **Policy Tickets**, whose
settings half is a single field (the voting deadline) that never justified a
page of its own — it renders read-only for non-admins (`lockUnlessAdmin`) at
the bottom of the proposal queue, Moderation → Policy Tickets.

The other five merged pages (XP & Leveling, Voice, Birthdays, Intake, Rules
Watch) were **split back apart 2026-08-29** by owner decision — see
`docs/plans/dashboard-config-ia.md` for the audit and rationale. Each split
revived the settings page's pre-merge id (`config-xp`, `config-voice-master`,
`config-birthday`, `config-intake`, `config-rules-watch`; the 2026-07-28
merges had deleted those ids with no MOVED_PAGES redirect, killing their deep
links), filed the settings under Config, and left the report/queue half on
the frozen merged-page id in its original section. The halves cross-link via
`related:` chips, and `lockUnlessAdmin` stays in the settings modules as
defense in depth. The accepted cost: moderators no longer get the read-only
settings view the merges provided.

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
| Economy | admin **or** the economy-manager role | Operations / Earning / Spending / Wagering |
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

**Economy** was the last flat list, twelve items deep; it gained four subgroups
in 2026-08 (IA2):

* **Operations** — Bank (relabelled from "Operations"), Statistics, Settings.
* **Earning** — Income Sources, Quests, Claims, Mention Awards, QOTD.
* **Spending** — Approvals, Shop & Perks (relabelled from "Sinks"), Pricing.
  One page until 2026-08-26; see below.
* **Wagering** — Casino, Pools.

**Spending was one 1,339-line page and is now three**, split by what each part
*is* rather than by what data it touches:

| Page | id | Kind | Gate | Cadence |
|---|---|---|---|---|
| Approvals | `shop-approvals` | work queues — emoji submissions, custom-item orders | **not** adminOnly | whenever something is bought |
| Shop & Perks | `economy-sinks` | what's on sale (per-perk switches) + catalogs — palette, role icons, custom items | adminOnly | setup burst, then dormant |
| Pricing | `pricing` | one form of priced dials | adminOnly | launch, then a few times a year |

`economy-sinks` keeps its id because the page still exists, so no `MOVED_PAGES`
entry is owed — that mechanism is for **retired** ids. The `order` field states
the frequency deliberately: the queue you work is first and the prices you
rarely touch are last, rather than leaving it to an alphabetical accident that a
future relabel would silently reshuffle.

Three things forced the split, and none of them were the page's height:

1. **Approvals must not be adminOnly.** `/api/economy/emoji-submissions` is
   gated `require_economy_manager`, so the backend grants the economy-manager
   role access — but the queue lived on an adminOnly page, so a manager could
   never reach it. Claims and QOTD, the comparable queues, were already open.
   Loosening the settings page's gate instead would have handed managers the
   hoard-tax dial.
2. **One Save governed six unrelated groups.** All six dial cards sat in a single
   `<form>`, so saving a raffle price also wrote the tax rate. The form is now
   the whole of `pricing`.
3. **A shared dirty bit.** `guardForm`'s unsaved-edits flag is a module global in
   `config-helpers.js` and `showStatus(el, true, …)` clears it. The old page had
   41 `showStatus` call sites, so approving an emoji — or merely *starting* an
   image upload — cleared the warning protecting a half-typed price.

The precedent that settles the "a price belongs beside its queue" objection is
already in the tree: sponsored QOTD's price and its submission queue have been
on separate pages all along.

Cross-page hints are the maintenance cost. A palette row priced 0 falls through
to the flat dial on `pricing`, and the icon catalog overrides another; those
hints used to say "above" and "further down" and now have to be links. When
moving a dial away from the thing it prices, grep the hint text.

Two rules are at work and both are load-bearing. Headings are **the job** — run
it, pay it out, spend it, wager it. Inside a heading a **feature that spans
pages stays whole**: Claims is the quest sign-off queue, so it sits under
Quests rather than in a queues-only group that reads tidy and then sends
someone hunting in the wrong place.

QOTD went further and **merged**. Its settings page (`economy-qotd`, adminOnly)
owned a single role id in 88 lines and never earned a nav slot; it is now the
top card of the page that already held the sponsored queue, which keeps the
whole feature on one screen and takes Earning from six entries to five. The two
audiences are preserved rather than merged: the settings card renders only when
the admin-gated config GET succeeds, the Income Sources probe pattern, so a
manager-role holder still sees exactly the queue they saw before. The retired id
redirects through `MOVED_PAGES`. This worked because QOTD was genuinely a
one-knob page — Quests + Claims was considered and rejected, since burying a
daily sign-off queue behind a tab on an 800-line authoring page costs more than
the sidebar line it saves.

Two labels changed with the regroup, ids untouched and both old names kept as
search keywords. "Operations" collided with the heading above it, and **Bank**
is what the feature is called everywhere else (`/bank`, the bank channel).
"Sinks" was economics jargon on the page holding everything a member can spend
on, and it has to read as a sibling of the shop pages arriving next to it.

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
