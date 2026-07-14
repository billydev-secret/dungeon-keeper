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

## Workflow

- Do edits in a git worktree; merge back to main when ready for user testing.
- This checkout **is production**. Never restart the bot or dashboard
  (`sudo systemctl restart dungeon-keeper`) unasked — code changes apply on
  restart, and the user pushes that button.
- Large tasks (multi-stage refactors or big features) get a plan doc in
  `docs/plans/`; commits reference their stage.
- When touching a module with open findings in `docs/reviews/`, mention them
  and offer to fold fixes in — don't expand scope uninvited.

## Gates (before every commit)

- The **pre-commit hook** runs `python scripts/gate.py --scoped` automatically
  on every commit: ruff + pyright, then only the tests mapped to the staged
  diff (git diff vs HEAD + untracked). Touching a broadly-shared file (`core/`,
  `models/`, `migrations/`, deps, any `conftest.py`, `gate.py`) falls back to
  the full suite, so those commits pause longer; changed source with no
  matching test prints "unmapped (CI/nightly covers it)". `git commit
  --no-verify` bypasses the hook.
- `python scripts/gate.py` — full pytest (xdist-parallel; `-n 0` to debug a
  single test). Full-suite green is required before a **push to origin**, but
  CI on that push satisfies it — a local full run is optional. If
  you do run it locally, run it **solo**: a parallel full run alongside other
  work can exhaust the tmpfs quota and spray hundreds of bogus sqlite errors
  (see memory: rm -rf /tmp/pytest-of-ben and re-run). `--quick` runs
  ruff + pyright only (no pytest). Coverage floor in pyproject.toml must not
  be lowered.
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
- Behavior-changing commit ⇒ append a live-test entry to
  `docs/TESTING_QUEUE.md` (same commit): what to verify on the live server,
  as checkboxes, with the commit hash.

## Conventions

- No Node on this box — syntax-check dashboard JS with a
  `gjs` `Reflect.parse` one-liner. Static-asset cache-busting is automatic
  (per-boot `?v=` rewrite in `server.py`); JS edits show up after the next
  service restart, not before.
- New embeds take their color from `resolve_accent_color(db_path, guild)`;
  keep red/green/etc. only where the color is semantic.
