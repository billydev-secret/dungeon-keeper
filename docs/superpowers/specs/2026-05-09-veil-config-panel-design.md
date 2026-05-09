# Veil Config Web Panel

**Date:** 2026-05-09  
**Status:** Approved

## Overview

Add a web dashboard config panel for the Veil NSFW guessing game. All veil settings currently configured via `/veil setup` (and its defaults) become editable from the Config section of the dashboard.

## Files Changed

| File | Change |
|------|--------|
| `web/routes/config.py` | Add `"veil"` block to `GET /api/config` response; add `PUT /api/config/veil` endpoint |
| `web/static/js/panels/config-veil.js` | New panel — single flat form |
| `web/static/js/app.js` | Register panel in Config section |
| `cogs/veil_cog.py` | No changes — `/veil setup` remains as a Discord-side alternative |

## Architecture

Follows the same pattern as confessions, spoiler, and starboard:

- `GET /api/config` (existing endpoint) gains a `"veil"` key populated by calling `get_veil_config` from `services/veil_repo.py`.
- `PUT /api/config/veil` writes individual keys via the existing `set_veil_config_value`.
- The frontend panel calls `loadConfig()`, `loadChannels()`, and `loadRoles()` in parallel, renders a single form, and on submit sends the full config object to the PUT endpoint.

## API Shape

### GET /api/config — added key

```json
"veil": {
  "channel_id": "0",
  "role_id": "0",
  "crop_difficulty": "medium",
  "guess_cooldown_seconds": 30,
  "min_image_dimension_px": 400,
  "max_image_size_mb": 10,
  "reuse_enabled": true,
  "reuse_quiet_hours": 24,
  "reuse_min_age_days": 30,
  "reuse_min_post_interval_hours": 48
}
```

IDs are strings (matching channel/role select convention). Numeric fields are ints. Bools are bools.

### PUT /api/config/veil

Accepts the same shape. Validates `crop_difficulty` is one of `easy | medium | hard`. Rejects unknown keys. Writes each field via `set_veil_config_value(conn, guild_id, key, value)`.

## Panel Layout

Single flat form in the Config section, labelled "Veil":

- **Game Channel** — channel select; `"0"` = disabled
- **Required Role** — role select; `"0"` = anyone can submit
- **Crop Difficulty** — select: easy / medium / hard
- **Guess Cooldown** — number input (seconds, min 0)
- **Min Image Dimension** — number input (px, min 1)
- **Max Image Size** — number input (MB, min 1)
- **Reuse Enabled** — yes/no select
- **Reuse Quiet Hours** — number input
- **Reuse Min Age Days** — number input
- **Reuse Min Post Interval Hours** — number input
- Save button + status indicator (reuses `showStatus`)

## Error Handling

- Save errors shown inline via `showStatus(el, false, err.message)`.
- Backend returns `{"ok": false, "detail": "..."}` for invalid `crop_difficulty` or unknown keys.
- Panel shows a loading state while fetching config.

## Out of Scope

- No changes to `/veil setup` Discord command — it continues to work as before.
- No new unit tests for the web layer (config PUT endpoints are not unit-tested in this codebase).
