# LegitLibs per-guild templates (with opt-in global pool)

**Status:** In progress. Branch `legitlibs-per-guild-templates`.

## Why

`legitlibs_templates` has no `guild_id` — every template is shared across all
guilds, and every guild's dashboard lists/edits/deletes the same global set.
Sibling per-guild tables (`games_allowed_channels`, `games_game_history`) were
scoped in migration 122; LegitLibs was left as an explicit open question.

Decision (2026-07-23): templates are **per-guild by default**, with an **opt-in
global** tier — an admin can promote a template to a shared pool that every guild
draws from. Gameplay for guild G draws from **G's own templates + global ones**.
Existing templates (all currently global) are **assigned to the sole guild** on a
single-guild install (re-globalizable individually); a multi-guild install leaves
them global.

## Model

- `legitlibs_templates.guild_id`: `0` = **global/shared** (every guild sees it and
  can draw it); `>0` = **owned by that guild**.
- Selection (`pick_template`): `WHERE status='published' AND tier <= ? AND
  (guild_id = ? OR guild_id = 0)`.
- Ownership for edit/delete/promote: a guild may manage its **own** templates
  (`guild_id = active`) and the **global** ones (`guild_id = 0`) — there is no
  cross-guild super-admin, and the global pool is a shared library (mirrors the
  question-bank global pool, which is editable by any game-host admin).

## Stages

1. **Migration 124** — `ALTER TABLE legitlibs_templates ADD COLUMN guild_id
   INTEGER NOT NULL DEFAULT 0`; index `(guild_id, tier, status)`; single-guild
   backfill of existing rows to the sole guild (guard `COUNT(DISTINCT guild_id)
   FROM games_game_config = 1`, matching migration 122). Update
   `init_games_tables` / any in-code schema fallback to match.
2. **Selection** — `data.py::pick_template` adds the `(guild_id = ? OR guild_id =
   0)` clause; `count_published` and `pick_template`'s starter-count read scope the
   same way. The seeder (`INSERT OR IGNORE ... legitlibs_templates`) writes
   `guild_id = 0` (starter content is global).
3. **Routes** (`routes/games.py`) — `list` filters own+global and returns
   `guild_id`/`is_global`; `create` sets `guild_id = active`; `update`/`delete`
   scope to own+global; new `PUT .../{id}/scope` promotes/demotes
   (`guild_id = 0` ↔ active).
4. **Dashboard** (`games-legitlibs.js`) — a "This server / Global" scope badge per
   row and a Make-global / Make-server-only toggle.
5. **Tests** — selection draws own+global and excludes another guild's; create
   stamps the active guild; promote toggles scope; single-guild backfill assigns,
   multi-guild no-ops.
6. **Docs** — update `games_system_spec.md` (currently calls the bank/templates
   "per-guild content" — now accurate for LegitLibs) and `manual.html` if the
   member-facing copy references template scope.

## Out of scope

`games_question_bank` stays global for now (the other games' shared bank); this
change is LegitLibs-only per the decision.
