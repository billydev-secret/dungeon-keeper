# Wellness: one ephemeral panel in Discord (the bank paradigm)

**Status: proposal — not built; partly obsolete.** The 2026-08-28 readiness
review deleted two building blocks this plan leans on: `_SettingsView` (dead
code, cut) and the whole partners system (`wellness_partners.py`, migration
188) — re-scope before acting on it. Written 2026-07-30 during the wellness review,
at Ben's request: "look at how we could move [to] the panels-with-buttons
paradigm we're using on the bank."

## The paradigm, as the bank actually builds it

Three pieces, all live in the economy code today:

1. **A persistent channel panel** — an embed posted to a configured channel
   with a `discord.ui.View(timeout=None)` whose buttons carry **static
   custom_ids**, re-registered on every boot in `cog_load` via
   `bot.add_view(...)` (`economy_cog.py:3981-3983` — `GuideView`,
   `QuestBoardView`, `ShopPanelView`). Buttons survive restarts because the
   custom_id, not the view instance, is the identity.
2. **Buttons serve the clicker a personal ephemeral menu** — and it is *the
   same menu the slash command opens* ("one shop menu, so the panel can't
   drift from it", `ShopPanelView` docstring). The panel is a launcher, never
   a second implementation.
3. **`DynamicItem` with a regex custom_id template** for per-entity buttons
   (`ShopRentButton`, `econ_shop_panel:(?P<perk>...)`) so stale panels keep
   working across restarts. Wellness already uses this shape for partner
   accept/decline DMs (`wellness_partners.make_partner_request_view`,
   registered in `__main__.py`).

Posting the panel is an **admin dashboard action** (Economy → Settings → "Post
to Discord"), not a slash command — consistent with CLAUDE.md's
config-on-the-web rule.

## What wellness gets

One "🌿 Wellness Guardian" panel in the configured wellness channel
(`wellness_config.channel_id`), managed like the active-list embed (stored
message id, edit-else-repost). Buttons:

| Button | Behavior |
|---|---|
| **🌱 Join / ⚙️ My Settings** | Branches on the clicker's opt-in state: not opted in → the existing `_SetupWizardView` ephemeral wizard; opted in → an ephemeral settings menu (see below). One button, two states — collapse controls. |
| **🤝 Find a Partner** | Ephemeral with a native `discord.ui.UserSelect` member picker → the existing partner-request DM flow. Replaces the web Partners page's raw-Discord-ID paste (enable developer mode, copy ID…), which is the one place the dashboard is clearly the *wrong* surface. The web page keeps list/dissolve. |
| **📊 Open Dashboard** | A plain `discord.ui.Button(url=...)` link button (no callback needed) via `wellness_dashboard_link()`'s URL — caps, blackouts, history. Hidden when no public URL is configured. |
| **🚪 Leave** | Ephemeral confirm (same consequence copy as the web dialog) → the same `opt_out_user` + role-removal path as `POST /optout`. The exit becomes reachable from Discord — important for exactly the member the dashboard has failed. |

The **settings menu** revives the dead `_SettingsView`
(`wellness_cog.py:296-423`, currently 128 unreferenced lines) as the
slash/panel-shared ephemeral menu:

- Enforcement select — keep its friendly labels + descriptions
  (`ENFORCEMENT_LABELS` / `ENFORCEMENT_DESCRIPTIONS`); these are currently
  *only* shown during setup, while the web select shows raw enum values.
  (Port the labels to the web select as part of this work.)
- Notifications select — with honest labels: "In-channel (visible for ~30s)" /
  "DM only" / "Both", not the current bare enums.
- Public-commitment toggle — a Discord-native consent control for the public
  streak list (defaults off as of 2026-07-30).
- Away on/off + message (absorbs `/wellness away set`'s dial; the command
  stays as a shortcut).
- Pause / Resume nudges.

## What stays web-only (deliberately)

Caps (histogram + sliders), blackout editor, weekly history. These are
configuration-shaped — multi-field, visual, iterated-on — exactly what
CLAUDE.md sends to the dashboard. The 2026-07 UX audit's verdict on the ~19
never-built slash commands was that the cut was correct; this panel adds the
missing *connective tissue* (launcher, exit, partner picker, link), not the
command sprawl.

## Slash surface after this lands

- `/wellness setup` — unchanged (discoverability for brand-new members).
- `/wellness away set` — unchanged shortcut into the same away dial.
- `/wellness panel` *(optional, decide at build time)* — summons your personal
  ephemeral menu from any channel, for members who never visit the wellness
  channel. Zero new state; same menu object.

## Build notes

- Static custom_ids: `wellness_panel:join`, `wellness_panel:partner`,
  `wellness_panel:leave`. Register one `WellnessPanelView` in `cog_load`.
- The opt-out and settings writes reuse the existing service functions —
  no new logic layer, so tests target the existing
  `test_wellness_service.py` surface plus one wiring assertion for the
  panel's branch-on-opt-in button.
- Panel posting: a "Post wellness panel" button on the wellness-admin
  dashboard page (mirrors Economy → Settings → Post to Discord), storing a
  `panel_message_id` in `wellness_config` beside `active_list_message_id`.
- Sticky-follow (bottom-of-channel repost) is **not** needed: the wellness
  channel is low-traffic and the active list is already pinned. Skip the
  sticky machinery entirely.
- Estimated size: ~250-400 lines in the cog (mostly the revived settings
  menu), ~40 in the admin route/panel for posting, small `wellness_config`
  addition. No migration (upsert already tolerates new columns via ALTER
  pattern used elsewhere) — verify at build time.
