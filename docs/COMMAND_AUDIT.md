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
| `/voice voice-admin …` config (voice_master_cog) | `/voice-master/config`, `/voice-master/name-blocklist`, `/voice-master/profiles` | PENDING — see below |

`/help` (mod_cog) updated: removed the dead "Activity & Graphs" page and the `promotion_review` entry.

### Checked and intentionally kept (loaded cogs, no clean web-dup)
`/xp_leaderboards` (member-facing), `/xp_give` (no web award route), `/watch …` (DMs invoker),
`/wellness`,`/away` (member self-service), `/todo` (member), `/starboard …` (**no** web route),
`/delete_me`/`/delete_user`, `/purge`, `/jail`,`/ai …`,`/ticket`,`/policy`, music, games, etc.

---

## Pending decision 1 — voice-admin split (needs confirmation)

`voice_admin` mixes web-duplicated config with Discord-coupled actions. Proposed split:

- **Remove** (covered by `/voice-master/config` & friends): `set-hub`, `set-category`,
  `set-control-channel`, `set-default-name`, `set-int`, `disable-saves`, `saveable-fields`, `show`,
  `view-profile` (→ `GET /voice-master/profiles/{user_id}`), and the whole `name-blocklist` group
  (add/remove/list → `/voice-master/name-blocklist`).
- **Keep**: `post-panel`, `post-inline-panel` (post into a channel), `force-delete`, `force-transfer`
  (real-time actions). **`force-clear-profile`**: recommend KEEP — it's a destructive admin action and
  the web profiles route appears to be **read-only** (`GET` only, no clear/delete). Removing it would
  leave no replacement.

This is surgical editing of ~10 interleaved methods in a 1760-line cog with live listeners — held for
explicit go-ahead on the split (esp. `force-clear-profile`).

## Pending decision 2 — delete the dead files? (intent confirmation)

Strong evidence says the 8 unloaded cogs above were **deliberately migrated** (web routes exist; stale
`/help`). If confirmed, these are safe to delete as cleanup:

- **Dead cogs (8):** `drama_cog`, `interaction_cog`, `gender_cog`, `auto_delete_cog`, `welcome_cog`,
  `foolsday_cog`, `wellness_admin_cog`, `inactivity_prune_cog`.
- **Dead `commands/*.py` (7):** `xp_commands`, `interaction_commands`, `activity_commands`,
  `mod_commands`, `auto_delete_commands`, `foolsday_commands`, `invite_commands`
  (keep `drama_commands.py` — imported by `web_server/routes/reports.py`).

Not deleted yet — confirm these were intentional (not unfinished work) first.
