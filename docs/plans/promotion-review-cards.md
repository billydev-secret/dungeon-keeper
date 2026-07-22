# Promotion-review cards — Grant-access buttons + return/sleeper triggers

## Goal

The roles manager needs a card in the **promotion-reviews channel** (the
existing **Level 5 Log Channel**, `level_5_log_channel_id`) with an action
button, whenever someone who lost access shows signs of life again. Extend the
*existing* promotion-review system — do **not** build a parallel one.

## Existing system (do not duplicate)

- `maybe_log_level_5` (`xp_service.py`) posts an informational card to
  `level_5_log_channel_id` when a member reaches Level 5. No buttons today.
- Deferred posts park in `pending_promotion_posts` (migration 085) and fire via
  `promotion_review_recheck_loop`.
- `role_prune_events` (migration 098) = the auto-sweep role-removal ledger.
- `inactive_members` (migration 057) = the inactive/"sleeper" hold; held members
  live in `inactive_channel_id` (the sleeper chat). `active_inactive_user_ids`.

## Triggers (all post the same card type to `level_5_log_channel_id`)

1. **Level 5 reached** — existing card; add the Grant button.
2. **Access-pruned member returns** — member with an open `role_prune_events`
   row posts a message anywhere.
3. **Sleeper wakes** — member in `active_inactive_user_ids` posts in
   `inactive_channel_id`.

## Card + button

- One embed style; a persistent **Grant access** button (DynamicItem, survives
  restarts) + a **Dismiss** button on the return/sleeper cards.
- Grant action: see the open decision below. Records who actioned it, closes the
  member's open prune events / (optionally) reactivates, resolves the card.
- Dedup: `promotion_review_cards` (migration 112) — one open card per member.
  Message hot path filters with an O(1) in-memory watch set, seeded at startup
  and fed by the prune sweep + inactive-hold path; the DB is the source of truth.

## Config (dashboard, not Discord)

- Reuse `level_5_log_channel_id` for the channel — no new channel setting.
- New `promotion_review_grant_role_id` = the role the Grant button adds
  (in this server, the NSFW/spicy access role).
- Ships **dark** for the return/sleeper triggers until the grant role is set.

## Open decision (see AskUserQuestion)

Grant button on a **sleeper** card: add the single configured role, or fully
**reactivate** (remove the Inactive role, restore stored roles via the existing
inactive-reactivate flow)? Level-5 and pruned-return cards grant the configured
role either way.

## Stage map

- S1: `promotion_review_service` (ledger, config, gating, watch set) + tests. ✅ built
- S2: `promotion_review_views` (embed, Grant/Dismiss DynamicItems, post/resolve). ✅ built
- S3: message hot-path hooks (prune-return + sleeper-chat) + startup warm + prune-sweep feed. ◑ partial
- S4: retrofit Grant button onto the Level 5 card (`maybe_log_level_5`).
- S5: dashboard grant-role setting (config.py section + PUT + panel + nav) + tests.
- S6: docs — manual.html, README, docs/INDEX.md spec.
