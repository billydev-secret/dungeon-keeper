# Slash Command Audit — Web Dashboard Overlap

**Date:** 2026-05-29
**Scope:** Which *currently-registered* slash commands duplicate the FastAPI dashboard (`src/web_server/`).
**Method:** Verified against the live load list `bot.extension_names` in `src/dungeonkeeper/__main__.py`
(the only loader — no auto-discovery; `beta_tools` is a dev-only sidecar that loads just `beta_tools.slash`).

---

## Key finding: the migration was almost entirely already done

Most "report/analytics/config" commands were **already moved to the web dashboard** — their cog files
still exist but are **not in `extension_names`, so they never register as slash commands**:

`drama_cog` (`/chilling_effect`), `interaction_cog` (`/invite_web`), `gender_cog` (`/gender …`),
`auto_delete_cog` (`/auto_delete`, `/auto_delete_configs`), `welcome_cog` (`/welcome_preview`,
`/leave_preview`), `foolsday_cog` (`/foolsday …`), `wellness_admin_cog` (`/wellness-admin …`),
`inactivity_prune_cog` (`/inactivity_prune …`).

`git log -S` shows these strings were **never** in `__main__.py` — the commands were retired to the web
and the cog files left behind. `/help` still advertised some of them (the whole "Activity & Graphs"
page pointed at dead commands), confirming stale docs rather than missing features.

The background **services** for these still run (`auto_delete_loop`, `inactivity_prune_loop`); only the
management slash commands moved to the web.

---

## What was actually live AND duplicated — and what was done

Only four registered commands genuinely duplicated a live dashboard route:

| Command (cog) | Dashboard route | Action taken |
|---|---|---|
| `/activity` (activity_cog) | `GET /activity` | Removed — delisted + cog file deleted |
| `/config` (config_cog) | `GET/POST /config…` | Removed — delisted + cog file deleted (config now web-only, per decision) |
| `/report promotion_review` (reports_cog) | reports section | Removed — `report` group dropped; `quality_leave` kept |
| `/voice voice-admin …` config (voice_master_cog) | `/voice-master/config`, `/voice-master/name-blocklist`, `/voice-master/profiles` | Removed — config setters, view-profile, name-blocklist group; kept post/force actions + force-clear-profile |

`/help` (mod_cog) updated: removed the dead "Activity & Graphs" page and the `promotion_review` entry.

### Checked and intentionally kept (loaded cogs, no clean web-dup)
`/xp_leaderboards` (member-facing), `/xp_give` (no web award route), `/watch …` (DMs invoker),
`/wellness`,`/away` (member self-service), `/todo` (member), `/starboard …` (**no** web route),
`/delete_me`/`/delete_user`, `/purge`, `/jail`,`/ai …`,`/ticket`,`/policy`, music, games, etc.

---

## Done — dead-file cleanup

Confirmed (not in the Discord `/` menu) and deleted — 8 unloaded cogs + 3 unused command modules
with **no importers and no tests**:

- **Cogs (8):** `drama_cog`, `interaction_cog`, `gender_cog`, `auto_delete_cog`, `welcome_cog`,
  `foolsday_cog`, `wellness_admin_cog`, `inactivity_prune_cog`.
- **`commands/*.py` (3):** `auto_delete_commands`, `foolsday_commands`, `invite_commands`.
- Kept `drama_commands.py` (imported by `web_server/routes/reports.py`).
- (`activity_commands.py` from the original list never existed in this repo.)

`pytest --co` collects 1631 tests with no import errors after deletion.

## Done — follow-up cleanup

1. **3 test-covered dead command modules deleted** (`xp_commands`, `interaction_commands`,
   `mod_commands`) along with their `tests/test_commands.py` groups; the `role_grant` tests were kept.
2. **8 orphaned command modules deleted** (no importers, no tests): `config_commands`,
   `gender_commands`, `inactivity_prune_commands`, `welcome_commands`, `wellness_admin_commands`,
   `wellness_commands`, `watch_commands`, `privacy_commands`. The live `watch_cog`/`privacy_cog`/
   `wellness_cog` define their commands inline; the web config/gender routes call the *services*
   directly, not these modules.
   - Kept: `drama_commands` (web reports), `jail_commands`, `role_grant_commands`,
     `voice_master_commands` — all have live importers.
   - `gender_service` / `welcome_service` are **not** orphaned (used by live web routes + `events_cog`).

## Still open

- **`services/foolsday_service.py`** — production-unused (only the deleted `foolsday_cog` used it),
  but it has a dedicated test (`tests/test_foolsday_service.py`) and implements a **seasonal**
  (April Fools) shuffle algorithm. Left in place — confirm the feature is fully retired before
  deleting it + its test.
