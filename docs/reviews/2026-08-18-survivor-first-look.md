# Survivor — Billy's first-look comments (2026-08-18)

Running log of comments from Billy's first pass over Survivor (shipped to main
2026-08-17, merge `ae803a5a`; live in prod since the 23:00:53 restart, nothing
configured, nothing played). **Collect now, act at the end** — no code changes
until Billy calls it.

Prod state at the time of review: `survivor_seasons`, `survivor_players`,
`survivor_picks`, `nfl_games`, `survivor_flavor` all 0 rows. No live data to
preserve, so shipped defaults can be changed outright.

---

## 1. Drop the flavor corpus so far

> "so far I want to drop the flavor corpus so far."

**Target:** `DEFAULT_FLAVOR`, `src/bot_modules/services/survivor_service.py:475`
— ~44 lines across the four `FLAVOR_CATEGORIES` (eulogy 20 / toll 10 /
no_death 10 / annul 4), seeded per-guild by `seed_default_flavor()` from the
dashboard's create-season route (`src/web_server/routes/survivor.py:186`).

**Open — scope not yet settled.** Three readings, not yet chosen:

- **(a) Delete the shipped lines only.** Season starts with an empty corpus;
  admin writes their own in the panel's flavor CRUD. Feature, CRUD, rotation
  and the per-guild-voice route all stay.
- **(b) Rip out the flavor feature.** Corpus + CRUD routes + rotation +
  dashboard card + `survivor_flavor` table; hardcode the Reckoning copy.
  Needs migration 169, spec and manual.html updates. Ends the per-guild
  voice route permanently.
- **(c) Replace, don't empty.** Rewrite `DEFAULT_FLAVOR` in a different
  register rather than shipping nothing.

**Findings that bear on it:**

- `rotate()` (`src/bot_modules/survivor/reckoning.py:51`) returns `""` on an
  empty category — **no crash**, but the toll line renders blank and no
  eulogies print. Under (a), the Reckoning embed needs a graceful-empty pass;
  I have not yet audited `survivor/embeds.py` for how `""` lays out.
- The corpus is the *only* route to a per-guild voice. Standard sports
  register was deliberately made the hardcoded default (commit `761b6182`)
  with TGM's meadow voice moved *into* the corpus. (b) closes that door for
  every guild; (a) leaves it open but ships nothing in it.
- The auto-assign-cap death line ("the groundskeeper stopped covering…") is
  **not** in the corpus — it's fixed at the site, so it survives all three.

---

## 2. The panel page is dense and under-formatted

> "the panel page has a lot of information without a lot of formatting.
> mismatched headers and buttons without design showing functional grouping
> sometimes."

**Target:** `src/web_server/static/js/panels/survivor.js` (711 lines).

**Measured:** 15 `.card`s, 16 buttons, **31 inline `style="` attributes**, 18
`.field-hint`s, 15 `.section-label`s — and heading elements used exactly
**once each**: one `<h2>` (panel title) and one stray `<h4>` (the sim
fixture name at :229). So there is no heading hierarchy at all below the
panel title; every card announces itself with a `.section-label` div, and
two of them are duplicated (`Season` ×2, `Roster` ×2) while one renders
**empty**. Two cards get a gold border via inline style rather than a class.

Button treatment is ungrouped: five variants in use (`btn-primary` ×5,
`btn-small` ×6, `btn-small btn-danger` ×2, bare `btn` ×2, `btn-danger` ×1)
with no visual distinction between *destructive*, *simulator-only* and
*routine* actions — the Season card puts **📌 Post Panel** (routine),
**▶ Run Weekly Tasks** (forces the clock; see #4) and **End Season**
(destructive) in one undifferentiated flex row.

For comparison, `role-menus.js` uses a real `h2/h3/h4` ladder; `pen-pals.js`
uses one `h2` + `section-label`. Survivor matches neither.

**Not yet decided:** whether the fix is a restyle in place (heading ladder,
drop inline styles for classes, group buttons by consequence) or a
reorganization of which cards exist and in what order. CLAUDE.md's guidance
("if a config page feels jumbled, reorganize it rather than appending")
points at the latter.

---

## 3. The channel panel did not update on join — **root cause found, real bug**

> "the panel is not updating when I joined"

**Confirmed in prod.** Season 1 "The Golden League" (guild
`1469491362444480666`, year 2035, status `enrolling`) has one player row —
Billy, `alive`, joined `2026-08-18T06:19:56Z`. So the join **wrote and
committed**; the panel just never redrew.

The prod log carries the traceback, at 23:19:56 local, from
`JoinConfirmView`'s "✅ I'm In" button:

```
sqlite3.OperationalError: cannot commit - no transaction is active
  ... survivor/views.py:310 in confirm → to_thread(_q)
  ... survivor/views.py:279 in _q → with open_db_immediate(db_path) as conn
  ... core/db_utils.py:65 → conn.execute("ROLLBACK")
sqlite3.OperationalError: cannot rollback - no transaction is active
```

**Cause:** `open_db_immediate` (`src/bot_modules/core/db_utils.py:53`) opens
with `isolation_level=None` and drives the transaction by hand — explicit
`BEGIN IMMEDIATE` on entry, `COMMIT` on exit. `_q` in `views.py:279` calls
**`conn.commit()` itself** on all three of its return paths (ghost-only :299,
gauntlet :302, normal :305). That inner commit ends the transaction, so the
context manager's exit-time `COMMIT` raises "no transaction is active" — and
the `except BaseException:` handler's `ROLLBACK` then raises too, masking the
first error.

**Consequences, in order:** the member is enrolled and charged (that part
committed), then the `OperationalError` escapes `_q` at the `to_thread`
await (:310). Only `logic.PickError` is caught there, so everything after is
skipped: the success response, `_grant_survivor_role` (:340),
`refresh_panel()` (:346) and `_echo_join()` (:347). Result: **panel never
redraws, no main-chat join echo, no Survivor role granted**, and the member
sees a failed interaction despite having been charged and enrolled.

**Scope:** a scan of every `with open_db_immediate(...)` block in `src/` finds
this inner-`commit()` pattern **only** in `survivor/views.py:279` — it is not
a fleet-wide problem.

**Fix (when we act):** delete the three `conn.commit()` calls; the context
manager already commits. Regression test at the logic/service layer that
drives the join through `open_db_immediate` and asserts no exception — it
fails before the fix. Secondary: `open_db_immediate`'s `except BaseException`
should not let a failed `ROLLBACK` shadow the original exception.

---

## 4. Why is "▶ Run Weekly Tasks" on the Season card?

> "why is there a 'run weekly tasks' there?"

**What it does:** `POST /api/survivor/tasks/run` → `run_weekly_tasks(...,
force=True)`, which fires the three clock-gated weekly jobs past their
day/hour gates — Wednesday `slate_hour` panel repost + week-open ping,
Saturday `lastcall_hour` pickless DM, Tuesday `reckoning_hour` Reckoning
post. The once-per-week state in the season config still holds, so it can't
double-post; the button's confirm dialog says as much.

**Why it exists:** the weekly cadence is the only thing that drives Survivor
forward, and without a force button nothing at all happens between
Wednesdays — you couldn't exercise a week in the Simulator, and a bot that
slept through a Tuesday could not be nudged.

**The objection, as I read it:** it is a *debug/operator* affordance sitting
undifferentiated next to Post Panel and End Season on the production Season
card — which is also exactly the grouping complaint in #2. Options when we
act: move it onto the Simulator card, gate it behind a disclosure, keep it
but style it as an operator action, or drop it and rely on the clock.
**Not decided.**

---

## Incidental findings (not Billy's comments, noted while grounding the above)

- The live season is year **2035**. Only `>= 2090` is synthetic, so this is
  treated as a *real* season and the ESPN ingest has no 2035 schedule to
  fetch — `nfl_games` will stay empty and `pick_week` returns None, which is
  why the panel shows the pre-kickoff face. Worth confirming this is
  deliberate (a hand-built test season) rather than a mis-typed year.

---

## 5. "Run Weekly Tasks" changes nothing in the channel

> "run weekly tasks doesn't seem to make a change in the channel"

**Confirmed, and it is two separate problems.**

**(a) There is no schedule, so nothing can ever be due.** `nfl_games` is
**0 rows** in prod. All three gates route through the week lookup:
`slate_due` (:79) and `lastcall_due` (:92) call
`logic.pick_week(conn, season_year, now)` and return `None` the moment it is
`None` — **before** the `force` check at :83/:96. `reckoning_due` (:105)
likewise returns `None` from `next_reckoning_week`. So `force=True` skips the
day/hour gates but cannot manufacture a week: with no games, all three
return `None` and `run_weekly_tasks` posts nothing. Nothing is logged,
because nothing errored.

This is the year-2035 note from #4's incidentals, now load-bearing: 2035 is
below the `>= 2090` synthetic threshold, so it is treated as a **real**
season and the ESPN ingest has no 2035 schedule to fetch — while the
Simulator's synthetic generator, which would have produced games, never
fires for it. The season is therefore permanently stuck pre-kickoff.

**(b) The button lies about it.** `POST /api/survivor/tasks/run` awaits
`run_weekly_tasks(..., force=True)` and returns success unconditionally;
the panel then shows **"tasks ran — check the channel"** whether it posted
three things or nothing at all. A silent no-op that reports success is the
reason this cost a debugging round.

**Fix (when we act):** (b) is the real code defect — `run_weekly_tasks`
should report which tasks fired (or that none were due and why: "no schedule
ingested for 2035"), and the route should surface that instead of a flat
success. (a) is a configuration question for Billy: was 2035 meant to be a
Simulator year (`>= 2090`), or should the real 2026 schedule be ingested?

---

## 6. A forced run hit every guild, not the caller's (found while fixing #5)

`POST /api/survivor/tasks/run` called `run_weekly_tasks(bot, db_path, now,
force=True)` with **no guild filter**, while `run_weekly_tasks` iterates
*every* guild with a non-complete season. Prod runs three guilds. One admin
pressing ▶ Run Weekly Tasks on their own dashboard would therefore force
another server's Reckoning, panel repost and last-call DMs past their clock
gates. Not observed live only because no other guild has a season yet.

---

## Status — what is fixed, what is still open

**Fixed (uncommitted at time of writing):**

- **#3** — the three `conn.commit()` calls inside `_q`'s
  `open_db_immediate` block are gone; the context manager already commits.
  Guarded by `test_no_source_commits_inside_open_db_immediate`, a static
  contract test over all of `src/` (verified: it reports all three
  offenders on the pre-fix source and passes on the fixed one), plus
  commit/rollback behaviour tests for `open_db_immediate` itself, which had
  none.
- **#5b** — `run_weekly_tasks` now returns a per-season record (`fired`,
  `blocked`, `reason`); `post_reckoning` / `post_slate` / `send_last_call`
  return a bool so the report can't claim a task fired when the channel was
  unreachable; the new `idle_reason()` names *why* nothing was due, and the
  panel prints it instead of "tasks ran — check the channel". Three
  `idle_reason` tests.
- **#6** — the route now passes `guild_id`, and `run_weekly_tasks` takes a
  `guild_id` filter.
- **#5a** — Billy's call: recreate the season as a **Simulator** season
  (year >= 2090) so the synthetic generator builds a schedule. A dashboard
  action, no code needed; season 1 (2035) must be ended first under the
  one-live-season rule.

**Still open:**

- **#1 flavor corpus** — scope still unchosen (a/b/c above).
- **#2 panel formatting** — needs a plan.
- **#4 Run Weekly Tasks placement** — **the spec already answers this**:
  §"Testing rig (added 2026-08-18)" puts ▶ Run Weekly Tasks on the
  **Simulator card**, as part of the rig. It shipped on the **Season card**
  instead. So the fix is to move it where the spec says, unless #2's
  reorganization decides otherwise.
