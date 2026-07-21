# Role Menus

**Status:** built (2026-07-14: 2a75737 routes, 65b5717 panel, 6c62514 tests, 3ffb5b5 nav) · **Owner:** role_menus · **Spec:** `docs/role_menus_spec.md`

## Goal

Self-service roles: DK posts an embed with buttons or a dropdown; members grant/
remove roles on themselves with private ephemeral feedback. Admins build,
preview, publish, and maintain menus entirely from a new Oracle page — no
commands, no reactions. Full v1 in one merge (user-confirmed), free-text emoji
input (no guild-emoji picker in v1, user-confirmed).

## Shape (mirrors the Docs feature)

Docs is the proven template: dashboard authors content, publishes/edits Discord
messages directly through the shared `ctx.bot` on the shared event loop, tracks
message ids in SQLite, and exposes a list+editor panel with a live `dp-*`
embed preview. Role Menus clones that shape and adds the one new ingredient:
**persistent interactive components** via `discord.ui.DynamicItem` (the
voice_master pattern — regex `template` custom_ids, re-registered with
`bot.add_dynamic_items` in `cog_load`, state rebuilt from the custom_id).

## Stages

1. **Schema + db module** — `src/migrations/073_role_menus.sql`:
   - `role_menus` — guild_id, title/description/accent/thumbnail_url,
     style (`buttons|dropdown`), mode (`toggle|unique|verify|drop|binding`),
     max_roles (0 = none), required_role_id, cooldown_seconds, placeholder,
     enabled, channel_id (0 = draft), message_id, alerted_at (degradation
     alert dedupe), created/updated bookkeeping.
   - `role_menu_options` — menu_id, role_id, label, emoji, description,
     button_color (`secondary|primary|success|danger`), position, elevated
     flag (loud override for dangerous roles).
   - `role_menu_grants` — append-only history (menu_id, guild_id, user_id,
     role_id, action, created_at). Survives menu deletion ("who got what,
     when" success criterion).
   - `role_menu_bindings` — (menu_id, user_id) → role_id, enforces Binding
     permanence independent of current roles.
   - `src/bot_modules/role_menus/db.py` — sync fns taking `conn`, docs/db.py
     style. Options are saved as a full ordered replace (positions from array
     order) — no per-option CRUD or reorder endpoints.

2. **Mode engine** — `src/bot_modules/role_menus/logic.py`, pure + table-tested:
   `resolve_click(mode, held, clicked, …)` and `resolve_selection(mode, held,
   selected, …)` → `(adds, removes, error)` with max_roles/binding/cooldown
   inputs. All five modes; dropdown-unique/binding constrained to single pick;
   dropdown submission = "selection becomes your set" in toggle mode, add-only
   in verify, remove-only in drop.

3. **Views + interaction handling** — `src/bot_modules/role_menus/views.py`:
   `DynamicItem` button (`rolemenu:btn:{menu_id}:{option_id}`) and select
   (`rolemenu:sel:{menu_id}`); `build_view(menu, options)` for posting.
   Callback path: defer(ephemeral) → load menu/options via `asyncio.to_thread`
   → guards (enabled, required role, in-memory per-(menu,user) cooldown, mode
   engine, bot manage_roles + top_role ladder from `role_grant_commands.
   _execute_grant`) → `add_roles`/`remove_roles` → grants history + compact
   mod-log line (`🎭 @user +Night Owl −Early Bird (Colors)`, mod_channel_id,
   AllowedMentions.none()) → ephemeral confirmation per spec §2.1 table.
   Failure caused by config drift (role deleted, hierarchy) → polite member
   message + **one** mod alert per menu (alerted_at gate, reset on healthy
   sync).

4. **Sync engine** — `src/bot_modules/role_menus/sync.py` (docs/sync.py
   pattern, single message per menu): `publish_menu`, `sync_menu` (edit in
   place), `unpublish_menu` (keep post, strip/disable components, enabled=0),
   `delete_menu_message`; `menu_health(guild, menu, options)` → issues list
   (`role_missing`, `role_above_bot`, `message_missing`, `channel_missing`)
   surfaced by the panel with fix paths.

5. **Cog** — `src/bot_modules/cogs/role_menus_cog.py`: thin; `cog_load`
   registers dynamic items. No slash commands. Registered in
   `src/dungeonkeeper/__main__.py` `extension_names`.

6. **Web routes** — `src/web_server/routes/role_menus.py` (`require_perms
   ({"moderator"})`, registered in `server.py`): CRUD (PUT = full menu +
   options replace, auto-syncs live message like docs PUT), `POST /{id}/
   publish {channel_id}`, `/{id}/unpublish`, `/{id}/enabled`, `DELETE /{id}`,
   `GET /role-menus/roles` (assignable = below bot top role, unmanaged;
   dangerous-permission roles flagged and hidden client-side unless the
   elevated override is checked). Every mutation → `write_audit`
   (`role_menu.*` actions; elevated override logged loudly as its own
   action).

7. **Panel** — `src/web_server/static/js/panels/role-menus.js` (docs.js
   clone): list (title, channel/Draft, style+mode badges, role count, on/off
   toggle), editor form + option rows (up/down reorder, free-text emoji,
   label, role select from `/role-menus/roles`, color or description),
   client-side live preview reusing `dp-*` classes + new `rm-*` button/select
   mocks, publish bar (channel picker, Publish / Update live / Unpublish /
   Delete with confirm). Nav: SECTIONS entry next to Docs. `rm-*` CSS
   appended to `app.css`. gjs `Reflect.parse` check.

8. **Tests + docs** — pure-logic tables for the mode engine; db round-trip;
   route tests on `tests/web` fixtures (list/create/update/duplicate/roles
   endpoint/audit rows); health + sync unit tests with fakes. INDEX.md entry
   (Design spec), `scripts/gate.py` green. QA cards post automatically from the
   commit's Testing: section (TESTING_QUEUE.md retired 2026-07-18).

## Decisions inherited from the product spec

- Unpublish keeps the post as decor (components disabled), Delete removes
  post + menu; grants history is kept either way.
- Same role in two menus is fine; Unique polices only its own menu.
- Lowering max_roles never strips existing holders; cap applies to new grants.
- 25 options/menu hard cap (platform ceiling), editor counts down.
- No reactions, no paid roles, no expiring roles in v1.

## Out of scope / parking lot

- Guild-emoji picker for options (free-text only in v1).
- Petal-priced roles (leave visual room for a price tag on option rows).
- Menu templates/cloning, adoption analytics, >25-option menus.
