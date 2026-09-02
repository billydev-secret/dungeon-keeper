# Dungeon Keeper — working agreement

Discord bot (`src/bot_modules/`: thin cogs, logic in per-feature modules) +
FastAPI dashboard (`src/web_server/`: routes + vanilla-JS panels in `static/js/`),
SQLite-backed. Tests in `tests/`.

## Design philosophy

- **Configuration lives on the web dashboard, not Discord.** Every feature's
  admin/server settings get an admin-gated panel in `src/web_server/`, filed
  under the right nav heading. Don't build slash commands, modals, or button
  flows for admin config; if a feature shipped command-managed, moving its
  knobs to the web and **deleting** the commands is the expected follow-up —
  keep the command surface clean.
- **Discord is for member self-service and mod actions** (playing games,
  opting in, customizing your own perks, a mod running QOTD). Prefer one
  ephemeral panel with buttons/modals over a sprawl of subcommands.
- **Collapse controls.** One dial with a few states beats several overlapping
  toggles (see Voice Control's access dial). Consistent button shapes/sizes;
  if a config page feels jumbled, reorganize it rather than appending.
- **Safety & privacy defaults:** NSFW gates on `channel.is_nsfw()` (Discord's
  own age-gate), never a bot-side toggle. Store minimal data — message
  content is off by default, so derive metadata at ingest time. Sensitive
  access is opt-in. Never ship a preference or toggle that isn't enforced.
- **Any surface that puts two members in contact consults the no-contact
  list** (`is_no_contact_conn` / `no_contact_partners_conn`), and refuses in a
  way the blocked party can't distinguish from an ordinary outcome. See
  `docs/no_contact_spec.md`.
- If a feature genuinely seems to need in-Discord admin config, raise it and
  ask instead of building it.

## Docs

- **`docs/design_guide.md` is the entry point** — the order the design
  decisions come in, the coding standards for the bot and dashboard layers,
  and the assembled pre-commit obligations checklist. It restates nothing:
  every rule there points at the document that owns it. Read it when building
  a feature; this file is the terse in-context statement of the same rules.
- Specs live in `docs/`. Read `docs/INDEX.md` **first** — it classifies every
  spec as Reference / Design / Aspirational. Aspirational specs describe
  unbuilt features; when a spec and the code disagree, the code wins.
- Behavior change ⇒ update the matching spec (and its INDEX.md classification
  if it changed flavor) **in the same commit**.
- UI/UX change (new/changed slash command, dashboard panel, embed copy,
  button/modal flow) ⇒ also update the **user-facing website docs** in the
  same commit: `src/web_server/static/manual.html` (the guide rendered in
  the dashboard's own Help panel — routed via
  `static/js/panels/help-sections.js`/`help.js`). This is a different surface
  from `docs/` (dev specs) and drifts independently — don't let it lag while
  `docs/` stays current.
- **New table holding per-user data ⇒ a row in `docs/data_register.md`** in the
  same commit, with an explicit decision: does `purge_user_data` clear it, or is
  it preserved — and if preserved, on what Art 17(3) ground (not just the
  engineering reason). The register is the record of processing activities, and
  a new personal-data store that isn't in it is invisible to an access or
  erasure request. If the column naming the member isn't one of the conventional
  names in `privacy_service.SUBJECT_ID_COLUMNS`, add it there too or the export
  can't see the table. Member-facing data collection also needs a line in the
  privacy notice (`manual.html` §Your Data & Privacy).
  `tests/test_privacy_register_coverage.py` **hard-fails** when a table with a
  subject-id column has no register row, so this is a gate, not an honour
  system. It cannot see a table whose member column is named unconventionally —
  run `scripts/privacy_coverage.py` against prod (read-only) for that; it finds
  member ids by value, not by column name.
- README.md is **not** part of that per-commit contract. It is a landing page
  for someone evaluating the bot, and its command table is a hand-picked
  highlights list, not a reference — `/help` and manual.html are the reference.
  Touch it only when a change alters what the bot *is* (a whole new feature
  area, or one removed), not when a command is added, renamed, or resignatured.

## Workflow

- Do edits in a git worktree; merge back to main when ready for user testing.
  `/dk-feature [model] <name>` creates one — worktree, branch, tmux window, and a
  `claude` running in it, all sharing one name — and `/dk-ship` reviews it (a
  `/code-review --fix` pass, on by default), merges it and tears it down. See `docs/dev_sessions.md`.
- This checkout **is production**. Never restart the bot or dashboard
  (`sudo systemctl restart dungeon-keeper`) unasked — code changes apply on
  restart, and the user pushes that button.
- Large tasks (multi-stage refactors or big features) get a plan doc in
  `docs/plans/`; commits reference their stage.
- When touching a module with open findings in `docs/reviews/`, mention them
  and offer to fold fixes in — don't expand scope uninvited.

## Regression tests (ship with the feature, not after)

- **Every new feature and every bug fix lands with tests in the same commit.**
  The unit under test is the logic/service layer — put behavior in
  `*_logic.py` / `*_service.py` and test it there; cogs/views/embeds are glue,
  exercised through the logic layer, not re-tested against Discord mocks.
- **What to cover** (this is the standard, not a line %): the happy path; **every
  guard/branch**, especially safety gates (NSFW `is_nsfw()`, opt-in, role gates)
  — a passing test *is* the enforcement CLAUDE.md's safety rule demands; and for
  a bug fix, **a test that fails before the fix** (write it first, watch it fail).
- **A bug observed in Discord still gets its failing test at the logic/service
  layer** — reproduce the state that broke, not the Discord surface where it
  showed up; add at most one wiring assertion in the cog test when the glue
  itself was wrong. Cog tests that re-prove service behavior through Discord
  mocks were the suite's main historical bloat (see
  docs/plans/test-suite-slim-and-remote-resilience.md).
- **Prefer a `pytest.param` row over a new test function** when covering
  another value variant of an existing behavior, and check whether a shared
  contract table already covers it (embed accents:
  `tests/test_embed_accent_contract.py` — new builders add one `case()` row,
  never per-file accent copies).
- **Coverage target is on the patch, not the repo.** New `*_logic.py` /
  `*_service.py` code should land ~80% of its new lines exercised. Don't chase
  whole-repo line %; don't lower `fail_under` in pyproject.toml — raise it when a
  feature adds headroom. The scoped gate below **hard-fails** if a *new*
  logic-layer file (`logic.py`, `store.py`, `service.py`, or anything ending
  `_logic.py` / `_service.py`) has no mapped test.

## Gates (before every commit)

- The **pre-commit hook** runs `python scripts/gate.py --scoped` automatically
  on every commit: ruff + pyright, then only the tests mapped to the staged
  diff (git diff vs HEAD + untracked). Touching a broadly-shared file (`core/`,
  `models/`, `migrations/`, deps, any `conftest.py`, `gate.py`) falls back to
  the full suite **in the prod checkout only**; in a session worktree that
  fallback is **deferred** — the diff maps normally and the gate prints which
  paths it skipped the full run for. The deferred run is not lost: gate main
  after merging (below). Changed source with no
  matching test prints "unmapped (CI/nightly covers it)". A **new**
  logic-layer file (`logic.py`/`store.py`/`service.py`/`*_logic.py`/
  `*_service.py`) with no mapped test is a hard failure, not a
  warning (add `tests/test_<feature>_logic.py`, or `--no-verify` if it's
  genuinely covered by an existing test under another name). `git commit
  --no-verify` bypasses the hook.
- `python scripts/gate.py` — full pytest (xdist-parallel; `-n 0` to debug a
  single test). **Run it on `main` once a batch of merges is complete** — that
  is where the work branches' deferred full runs are paid, and where a clean
  merge between two parallel sessions is caught leaving main red. A green run moves
  the local `last-full-gate` tag to main's HEAD, and `dk_session.py teardown`
  prints how far main has drifted from it, so a skipped run is visible rather
  than remembered. The tag is never pushed — it records what *this* machine
  verified. Full-suite
  green is required before a **push to origin**, but
  CI on that push satisfies it — a local full run is optional. If
  you do run it locally, run it **solo**: a parallel full run alongside other
  work can exhaust the tmpfs quota and spray hundreds of bogus sqlite errors
  (see memory: rm -rf /tmp/pytest-of-ben and re-run). `--quick` runs
  ruff + pyright (no pytest) plus the scoped browser panel checks (layout +
  console) when dashboard assets changed. Coverage floor in pyproject.toml must
  not be lowered.
- Backstop: CI (`.github/workflows/test.yml`) runs the full suite + coverage on
  every push/PR to main, and `nightly.yml` runs it on a schedule — so a miss in
  the scoped tier is caught at push, not in prod.

## Dependencies

- `requirements*.txt` = human-edited direct deps; `requirements*.lock` =
  compiled pins (what CI and prod actually install). After editing a .txt,
  regenerate: `uv pip compile requirements[-dev].txt -o requirements[-dev].lock
  --universal -p 3.14`. Dependabot bumps the locks weekly; CI green on its PR
  means the new versions passed the full suite.

## Commits

- Subject: `Scope: descriptive summary` (~60 chars), e.g.
  `Pen Pals: dashboard question bank + AI prompt studio`. Prose body: why,
  edge cases handled, what tests cover it.
- **No** `Co-Authored-By` / `Claude-Session` trailers.
- **User-facing change ⇒ a `Testing:` section** ending the message body, as
  `- [ ]` lines saying what to verify on the live server. User-facing means a
  member or admin can see it in Discord or on the dashboard; an internal
  refactor, a dep bump, a docs or test-only commit gets none.
- Write those lines **for a volunteer tester, not a developer** — they *are*
  the QA card's text. One action and one observable result per box; name what
  the tester clicks and sees, never a code identifier, table name, config key
  or issue number; verifiable in one sitting (nothing that waits for a
  scheduled job or overnight roll).
- The card is **one per feature, not per commit**: a merge posts nothing, and
  at `/dk-ship` teardown `scripts/post_testing_docs.py --branch <name>` gathers
  every `Testing:` section the branch ever merged, has Claude rewrite them into
  one deduped checklist, and posts a single QA Tracker card (Pass/Fail/Blocked
  buttons). A commit landing straight on main still posts its own card. Run
  that command by hand for a session shipped with `--keep`, which never tears
  down and so never posts.

## Conventions

- **Node 20 is installed user-local** at `~/.local/lib/node20` (symlinked into
  `~/.local/bin`, already on PATH) purely as dev tooling — nothing the bot or
  dashboard runs at runtime depends on it, and it is not a system package.
  It exists so the **blocking** CI lint job can be reproduced before pushing:
  `npm install --no-save` once, then `npx eslint src/web_server/static/js` and
  `npx stylelint "src/web_server/static/**/*.css"` — the exact commands
  `.github/workflows/test.yml` runs. Run both after touching dashboard JS/CSS;
  stylelint takes `--fix` for the mechanical ones. The `gjs` `Reflect.parse`
  one-liner still works for a quick syntax-only check without npm.
  Static-asset cache-busting is automatic (per-boot `?v=` rewrite in
  `server.py`); JS edits show up after the next service restart, not before.
- **Dashboard test suite** (`docs/web_testing.md`). Cross-cutting sweeps beyond
  per-route tests: an **authz sweep** (every route rejects an unauthenticated
  caller — a new route is covered automatically; add to `PUBLIC_PATHS` only if
  truly public), a **snowflake-precision sweep** (no id > 2^53 returned as a
  bare number), a **manual broken-link** check, and a **browser suite**
  (`browser` marker, Playwright): responsive **layout** (no off-screen/clipped
  content at phone/tablet/desktop) and **panel-load health** (no JS exception /
  console error / broken fetch on mount). The browser suite runs scoped to
  changed panels in `gate.py` and fully in nightly; it auto-skips without a
  browser (`python -m playwright install chromium` to enable). Both tiers
  select by the **`browser` marker over `tests/web/`, never by filename** —
  naming files is what once left five of the seven browser test files running
  in no tier at all; `tests/test_gate_mobile_scope.py` fails if either tier
  goes back to a list. When you add or
  restyle a panel, prefer wrapping/scrolling flex rows over fixed-width ones and
  add an interaction scenario if layout lives behind a click; measure with
  `scripts/mobile_layout_scan.py`.
- **A cog holds `self.bot`, and reaches the app context through it**
  (`self.bot.ctx`). Cogs take `(self, bot)` only — there is no second `ctx`
  parameter and no `self.ctx` field, so shared helpers can rely on the bot
  being there rather than duck-typing what a caller happens to hold. Views
  and services that aren't cogs still take whatever they need directly.
- New embeds take their color from `safe_resolve_accent(source, guild)`
  (`core/branding`), where `source` is whatever you hold — a bot, an
  AppContext, or a db_path; pass `default=DEFAULT_ACCENT_COLOR` if you need a
  non-optional `Color`. Never call `resolve_accent_color` directly: it raises,
  and a repo-wide test fails the suite if you do. Keep red/green/etc. only
  where the color is semantic.
- **A member named inside an embed is resolved, never a `<@id>`** — an embed
  mention is resolved by the *reading* client from its own cache, so it renders
  as a bare number to anyone who hasn't seen that user. Use
  `services/name_resolver.build_name_fn`; builders take a `name_fn` and a test
  guards that every render site passes one. Mentions in message `content=` are
  fine. A **no-contact pair degrades to a plain `User <id>` on purpose** — don't
  sweep it. Fuller conventions for bot embeds/panels
  (section spacing, monospace tables, persistent views, ping allow-listing)
  live in `docs/embed_style_guide.md`.
- **Dashboard route ids are the bare feature name** (`pen-pals`, `role-menus`)
  — no `config-`/`games-`/`mod-` prefix. The prefixes on older ids are
  historical, and **every existing id is frozen**: deep links, the nav `help:`
  mappings, and usage telemetry all key off them. Regroup and relabel nav
  entries freely; never rename an id. Nav taxonomy, where a feature's settings
  live, and the URL-state convention are in `docs/dashboard_ia.md`.
- **Shared dashboard widgets are safe by default**: `table.js` escapes every
  cell (a column opts into markup with `html: true` and then owns its own
  escaping), and config panels mount through `mountAsync` so a failed first
  fetch renders an error with a retry, never a permanent spinner — the loader
  must **let its rejection reach it**, since an inner catch that returns
  normally makes the retry unreachable. A refreshing report panel uses
  `mountReloadable` so every pass is guarded, not just the first, and
  unsaved-edit tracking is per guarded form, not per page. Guild-scoped
  caches in `config-helpers.js` must be cleared in `resetMetaCaches()` — a test
  hard-fails if a new one isn't.
