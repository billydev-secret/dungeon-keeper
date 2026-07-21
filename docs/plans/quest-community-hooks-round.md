# Quest round: community hooks (7 new trigger kinds)

**Status:** built + seeded 2026-07-21 · **Owner:** economy · **Spec:** `docs/economy_spec.md` §4.5

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

## Seeding (done 2026-07-21, main guild)

Library ids 51–57, calibrated on trailing 28–35d prod data (guess ~5
submissions/wk, confession replies ~10/28d, birthdays ~1/mo announced):

| id | title | qtype | reward | kind | state |
|---|---|---|---|---|---|
| 51 | Hello Back | daily | 12 | greeting_answered | **dark** — Greeting Watch has no watched channels; activate after configuring it |
| 52 | Cake Day Cheer | event | 15 | birthday_wish | active |
| 53 | Pouch Snatcher | event | 5 | drop_claim | active (pays once drops get a channel) |
| 54 | Feed the Pool | weekly | 40 | guess_submit | active |
| 55 | Echo in the Dark | weekly | 35 | confession_reply | active |
| 56 | Pick Your Colors | daily setup | 25 | role_pick | **dark** — only a "test" role menu exists; activate with a real menu |
| 57 | First Purchase | daily setup | 25 | shop_purchase | active |

Also repaired quest 47 "Round Master": its `guess_post` trigger kind never
existed in `TRIGGER_KINDS` (dead since seeding); now `guess_submit`, still
dark with the 47–50 paired-dailies batch. Seed script in the session
scratchpad (`seed_round3_quests.py`). Rewards follow library conventions
(setup 25 like birthday_set; weeklies 35–40; events 5–15).

## Community extension (done 2026-07-21)

- **Greeting Watch configured** (main guild): watching 💛│the-meadow +
  🕊️│welcome-chat, 10-min window, notify member unset (0 = no DM; pending
  rows still record/retire, which is all the quest detector needs — pick a
  notify member on the dashboard to get the "left hanging" alerts). Quest 51
  Hello Back activated with it.
- **Community rotation rows seeded** (ids 58–62, reward 10/tier, inactive —
  the gap-week scheduler activates them): Nobody Greets Alone
  (greeting_answered), Stock the Pool (guess_submit), Shopping Spree
  (shop_purchase), Leave No Pouch Behind (drop_claim), Voices from the Dark
  (confession_reply). `next_community_weekly` orders by `last_run_week, id`,
  so these queue behind never-run 42–46: on-weeks land W31→42 … W39→46,
  **W41→58 onward** — by which time `econ_kind_activity` has real 4-week
  history for auto-sizing (floor 10 backstops regardless).
- **Anonymous-kind guard** shipped for the confession row:
  `ANON_COMMUNITY_KINDS` (confession, confession_reply, whisper) pay flat
  tiers only — no top-contributor bonus, name-free beat sheet.
- Not extended: birthday_wish (too lumpy for weekly targets — a birthday-day
  flash goal would need a 24 h community cadence, future round), role_pick
  (once-ever setup shape; a "N members made their first pick" distinct-member
  variant is a possible one-off campaign).
