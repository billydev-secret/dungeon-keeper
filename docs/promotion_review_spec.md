# Promotion-review cards

**Status:** Reference (built). Extends the existing Level 5 promotion card.

The **Level 5 Log Channel** (`xp_level_5_log_channel_id`) is the
promotion-reviews channel. Three triggers post a review card there, each
carrying a persistent **Grant access** button (survives restarts) so a roles
manager can action a return without leaving Discord — no slash commands.

## Triggers

All three cards ping `promotion_review_ping_role_id` when it is set.

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
- These settings live on the XP config panel (`xp-settings.js`,
  `PUT /api/config/xp`).
- Buttons are limited to admins/mods or Manage Roles.

### The two roles are different settings

| Key | Role it names | Used for |
|-----|---------------|----------|
| `promotion_review_grant_role_id` | The role handed **to** the promoted member | What the Grant button adds (`_do_grant_role`) |
| `promotion_review_ping_role_id` | Your **role managers** | Pinged when a card posts, so a human sees it |

Neither is the approver gate — that is `_can_action`: administrator, Manage
Roles, or a configured mod. A card pings the ping role only; leave it "(none)"
and all three cards post silently, as they did before this shipped.

The mention goes in the message **content** (a role mention inside an embed
renders but never notifies) and allow-lists exactly that one role via
`ping_send_kwargs`. Note `discord.AllowedMentions`' unset fields default to
*allow*, so `everyone`/`users`/`replied_user` are pinned `False` explicitly —
the bare `AllowedMentions(roles=[...])` form still serializes
`parse: ['everyone', 'users']`.

## Mechanics

- **Ledger:** `promotion_review_cards` (migration 112). One **open**
  (`resolved_at IS NULL`) card per member — a partial unique index enforces the
  dedup so multiple messages never spawn multiple cards. `kind` drives the Grant
  action; resolving records `resolved_by` + `resolution`
  (`granted`/`reactivated`/`dismissed`). The Level 5 card is **not**
  ledger-backed (its button is keyed by member id).
- **Level 5 card refresh:** the Level 5 card's **Spicy access** field is the one
  live element on an otherwise at-promotion snapshot. `xp_level_5_cards`
  (migration 141) stores where each card was posted, and the `on_member_update`
  role diff in `events_cog` re-renders just that field whenever the NSFW grant
  role (`grant_roles["nsfw"]`) is added or removed — so the card tracks access
  granted by `/grant` or by a hand-added role, neither of which it could see
  before. **Two independent settings:** the field reports `grant_roles["nsfw"]`,
  while the Grant button hands out `promotion_review_grant_role_id`. The button
  therefore refreshes the card only when those are the same role; set to a
  different role it grants successfully and the field correctly doesn't move.
  Deliberately a separate table from `promotion_review_cards`: a Level 5 card
  never resolves, so it would squat that table's one-open-card-per-member slot
  forever. Cards whose message *or* channel has been deleted are forgotten (a
  cache miss is confirmed with a fetch first, so a card in an archived thread is
  kept). The refresh never raises, and is spawned **detached**
  (`asyncio.create_task`) so its two HTTP round trips neither delay the
  verified-welcome / greeter ping nor get skipped when that later work raises.
  Refreshes are **serialized per member** (`_member_refresh_lock`): two quick
  toggles otherwise race, the loser no-ops against a not-yet-edited card, and the
  winner's stalled edit lands the older state permanently.
  **Best-effort on events only, not eventually consistent:** a role change while
  the bot is down is never replayed, so that card stays wrong until the member's
  next NSFW role change. Nothing reconciles it — a repair pass on
  `promotion_review_recheck_loop` (which already owns deferred Level 5 posting)
  is the natural home if that becomes a problem. Cards posted before migration
  141 have no stored row and stay stale — fix-forward only.
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
- Level 5 card refresh: `promotion_review_views.refresh_spicy_field` (pure) +
  `refresh_level_5_cards` (Discord edit)
- Hooks: `cogs/events_cog.py` (`on_message`, `on_member_update`),
  `inactivity_prune_service.py`,
  `inactive/apply.py`, `dungeonkeeper/__main__.py` (warm + button registration)

See also: [role_grant_spec.md](role_grant_spec.md), [inactive_spec.md](inactive_spec.md), [xp_spec.md](xp_spec.md).
