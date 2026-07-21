# Booster Roles

**Flavor: Reference** — matches current behavior.

## Purpose

A cosmetic color-role picker for **server boosters**: the bot posts a panel of
image + button messages in a channel; pressing a button gives that booster the
matching color role and removes any other booster color role they held
(mutually exclusive set). Roles are typically generated in bulk from uploaded
"swatch" images whose filenames carry the gradient colors. No commands —
dashboard-configured (**Config → Roles → Booster Roles**, panel id
`config-booster-roles`, admin-only). Core logic lives in
`src/bot_modules/services/booster_roles.py`.

## Claim flow

Buttons are `BoosterRoleDynamicButton`, a `discord.ui.DynamicItem` with
custom-id template `booster_role:(?P<key>.+)`, registered at startup via
`bot.add_dynamic_items(BoosterRoleDynamicButton)` in
`src/dungeonkeeper/__main__.py` — so presses on panels posted before a restart
keep working with no view re-attachment. On press:

1. **Eligibility:** guild context required, then
   `member.premium_since is None` → ephemeral "Only server boosters can pick a
   cosmetic role." Boosting is the only gate — no role/level checks.
2. The button's key is looked up in `booster_roles`; a stale key or a
   `role_id` that no longer resolves in the guild gets an ephemeral error.
3. **Mutual exclusion:** every *other* configured booster role the member
   holds is removed (`reason="Booster cosmetic role switch"`), then the target
   is added (`reason="Booster cosmetic role pick"`). Already holding it with
   nothing to remove is a friendly no-op. All replies are ephemeral.

Nothing revokes the role when a member stops boosting — Discord keeps the role;
they just can't *switch* anymore.

## Panel posting

`post_or_update_booster_panel` (triggered from the dashboard's **Repost
Panel**, `POST /api/config/booster-roles/post-panel`) deletes the previously
posted panel messages (bulk-delete in ≤100 chunks per channel, per-message
fallback for >14-day-old messages), then posts a header
("**Pick your booster cosmetic role:**") plus one message per role — the
role's image file (if its `image_path` exists on disk) with a single
secondary-style button — with zero-width-space spacer messages between. All
posted message ids are saved to `booster_panel_messages` so the next repost
can clean up. Posting with zero configured roles is a 400.

## Swatch sync

The bulk pipeline (`sync_swatches`, dashboard **Sync Swatches** button):

- **Source folder:** per-guild managed uploads at `swatches/<guild_id>/` next
  to the DB (`get_guild_swatch_dir`); it wins as soon as it holds one validly
  named file, else the legacy global `booster_swatch_dir` config key (host
  path) is used (`resolve_swatch_directory`).
- **Filename contract:** `ColorName_HEX1_HEX2.ext` (png/jpg/jpeg/gif/webp) →
  label + two gradient hex colors; `role_key` is the lowercased,
  underscored label. Invalid names are skipped and flagged in the UI.
- **Sync:** creates a Discord role per new swatch with `color`/
  `secondary_color` from the hex pair, positions new roles under the
  `#### Cosmetics` anchor role (matched by name; best-effort), updates
  existing roles' colors/image paths in place, and **deletes** roles whose
  swatch file disappeared (Discord role + DB row). Sort order is HSV hue of
  the gradient, so the panel reads as a color wheel. An empty/all-invalid
  folder aborts with an error rather than deleting everything.

## Configuration (Config → Roles → Booster Roles)

The panel (`static/js/panels/config-booster-roles.js`) offers: per-role cards
(label, role, image path, sort order — save/remove), swatch upload/list/delete,
Sync Swatches, manual Add Booster Role, and Repost Panel. Routes in
`src/web_server/routes/config.py`, all `require_perms({"admin"})`:

| Route | Purpose |
|---|---|
| `PUT /api/config/booster-roles/{role_key}` | Upsert a role row (also used by Add). |
| `DELETE /api/config/booster-roles/{role_key}` | Remove a role row (Discord role untouched). |
| `POST /api/config/booster-roles/post-panel` | Repost the panel in a chosen text channel. |
| `POST /api/config/booster-roles/sync-swatches` | Run the swatch sync. |
| `GET/POST/DELETE /api/config/booster-roles/swatches[/{filename}]` | Managed uploads: list / upload (8 MB cap, sanitized filenames, image extensions only) / delete. |

Current state comes from `GET /api/config` (`booster_roles` list +
`booster_panel_channel_id`). `booster_swatch_dir` is settable through the
general settings update (`PUT /api/config`) but the panel no longer exposes
it — it survives as the legacy fallback only.

## Stored data

Tables are created idempotently by `init_booster_role_tables` at web-server
startup (`src/web_server/server.py`), not by a numbered migration (an old
single-row `booster_panel_messages` shape is migrated in the same function):

```
booster_roles(guild_id, role_key, label, role_id, image_path, sort_order)
    PK (guild_id, role_key)
booster_panel_messages(guild_id, channel_id, message_id)
    PK (guild_id, message_id)
```

Plus `booster_swatch_dir` in config (global, guild 0) and uploaded swatch
images on disk under `swatches/<guild_id>/`.

## Non-goals

- No automatic role removal when boosting lapses — eligibility is enforced at
  claim time only.
- No self-service color creation; members pick from the admin-curated set.
- No automatic panel refresh after sync or role edits — reposting is an
  explicit admin action.
- No slash-command surface at all.
