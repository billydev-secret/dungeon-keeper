# Design guide — building a feature in Dungeon Keeper (Reference)

The entry point. Everything a feature has to get right is written down
somewhere in this repo; nothing tells you *the order you hit those decisions*
or *what must be true before you commit*. That is what this document is.

## How to read it

**This guide owns the spine, not the detail.** Almost every line here is a
rule plus a pointer to the document that actually owns it:

- `→ doc § Section` — the rule lives there. That document is authoritative.
  If it and this guide disagree, **it wins** and the pointer here is a bug
  worth fixing. Pointers are written as `doc § Section` because that is
  exactly `get_doc(path, section=…)` over the MCP.
- `⌂` — no other document owns this, so this guide does. There are only a
  handful; keep it that way. A new cross-cutting rule belongs in an owner
  doc with a one-line pointer here, not in this file's body.

**This guide and CLAUDE.md are not summaries of each other.** CLAUDE.md is
loaded into every session automatically, so it pays a token cost on every
turn and states each rule as tersely as it can. This is a document you open
on purpose, so it can afford the *why* and the *order*. When a rule belongs
in both, CLAUDE.md states it and this explains it.

**When in doubt, the code wins.** `docs/INDEX.md` classifies every spec as
Reference, Design or Aspirational; a Design spec may lag the code and an
Aspirational one describes things nobody built. → `INDEX.md`

---

# Part 1 — The decision sequence

Five decisions, in the order they actually come up. Getting one wrong early
is expensive; getting it wrong at step 4 ships a safety hole.

## 1. Where does the surface live?

**Admin and server configuration lives on the web dashboard.** Every
feature's settings get an admin-gated panel in `src/web_server/`, filed under
the right nav heading. Not a slash command, not a modal, not a button flow.
→ CLAUDE.md § Design philosophy

**Discord is for member self-service and mod actions** — playing a game,
opting in, customizing your own perks, a mod running QOTD.

Why the line is drawn there: Discord command surfaces sprawl and can't be
discovered, versioned or gated the way a page can, and a knob that only one
admin can find is a knob nobody tunes. A feature that shipped
command-managed gets its knobs moved to the web and **the commands deleted** —
that migration is the expected follow-up, not optional cleanup.

If a feature genuinely seems to need in-Discord admin config, **raise it and
ask** rather than building it.

## 2. If it's a dashboard panel — which page, and which id?

**Settings live with the data they produce.** A feature that already has a
report or a queue keeps its dials at the bottom of that page, one pane, with
the settings half read-only for non-admins (`lockUnlessAdmin`). A feature with
no such page keeps them under **Config**.
→ `dashboard_ia.md` § Where settings live

**The route id is frozen the moment it ships.** Deep links, bookmarks, the
nav `help:` mappings and usage telemetry all key off it. New ids are the bare
feature name — `pen-pals`, `role-menus` — with no `config-`/`games-`/`mod-`
prefix, because a prefix encodes the section, which is the thing that moves.
Regroup and relabel freely; never rename an id.
→ `dashboard_ia.md` § Naming and route ids

Panel-local state goes in the URL as `#/<page-id>?key=val`, so a filtered view
is a link someone can send.
→ `dashboard_ia.md` § Panel-local URL state

## 3. If it's a Discord surface — what shape?

**One ephemeral panel with buttons and modals beats a sprawl of subcommands.**
A member should reach everything a feature offers them from one message.

**Collapse controls.** One dial with a few states beats several overlapping
toggles — Voice Control's access dial is the reference implementation. If a
config page feels jumbled, reorganize it rather than appending to it.
→ CLAUDE.md § Design philosophy

Command naming, description style, button and modal copy, select placeholders:
→ `embed_style_guide.md` § Slash commands, § Buttons, modals & selects

A view that must survive a bot restart needs a stable static `custom_id`,
`timeout=None`, and re-registration at cog load — and its callback must
degrade to an ephemeral note rather than becoming a dead button.
→ `embed_style_guide.md` § Persistent views

## 4. Who does it put in contact, and who can see it?

The safety step. Three gates, and one rule that governs all of them.

**Never ship a preference or toggle that isn't enforced.** A setting the code
doesn't read is worse than no setting: it is a promise to a member that the
bot silently breaks. A passing test *is* that enforcement — see Part 3.
→ CLAUDE.md § Design philosophy

**NSFW gates on `channel.is_nsfw()`** — Discord's own age-gate — never a
bot-side toggle an admin can flip on a channel Discord doesn't consider adult.
→ CLAUDE.md § Design philosophy, `nsfw_classifier_spec.md`

**Sensitive access is opt-in, and reach is the narrowest that works.** Member
self-service replies are ephemeral by default; go public only for genuinely
shared state. Recurring economy DMs gate on the opt-in game role
(`notify_member(require_game_role=True)`).
→ `embed_style_guide.md` § Reach & privacy

**Any surface that puts two members in contact must consult the no-contact
list.** Whispers, directed questions, anonymous replies, matching passes, voice
room permissions — anything where one member's action reaches a specific other
one. The surfaces gated today are tabulated in the spec; a new member-to-member
surface joins that table or it is a hole. Read the list through
`is_no_contact_conn` / `no_contact_partners_conn` rather than querying
`no_contact_pairs`, so the table's shape stays owned by one module.

Two things about this gate are easy to get wrong and both matter:

- **The refusal must be indistinguishable** from an ordinary success or an
  ordinary failure. The blocked party must never be able to infer that a
  no-contact entry exists — a gate that leaks is worse than no gate, because
  it tells someone they were named.
- **Enforcement and logging are separate questions.** Surfaces gated inside a
  matching loop or a permission sync record no attempt event, because there is
  no single moment there that means "he tried". They are enforced just as
  strictly.

→ `no_contact_spec.md` § Gated surfaces, § The disclosure rules

## 5. What data does it store?

**Store the minimum and derive at ingest.** Message content is off by
default, so compute the metadata you need when the message arrives rather
than keeping the text to compute it later. Several features exist only
because their metadata-plus-pointer shape made the content unnecessary.
→ CLAUDE.md § Design philosophy

**A new table holding per-user data needs a `data_register.md` row in the same
commit**, and the row must carry an explicit decision: does `purge_user_data`
clear it, or is it preserved — and if preserved, on what Art 17(3) ground, not
just the engineering reason. The register is the record of processing
activities; an unregistered table is invisible to an access or erasure
request, which is a compliance failure, not a docs failure.
→ `data_register.md`, `gdpr_runbook.md`

**If the column naming the member isn't one of the conventional names in
`privacy_service.SUBJECT_ID_COLUMNS`, add it there too** — otherwise the
subject-access export cannot see the table.
→ `privacy_spec.md` § Subject access export

**Member-facing collection also needs a line in the privacy notice** —
`manual.html` § Your Data & Privacy.

⌂ **Migrations are numbered `NNN_name.sql` in `src/migrations/`.** Take the
next free number, and re-check it against `main` before you merge — parallel
worktrees pick the same number constantly, and two files sharing one is a
merge conflict that only shows up when the chain runs.

---

# Part 2 — Coding standards

Two layouts are live and both are fine: a per-feature package
(`bot_modules/survivor/` with `logic.py`, `embeds.py`, `views.py`;
`bot_modules/music_playlist/` with the same split under prefixed names), and
the older flat pair — `bot_modules/services/<feature>_service.py` alongside
`bot_modules/cogs/<feature>_cog.py`. **The directory is not the point; the
layer boundary is.** Pick whichever fits the feature's size and match the
naming the gate looks for below.

## Layering

**Behavior goes in the logic/service layer. Cogs, views and routes are glue.**
A cog resolves Discord objects, calls one function, and renders the result.
This is a design decision made at build time, not a refactor you do when the
tests get awkward — it is what makes the feature testable offline at all, and
it is why the test rule in Part 3 can say "don't test through Discord mocks".
→ CLAUDE.md § Regression tests

The gate enforces the floor: a **new** `logic.py` / `store.py` / `service.py` /
`*_logic.py` / `*_service.py` with no mapped test is a hard commit failure.

## Bot-side code

- **Embeds are built by pure `build_*` functions in a per-feature `embeds.py`**
  — plain dicts and primitives in, `discord.Embed` out, no Discord or network
  calls. Name lookups arrive as a resolver callable. A cog building eight
  embeds inline is the anti-pattern.
  → `embed_style_guide.md` § Builder conventions
- **Color comes in as a parameter**, resolved by the caller via
  `safe_resolve_accent(source, guild)` (`core/branding`) — never
  `resolve_accent_color` directly, which raises and is a hard test failure.
  `source` is whatever the caller holds: a bot, an AppContext, or a db_path.
  A builder never resolves the accent itself. Keep a hard-coded red/green only
  where the color is *semantic*.
  → `embed_style_guide.md` § Color
- **DMs go through `services/dm_branding.py`** — `send_branded_dm` for the
  common case, `brand_dm_embed` for callers that own their own delivery
  policy. Four near-identical `_try_dm` helpers existed before it did; there
  should not be a fifth.
  → `embed_style_guide.md` § DM branding
- **Escape member-supplied text and pin your mentions.**
  `discord.utils.escape_markdown` before it goes in an embed,
  `escape_mentions` in any `content=` that isn't allow-listed, and always set
  `allowed_mentions` explicitly — default `AllowedMentions.none()`, and when
  you do want a ping, allow-list exactly the role or user intended.
  → `embed_style_guide.md` § Mentions, pings & user-supplied text

- **Naming a member in an embed? Resolve it — `<@id>` renders as a number.**
  An embed mention is resolved by the *reading* client from its own cache, so
  anyone who hasn't seen that user gets bare digits. Use
  `services/name_resolver.build_name_fn` (member cache → `known_users` →
  `<@id>`, markdown-escaped), have builders take a `name_fn`, and guard the
  wiring with a test. Content mentions are fine; a no-contact pair degrades to
  a plain `User <id>` on purpose.
  → `embed_style_guide.md` § Naming members in embeds

- **Hold a message id? Link the message, not the channel.** A channel-only
  link lands the reader at the bottom of the channel; build permalinks with
  `core/utils.jump_url`, never a hand-rolled URL. Link the channel only when
  the room itself is the subject.
  → `embed_style_guide.md` § Pointing at things

## Dashboard-side code

The shared widgets are **safe by default**, and the safety only holds if you
use them:

- **`table.js` escapes every cell.** A column opts into markup with
  `html: true` and then owns its own escaping — that opt-in is the whole
  security boundary.
- **A picker never silently drops the value it was given.** Assigning a
  `<select>` a value it has no `<option>` for selects *nothing* — the control
  renders blank, or falls to whatever sits first (usually "(none)"), and is
  then indistinguishable from one nobody ever set. Config panels save the whole
  form at once, so the next save of an unrelated field writes that blank over a
  real setting. Two sources put a value outside the option list: an id whose
  role/channel was **deleted in Discord**, and a stored number outside the
  presets a panel happens to offer. Build role/channel/category options with
  the `config-helpers.js` builders (`roleSelect`, `channelSelect`, the `Multi`
  variants) or the `mount*Picker` wrappers, which keep an unmatched id selected
  and label it `⚠ Missing role (id …)`; where a value is assigned *after* the
  options are built — a mode/resolution change, or an edit form populating
  itself — use `selectValueOrAdd`. `tests/web/test_shared_js_safety.py` holds
  the invariant, and `tests/web/test_activity_compare_picker.py` the
  rebuild-on-mode-change case.
- **Config panels mount through `mountAsync`**, so a failed first fetch
  renders an error with a retry instead of a permanent spinner. **Let the
  rejection reach it.** Five panels wrapped their loader's own fetch in a
  try/catch that rendered a bare error and returned *normally*, so
  `renderFailure` — the error *plus a working Try again button* — could never
  run, and the `errorMsg` they declared was dead code. `wellness-caps.js` is
  the model for a panel that also refreshes: rethrow on first load, render in
  place afterwards.
- **A report panel that refreshes uses `mountReloadable`** (`report-helpers.js`),
  which puts the catch on *every* pass. Seven health panels guarded only the
  first load, so a failed refetch left the previous figures up under a Show
  Bots toggle that had already flipped — numbers that look like an answer and
  are not.
- **Unsaved-edit tracking is per guarded form**, not per page. `guardForm`
  registers the container and `showStatus` clears only the form its status
  element sits in. `isFormDirty(form)` asks the same registry, which is how a
  panel that rebuilds itself after an unrelated action decides whether it may
  — mahjong remounts after a card upload and would otherwise throw away
  half-typed House Rules. **Do not report a held rebuild through
  `showStatus(el, true, …)`**: if that element sits inside the guarded form —
  mahjong's `[data-status]` does — it clears the very dirt that held the
  rebuild, so the next action goes through and takes the edits with it. Use a
  toast.
- **An optimistic write owns its rollback.** A list mutated and re-rendered
  before the PUT has to be put back when the PUT fails, or the screen shows
  state the server never accepted. Pen Pals' separations did not, and that is
  the keep-them-apart list. Never write a failure message into an empty-state
  element either: shop-approvals did, and one failed fetch replaced the real
  empty copy for the rest of the session. It used to be one module-global boolean that any success
  cleared, so on the fourteen panels guarding two to four forms, saving one —
  or any unrelated action reporting success — silently disarmed the warning
  protecting half-typed values in the rest.
- **A new guild-scoped cache in `config-helpers.js` must be cleared in
  `resetMetaCaches()`** — a test hard-fails if it isn't, because a stale cache
  survives a guild switch and shows one server's members inside another.
- **Snowflake ids cross the boundary as JSON strings both ways.** Never
  `parseInt` an id: a bare number above 2^53 is silently rounded, and the
  sweep in `web_testing.md` will catch it after you've already shipped the bug.
- **Destructive confirms go through `confirmDialog`** — question plus one
  consequence sentence, `danger` styling — never native `confirm()`.
  → `embed_style_guide.md` § Dashboard (JS) specifics, `web_testing.md`

**Styling picks a step off the scale; it does not invent a value.** Sizes come
from `--t-1..--t-6`, spacing from `--s-0..--s-7`, and the rule for a config
page is compact rows, airy sections. The palette is Discord's on purpose, two
of its contrast decisions are load-bearing, and Archivo's width axis is what
the nav rail uses to mark the section you're in — so it is not swappable for a
static face. → `dashboard_visual_language.md`

**Run the JS gates before pushing.** Node 20 is installed user-local purely so
the blocking CI lint job can be reproduced: `npm install --no-save` once per
worktree, then `npx eslint src/web_server/static/js` and
`npx stylelint "src/web_server/static/**/*.css"`. Static-asset cache-busting
is automatic per boot, so JS edits appear after the next restart, not before.
→ CLAUDE.md § Conventions

## Copy

Structure rules are global; **voice** splits by surface — games may be playful
("Start a Would You Rather game!"), while moderation, economy and admin stay
measured. Titles and labels are Title Case; prose is prose; denials carry `❌`;
it is **"server", never "guild"** in member-facing copy; currency and perk
vocabulary route through the guild's own settings rather than a hard-coded
"coins".
→ `embed_style_guide.md` § Voice & terminology, § Titles, labels & casing

---

# Part 3 — What tests ship with it

**Same commit as the feature. Every time.** This is the standard, not a
coverage percentage:

- The **happy path**.
- **Every guard and branch**, especially the safety gates from Part 1 §4 —
  NSFW, opt-in, role gates, no-contact. A passing test *is* the enforcement
  CLAUDE.md's safety rule demands; without one you have a comment, not a gate.
- For a bug fix, **a test that fails before the fix**. Write it first and
  watch it fail — a test written after a green fix proves nothing about the
  bug.
- **A bug seen in Discord still gets its test at the logic layer.** Reproduce
  the state that broke, not the surface it showed up on. At most one wiring
  assertion in the cog test, and only when the glue itself was wrong.

**Prefer a `pytest.param` row over a new test function** when you're covering
another value variant, and check whether a shared contract table already
covers it — embed accents live in `tests/test_embed_accent_contract.py` and
take one `case()` row per new builder, never a per-file copy.

Dashboard work gets some coverage for free: a new route joins the
authorization sweep automatically (add it to `PUBLIC_PATHS` only if it is
genuinely public), and the browser suite checks every panel for layout
breakage and console errors on mount. Add an interaction scenario when the
layout only appears behind a click.
→ `web_testing.md` § Adding a route? Two freebies, CLAUDE.md § Regression tests

---

# Part 4 — Before you commit

⌂ The assembled obligations. Nothing here is new; the point is that it is in
one place, in the order the pre-commit hook and the reviewer will hit it.

## The docs contract

Same commit, not a follow-up.

- [ ] Behavior changed ⇒ the matching spec in `docs/` is updated, and its
      `INDEX.md` classification too if the flavor changed.
- [ ] UI or UX changed (new/changed slash command, dashboard panel, embed
      copy, button or modal flow) ⇒ `src/web_server/static/manual.html` is
      updated. It is a different surface from `docs/` and drifts on its own.
- [ ] New per-user table ⇒ a `data_register.md` row with the purge-or-preserve
      decision and, if preserved, the Art 17(3) ground.
- [ ] Subject column not in `privacy_service.SUBJECT_ID_COLUMNS` ⇒ added.
- [ ] New member-facing collection ⇒ a line in `manual.html` § Your Data &
      Privacy.
- [ ] New doc ⇒ a row in `docs/INDEX.md` with its classification.
- [ ] README only if the change alters what the bot *is* — a feature area
      appearing or disappearing. Not for a command being added or renamed.

## Enforcement

- [ ] Every gate from Part 1 §4 has a test that proves it denies.
- [ ] Member-to-member surface ⇒ no-contact consulted, and the refusal is
      indistinguishable from an ordinary outcome.
- [ ] No preference or toggle that nothing reads.
- [ ] New logic-layer file ⇒ a mapped test file (the hook hard-fails otherwise).

## Gates

- [ ] `python scripts/gate.py --scoped` — runs automatically in the pre-commit
      hook (ruff, then the tests mapped to the staged diff). Neither heavy
      check runs there: **pyright and the browser panel sweep are CI's**, on
      every push/PR, because neither can be scoped to a diff and parallel
      sessions ran one copy each. `--quick` runs both locally before a push;
      `--pyright` / `--browser` force one into any run. In a
      session worktree a shared-file edit no longer fans out to the whole suite;
      the gate names the paths whose full run it **deferred** to main.
- [ ] `python scripts/gate.py` on **main**, once a batch of merges is complete —
      this is where the deferred runs are paid, and where a clean merge between
      two parallel sessions is caught leaving main red. One run covers every
      branch merged since the last one; it is not a per-ship step. A green run
      moves the local `last-full-gate` tag; teardown prints how many commits
      main is past it, so a skipped run is visible rather than remembered.
- [ ] Dashboard assets touched ⇒ `npx eslint` and `npx stylelint`, the exact
      commands the blocking CI job runs.
- [ ] Full suite green before a **push to origin** — CI on that push satisfies
      it. Run it locally only solo; a parallel full run can exhaust the tmpfs
      quota and spray bogus sqlite errors.
- [ ] Coverage floor in `pyproject.toml` not lowered.

## The commit itself

- [ ] Subject is `Scope: descriptive summary`, ~60 characters.
- [ ] Body is prose: why, edge cases handled, what the tests cover.
- [ ] No `Co-Authored-By` / `Claude-Session` trailers.
- [ ] **User-facing** ⇒ the body ends with a `Testing:` section of `- [ ]`
      lines. Not every behaviour change earns one: it has to be something a
      member or admin can see in Discord or on the dashboard, so an internal
      refactor, a dep bump, a docs or test-only commit gets none.
- [ ] Those lines are written for a **volunteer tester, not a developer** —
      they *are* the QA card. One action and one observable result per box,
      naming what the tester clicks and sees rather than what the code calls
      it, each verifiable in one sitting. The card itself is assembled from
      every `Testing:` section the branch merged, once, at `/dk-ship` teardown.

→ CLAUDE.md § Docs, § Gates, § Commits

---

# Part 5 — Where the rules live

The eight documents this guide points at, and what each owns.

| Document | Owns |
|---|---|
| `CLAUDE.md` | The working agreement: where config lives, the docs contract, test discipline, the gates, commit shape. Loaded into every session automatically |
| `docs/INDEX.md` | Every spec's classification — Reference / Design / Aspirational — and the rule that the code wins |
| `docs/embed_style_guide.md` | Embeds, panels and all user-facing copy: color, card anatomy, casing, errors, voice, commands, buttons, DM branding, escaping, dashboard JS copy |
| `docs/dashboard_ia.md` | Dashboard nav taxonomy, where a feature's settings live, the frozen route ids, panel URL state |
| `docs/data_register.md` | The Art 30 record of processing: every table holding personal data, its retention, purge coverage and Art 17(3) grounds |
| `docs/privacy_spec.md` | Deletion and subject-access export, and `SUBJECT_ID_COLUMNS` |
| `docs/web_testing.md` | The dashboard sweeps and the Playwright browser suite |
| `docs/no_contact_spec.md` | The no-contact list: gated surfaces, disclosure rules, storage |

Plus the two surfaces that are not dev docs at all:
`src/web_server/static/manual.html` is the user-facing guide rendered in the
dashboard's Help panel, and `README.md` is a landing page for someone
evaluating the bot.

---

# Part 6 — When this guide is wrong

- **Code beats specs.** If a spec and `src/` disagree, the code is what runs.
- **The owner doc beats this guide.** Every `→` line is a summary; the target
  is authoritative. A disagreement is a pointer to repair, not a choice.
- **A cross-cutting rule with no home goes in an owner doc**, and gets one
  line here. Writing the rule itself into this file makes it a ninth place
  rules live, which is the problem this document exists to solve.
- The machine view of this guide is `get_conventions()` on the DK MCP server,
  which serves these sections to claude.ai. It reads this file live, so there
  is nothing to keep in sync — but its topics address this guide's headings by
  name, so **renaming a heading here is a code change**; a test will tell you.
  → `dk_mcp_server.md`
