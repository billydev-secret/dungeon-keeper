# Promotion-review cards

**Status:** Reference (built). Extends the existing Level 5 promotion card.

The **Level 5 Log Channel** (`xp_level_5_log_channel_id`) is the
promotion-reviews channel. Three triggers post a review card there, each
carrying a persistent **Grant access** button (survives restarts) so a roles
manager can action a return without leaving Discord — no slash commands.

## Triggers

| Kind | Fires when | Grant button does |
|------|-----------|-------------------|
| Level 5 | Member reaches `role_grant_level` (existing card in `maybe_log_level_5`) | Adds `promotion_review_grant_role_id` |
| `pruned_return` | A member with an **open** `role_prune_events` row (auto-sweep removed a role, migration 098) posts a message **anywhere** | Adds `promotion_review_grant_role_id`, then `restored_at` on their open prune events |
| `sleeper` | A member currently held inactive (`inactive_members`, migration 057) posts in the **sleeper channel** (`inactive_channel_id`) | Full `reactivate_member` — restores stored roles, removes `@Inactive` |

The `pruned_return` and `sleeper` cards also carry a **Dismiss** button.

## Gating / config (dashboard, not Discord)

- Ships **dark**: the return/sleeper triggers do nothing until the Level 5 Log
  Channel is set. `pruned_return` additionally requires
  `promotion_review_grant_role_id` (else its button would no-op).
- Both settings live on the XP config panel (`config-xp.js`,
  `PUT /api/config/xp`).
- Buttons are limited to admins/mods or Manage Roles.

## Mechanics

- **Ledger:** `promotion_review_cards` (migration 112). One **open**
  (`resolved_at IS NULL`) card per member — a partial unique index enforces the
  dedup so multiple messages never spawn multiple cards. `kind` drives the Grant
  action; resolving records `resolved_by` + `resolution`
  (`granted`/`reactivated`/`dismissed`). The Level 5 card is **not**
  ledger-backed (its button is keyed by member id).
- **Hot path:** `on_message` filters with an O(1) in-memory watch set
  (`promotion_review_service.is_watched`), seeded at startup (`warm`) and fed by
  the prune sweep (`note_pruned`) and the inactive-hold path (`note_inactive`).
  The DB (`evaluate_trigger`) is the source of truth; the watch set is only a
  cheap pre-filter, so a stale entry is harmless.
- Posting reserves the card slot **before** the Discord send; a failed send
  rolls the reservation back and leaves the member on the watch set to retry.

## Code

- Service (ledger + gating + watch set): `services/promotion_review_service.py`
- Embed + persistent buttons: `services/promotion_review_views.py`
- Level 5 card: `services/xp_service.py` (`maybe_log_level_5`)
- Hooks: `cogs/events_cog.py` (`on_message`), `inactivity_prune_service.py`,
  `inactive/apply.py`, `dungeonkeeper/__main__.py` (warm + button registration)

See also: [role_grant_spec.md](role_grant_spec.md), [inactive_spec.md](inactive_spec.md), [xp_spec.md](xp_spec.md).
