# Bot Identity Panel Design

**Date:** 2026-05-09
**Scope:** Per-server bot nickname and guild member avatar, exposed in the Global Config panel of the dashboard.

---

## Overview

Adds a "Bot Identity (this server)" section to the existing Global Config panel. Allows admins to change the bot's guild nickname and guild-specific avatar without touching the global Discord account identity (which is managed via the Discord Developer Portal).

Changes are applied live via the Discord API (`guild.me.edit()`), not stored in the database.

---

## Architecture

### New API endpoint

`POST /api/config/bot-identity`

- **Auth:** admin
- **Body:** `multipart/form-data`
  - `nick` (string, optional) — new guild nickname; empty string clears it
  - `avatar_url` (string, optional) — image URL to fetch and upload as the guild avatar
  - `avatar_file` (binary, optional) — image file uploaded directly; takes priority over `avatar_url`
- **Logic:**
  1. Resolve avatar bytes: if `avatar_file` provided use it; else if `avatar_url` provided fetch it with `httpx`; else `None` (no avatar change).
  2. Build `edit_kwargs`: include `nick` if provided, include `avatar` if bytes resolved.
  3. Call `await guild.me.edit(**edit_kwargs)`.
  4. Return `{"ok": True, "nick": guild.me.nick, "avatar_url": <guild member avatar URL>}`.
- **Errors:**
  - 503 if bot or guild unavailable
  - 400 if image URL fetch fails or Discord rejects the payload (invalid format, etc.)

### GET /api/config — new `bot_identity` section

Add to the existing config snapshot:

```json
"bot_identity": {
  "nick": "DungeonKeeper",      // guild.me.nick or ""
  "avatar_url": "https://..."   // guild member avatar URL, fallback to global bot avatar
}
```

This is read-only; the panel loads it on mount to pre-fill the nickname and render the avatar preview.

---

## Frontend

**File:** `web/static/js/panels/config-global.js`

New `<section>` appended below the existing `<form>`, rendered after the config load:

```
┌─ Bot Identity (this server) ──────────────────────┐
│  [Avatar preview img]                             │
│                                                   │
│  Nickname  [___________________________]          │
│                                                   │
│  Avatar URL  [_________________________]          │
│  — or —                                           │
│  Upload image  [Choose file]                      │
│                                                   │
│  [Apply]  <status>                                │
└───────────────────────────────────────────────────┘
```

**Behaviour:**
- Avatar preview is a small `<img>` (64×64) initialised from `config.bot_identity.avatar_url`.
- File input takes priority over URL input; both are optional.
- On submit: build a `FormData`, append fields that are non-empty, POST to `/api/config/bot-identity`.
- On success: update the avatar preview `src` from the response `avatar_url`; show "Applied" status.
- On error: surface the error detail in the status span.
- The Apply button is independent of the main Save button.

---

## Error handling

| Condition | HTTP | UI message |
|-----------|------|------------|
| Bot not connected / guild unavailable | 503 | "Bot not available" |
| Image URL fetch fails | 400 | error detail from response |
| Discord rejects edit (bad format, permissions) | 400 | error detail from response |

---

## Out of scope

- Global username / account avatar (handled in Discord Developer Portal)
- Storing the nickname/avatar in the database (these live on the Discord side)
- Rate-limit UI (Discord's per-server edit limit is generous; no special handling needed)
