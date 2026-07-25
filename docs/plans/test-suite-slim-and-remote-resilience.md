# Test-suite slimming + remote-runner resilience

**Branch:** `test-suite-slim-and-remote-resilience` · **Started:** 2026-07-24

## Why

Two related problems surfaced together:

1. **Inode/disk exhaustion on test machines.** Nearly every DB-touching test
   builds a fresh file-backed SQLite DB by running all 140 migrations (~1.0s,
   2.2MB each), and WAL mode adds `-wal`/`-shm` sidecars — up to 4 inodes per
   test, ~7,800 tests, all retained for the whole session
   (`tmp_path_retention_count = 1` only prunes *previous* sessions). A full
   run writes ~1–2GB; on the remote runner this exhausted tmp and sprayed
   bogus sqlite failures that read as a red suite.
2. **Suite bloat.** 7,810 tests / 326 files, 88s just to *collect*. Three
   subagent analyses (2026-07-24) found the growth is structural: only 22
   exactly-duplicated functions, but ~99 copy-pasted games embed-contract
   tests, ~120 value-variant clusters collapsible via `parametrize`, and
   ~10–14 economy cog tests that duplicate service-layer coverage through
   Discord mocks (against CLAUDE.md's cogs-are-glue rule).

Methodology conclusion (kept here so the "why" survives): the suite achieves
its regression-safety goal, but (a) the glue-layer rule has no enforcement so
Discord-observed bugs get their regression test written at the mock layer,
(b) the *convenient* DB fixture is the *expensive* one, and (c) growth is
linear in test functions where it should be linear in parametrize rows.
Stages 2–5 fix the instances **and** the defaults that produced them.

## Stages

Each stage is one commit; later stages are independent of earlier ones except
where noted.

### Stage 1 — remote-runner resilience (`scripts/remote_test.py`)

The module's contract is "never block a commit"; three holes violate it:

- Exit codes outside pytest's 0–5 (ssh's 255 on a dropped connection, 137 on
  a kill) currently propagate as a red suite → map to local fallback.
- The pytest ssh session has no keepalive → a mid-run network drop hangs the
  pre-commit hook indefinitely. Add `ServerAliveInterval=15` /
  `ServerAliveCountMax=4`; with the exit-code fix, a dead link becomes a
  ~60s local fallback. Optional `REMOTE_TEST_TIMEOUT` (seconds, 0 = off)
  wall-clock cap as belt-and-braces.
- Remote tmp exhaustion (the observed failure) red-fails the run → bootstrap
  (remote-side, plain Python) sweeps stale `pytest-of-*` session dirs, GCs
  sibling `ws-*` workspaces untouched for 30 days, and preflights free
  disk/inodes, returning `BOOTSTRAP_FAILED` (= clean local fallback) when
  the host isn't fit.
- One `sync()` retry — a transient blip shouldn't cost a 10× slower local run.

### Stage 2 — template-DB fixtures + per-test teardown (`tests/conftest.py`, `tests/web/conftest.py`)

Build one fully-migrated template DB per xdist worker (`tmp_path_factory`);
`sync_db_path` / `temp_db` / `web_db` copy it per test (~2.3ms vs ~1.03s
measured, ~450×) and delete the db + `-wal`/`-shm` in teardown so inode use
stays flat. Redirect the ~85 file-local fixtures that call
`apply_migrations_sync` to the shared helper. Excluded (must keep running
real migrations): `tests/unit/test_games_migration.py`,
`tests/unit/test_whisper_migration*.py`, `tests/unit/test_id_remap.py`,
`tests/beta/test_beta_migration.py`.

### Stage 3 — shared games embed-contract harness

One parametrized module over a `(game, builder-factory)` case table replaces
the ~99 per-game copies of `*_honors_passed_accent` /
`*_falls_back_to_phase_color` (plus recurring footer/`escapes_markdown`
tests). Shared helpers (`_ACCENT`, `_name_resolver`, field lookup) hoisted to
`tests/conftest.py`. The games test template is updated so new games inherit
the harness instead of the copies.

### Stage 4 — parametrize merges + economy cog dedup

Per the 2026-07-24 subagent reports (re-verify each collapse at edit time —
counts are estimates): value-variant merges in games files (~75–90),
`test_economy_quests_service.py` (~12–15), `test_economy_cog.py` (~16),
`test_economy_service.py` (~2); delete the ~10–14 cog tests that fully
duplicate service coverage; add fixtures for the `_make_cog`/`_enable`/
`_interaction`/sent-text boilerplate (136/139/50/81 call sites).

### Stage 5 — methodology guardrails

CLAUDE.md addition: a bug observed in Discord gets its regression test at the
service layer plus at most one wiring assertion. Docs sweep per working
agreement.

## Expected outcome

- Remote runner degrades to local instead of hanging or red-failing.
- Per-test DB cost drops ~450×; tmp footprint drops from ~1–2GB/run to MBs.
- ~350–400 fewer test functions (7,810 → ~7,450) with identical behavioral
  coverage; collection time down accordingly.

## Related open findings (not in scope, flagged)

- 2026-07-22 deep review S2: `economy_cog.py` god-cog — logic in the cog is
  *why* some cog tests are legitimately cog-level; the refactor is separate.
- 2026-07-22 deep review S2: `gate.py` mandatory-test hard-fail misses bare
  `logic.py`/`store.py` filenames — adjacent to Stage 5, needs owner sign-off.
