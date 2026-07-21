# Quest round: community hooks (7 new trigger kinds)

**Status:** in progress 2026-07-21 · **Owner:** economy · **Spec:** `docs/economy_spec.md` §4.5

## Goal

Third quest-variety round, aimed squarely at community engagement: reward the
member who answers a hello, wishes a happy birthday, feeds the Guess Who pool,
engages with confessions, catches a coin drop, or takes their first steps into
the server's role menus and perk shop. Follows the pattern locked in
`quest-variety-and-community-weeklies.md` (kind + hook + occurrence key + spec
row + tests; every fire feeds `econ_kind_activity`, so any kind can later
headline a community weekly).

Driven by user selection 2026-07-21 from a docs sweep of quest-less features.
All seven are quest-board-only — no public pings; trigger claims are silent by
construction (the `confession` kind precedent).

## The kinds

| kind | fires when | hook site | occurrence key |
|---|---|---|---|
| `greeting_answered` | member replies to / @mentions someone whose greeting is still pending in Greeting Watch (same channel) | `events_cog._econ_work` via `greeting_watch_service.pending_greetings_for` | `greeting_answered:<greeting_message_id>` |
| `birthday_wish` | member wishes a happy birthday on a day a birthday was **announced**: a reply/mention of the birthday member, or a birthday-wish phrase (`is_birthday_wish`) anywhere | `events_cog._econ_work`, gated on a `birthday_announcements` row for the guild-local day | `birthday_wish:<target_id>:<local_day>` (phrase path: `birthday_wish:day:<local_day>`) |
| `drop_claim` | member wins a coin-drop Claim race | `economy_drops_service.try_claim_drop` (service layer, after the credit) | `drop_claim:<drop_id>` |
| `guess_submit` | member's Guess Who submission posts as a round (✓ Post in the crop editor) | `guess_cog.CropEditorView._on_post` | `guess_submit:<round_id>` |
| `role_pick` | member self-assigns a role via a role menu or an announcement role button | `role_menus/views._apply_outcome` (grants only) + `announcements/buttons._apply` (grant path) | `role_pick:set` (setup kind — once ever) |
| `confession_reply` | member posts an anonymous reply to someone **else's** confession (OP self-replies never fire) | `confessions_cog.ReplyModal.on_submit` after the reply posts | `confession_reply:<reply_message_id>` |
| `shop_purchase` | member makes a shop purchase: perk rental, streak shield, emoji sponsorship, QOTD sponsorship, raffle tickets | each purchase service beside its `apply_debit` (renewal billing deliberately NOT hooked — no credit for an automatic charge) | `shop_purchase:set` (setup kind — once ever) |

## Design decisions

- **`role_pick` and `shop_purchase` join `SETUP_QUEST_KINDS`** (the
  `bio_set`/`birthday_set` pattern): claim once ever on a constant period,
  always pay on completion even if not drawn on today's board, drop off the
  board once done. Underlying-done checks: any `role_menu_grants` grant row
  (announcement-button grants aren't recorded — the paid-claim backstop covers
  those pickers), any `econ_ledger` row with a purchase kind
  (`rental`, `streak_shield`, `emoji_sponsor`, `qotd_sponsor`, `raffle_ticket`).
- **`birthday_wish` privacy gate:** wishes only count when the birthday was
  publicly announced (a `birthday_announcements` row for today) — members with
  a quiet/unset birthday never become quest bait. Pre-announcement (before
  09:00 local) wishes don't count; documented soft edge. The wisher must not be
  the birthday member; the phrase path skips when the author is the only
  announced birthday. One fire per message (target path wins over phrase path).
- **`greeting_answered` self-gates** on Greeting Watch config: no watched
  channels → no pending rows → no fires. Answering counts while the row is
  unresolved (the loop resolves shortly after the window closes, so "pending"
  ≈ "within the window"). The greeter answering someone else's greeting counts;
  answering your own can't happen (self-edges are excluded at ingest).
- **`drop_claim` fires in the service** (`try_claim_drop`), not the button
  callback, so the logic layer owns it and tests hit it directly. Double-pays
  beside the drop credit by design — the `cat_catch`/`qotd_reply` precedent.
- **`confession_reply`** keeps the confession privacy contract: fires only for
  non-OP repliers, credited privately (no channel noise; the only trace is the
  member's own quest log + staff ledger).
- **No migration.** Every detector reads existing tables.
- **Rejected in this round** (recorded so we don't relitigate): `gift_sent`
  (pair ping-pong = money printer, the `reaction_received` collusion shape),
  sink-usage kinds beyond the first-purchase setup nudge (paying people to
  spend undoes the sink), comeback/inactive-return (perverse incentive),
  wellness/privacy/DM-perms (never incentivize).

## Stages

1. Kinds registered (`TRIGGER_KINDS`/`TRIGGER_KIND_INFO`/`SETUP_QUEST_KINDS`)
   + service helpers (`pending_greetings_for`, `is_birthday_wish`,
   `announced_birthday_ids`, setup underlying-done branches).
2. Hooks: events_cog (greeting/birthday), drops service, guess post,
   confession reply, role menus + announcement buttons, five purchase sites.
3. Spec §4.5 rows + this plan doc; tests per hook (service-layer where the
   logic lives, per the testing standard).

Seeding/activating library quests on these kinds is a follow-up (the
2026-07-13-style calibrated seed), decided with the user once live.
