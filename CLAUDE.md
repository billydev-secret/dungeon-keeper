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
  toggles (see Voice Master's access dial). Consistent button shapes/sizes;
  if a config page feels jumbled, reorganize it rather than appending.
- **Safety & privacy defaults:** NSFW gates on `channel.is_nsfw()` (Discord's
  own age-gate), never a bot-side toggle. Store minimal data — message
  content is off by default, so derive metadata at ingest time. Sensitive
  access is opt-in. Never ship a preference or toggle that isn't enforced.
- If a feature genuinely seems to need in-Discord admin config, raise it and
  ask instead of building it.

## Docs

- Specs live in `docs/`. Read `docs/INDEX.md` **first** — it classifies every
  spec as Reference / Design / Aspirational. Aspirational specs describe
  unbuilt features; when a spec and the code disagree, the code wins.
- Behavior change ⇒ update the matching spec (and its INDEX.md classification
  if it changed flavor) **in the same commit**.
- UI/UX change (new/changed slash command, dashboard panel, embed copy,
  button/modal flow) ⇒ also update the **user-facing website docs** in the
  same commit: `src/web_server/static/manual.html` (the guide rendered in
  the dashboard's own Help panel — routed via
  `static/js/panels/help-sections.js`/`help.js`), plus README.md's
  slash-command reference. This is a different surface from `docs/` (dev
  specs) and drifts independently — don't let it lag while `docs/` stays
  current.

## Workflow

- Do edits in a git worktree; merge back to main when ready for user testing.
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
  the full suite, so those commits pause longer; changed source with no
  matching test prints "unmapped (CI/nightly covers it)". A **new**
  logic-layer file (`logic.py`/`store.py`/`service.py`/`*_logic.py`/
  `*_service.py`) with no mapped test is a hard failure, not a
  warning (add `tests/test_<feature>_logic.py`, or `--no-verify` if it's
  genuinely covered by an existing test under another name). `git commit
  --no-verify` bypasses the hook.
- `python scripts/gate.py` — full pytest (xdist-parallel; `-n 0` to debug a
  single test). Full-suite green is required before a **push to origin**, but
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
- Behavior-changing commit ⇒ end the message body with a `Testing:` section
  listing what to verify on the live server, as `- [ ]` checkbox lines. The
  post-commit hook (`scripts/post_testing_docs.py`) reads it straight off the
  commit and posts a QA Tracker card (Pass/Fail/Blocked buttons in Discord)
  automatically — no separate doc to maintain.

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
  browser (`python -m playwright install chromium` to enable). When you add or
  restyle a panel, prefer wrapping/scrolling flex rows over fixed-width ones and
  add an interaction scenario if layout lives behind a click; measure with
  `scripts/mobile_layout_scan.py`.
- New embeds take their color from `resolve_accent_color(db_path, guild)`;
  keep red/green/etc. only where the color is semantic. Fuller conventions
  for bot embeds/panels (section spacing, monospace tables, persistent views,
  ping allow-listing) live in `docs/embed_style_guide.md`.
