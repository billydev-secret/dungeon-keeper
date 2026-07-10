# Dungeon Keeper — working agreement

Discord bot (`src/bot_modules/`: thin cogs, logic in per-feature modules) +
FastAPI dashboard (`src/web_server/`: routes + vanilla-JS panels in `static/js/`),
SQLite-backed. Tests in `tests/`.

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

- `python scripts/gate.py` — ruff + pyright + full pytest (xdist-parallel;
  `-n 0` to debug a single test), all green. `--quick` skips pytest (the
  pre-commit hook runs that). Coverage floor in pyproject.toml must not be
  lowered.

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
