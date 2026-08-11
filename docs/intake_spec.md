# Intake cards (welcome tracker) + procedure reference

**Status:** Reference (built). Plan: [plans/intake-cards.md](plans/intake-cards.md).

Two halves, both dashboard-configured (Config → Members → Intake Cards), no
slash commands:

1. **Intake cards** — on join, a card posts to greeter chat tracking the
   welcome procedure as a checklist; the open cards are the intake queue.
2. **Procedure reference** — the `#welcome-procedure` content is
   dashboard-edited blocks the bot syncs into a channel, question lists
   rendered **one message per question** for one-tap copy-paste.

## Cards

- **Post:** `on_member_join` (humans only; jailed rejoiners skipped) when
  `intake_enabled` + a channel resolves (`intake_channel_id`, falling back
  to `greeter_chat_channel_id`). Content line pings `greeter_role_id`
  (allow-listed); embed carries a `▰▱` progress bar
  (`economy.leaderboard.progress_bar`), ✅/⬜/⏭️ checklist lines (done shows
  who/when; `done_by 0` renders "auto"), account age, invited-by
  (`invite_edges`). The legacy bare `@here — has arrived` ping is
  suppressed only when a card surface actually exists: the join variant
  falls back to the ping whenever the card could not post (channel
  missing/send failed — `post_intake_card` returns False), and the
  verified-trigger variant suppresses per-member (`is_watched`, i.e. an
  open card), not per-guild. While dark, join behavior is unchanged.
- **Steps** are snapshotted onto the card at creation from `intake_steps`
  (JSON; invalid entries drop, empty/invalid falls back to the default
  six-step list — whose two role steps are **manual** until real roles are
  configured, since an unconfigured `role_gained` step could never tick) —
  config edits never mutate in-flight cards. Kinds: manual (persistent
  toggle button, greeters + mods, first ticker preserved on races),
  `greeted` (a greeter **or mod** @mentions/replies to the newcomer in the
  **greeting channel** — `greeter_chat_channel_id`, else
  `welcome_channel_id`; 0 means nothing greets), `verified` (verification
  signalled: `unverified_role_id` removed **or**
  `intake_verified_role_id` gained), `role_gained` (member
  gains the step's configured role — `/grant` or a manual add; `role_id 0`
  never ticks, and the dashboard refuses to store it). Step keys are
  normalized to `[\w-]` and capped at 64 chars on save so persistent-button
  custom_ids always fullmatch the dispatch template after a restart.
- **Where greeting is watched** is deliberately *not* the card channel.
  Cards post to a greeter-facing noticeboard; the greeting itself happens
  where the newcomer landed. Watching `intake_channel_id` meant `greeted`
  could never tick in practice (prod, 2026-07-29: every greeting was in
  #welcome-chat while cards posted to the tracker channel), so
  `greet_channel_id()` resolves `greeter_chat_channel_id` first — a room
  dedicated to handling arrivals — then `welcome_channel_id`. Mods count
  alongside greeters: the greeter role gates nothing else here either.
- **Verification wears two shapes** and both signal the step
  (`verification_signalled()`): our own gate *strips* `unverified_role_id`,
  while a third-party verifier (Double Counter) *grants* a role instead —
  `intake_verified_role_id`, admin-only, 0 = unset. Watching only the
  removal left `verified` dead on guilds using the second shape, which is
  every guild whose verification isn't ours. A verified role that is also
  a `role_gained` step's role ticks both steps; that's intended.
- **Step codes:** each step may carry its own free-text `code`. A
  greeter/mod message in **any channel** containing it, addressed to the
  newcomer, ticks **that step only** — the card stays open. The point is
  that each canned message in the procedure reference can carry the code
  for the step it corresponds to, so pasting the message ticks the step
  without anyone touching the card. One message may tick several steps;
  an empty code never matches. Codes are matched against the **live**
  config rather than the card's snapshot (a code is a lookup phrase, not
  per-card state, so fixing a typo doesn't strand in-flight cards) — a
  code whose step isn't on this card simply ticks nothing. Matching is
  case-insensitive containment, so a save where one code contains another
  (or contains the completion code) is rejected: it would silently fire
  both, and a completion code swallowed by a step code is worse than a
  double-tick — pasting that step's message would close the card and stamp
  the rest skipped. Step codes and the completion code are writable
  independently, so `intake_service.config_code_conflict` (which reads
  whichever side the caller isn't overriding) is enforced at **both**
  writers: the dashboard route, and `advisor_actions.validate_config_change`
  for the advisor, which writes the key straight through
  `set_config_value`.
- **Completion:** a greeter/mod message in **any channel** containing
  `intake_completion_code` and addressed to the newcomer. Wins over step
  codes (the card is closing anyway). Unticked steps are stamped
  **skipped** (code always wins), the poster becomes the welcomer of
  record, the card flips to "🎉 Intake complete", 🎉 reaction on the
  trigger message. Empty code = detection off.
- **"Addressed to the newcomer"** means an @mention **or a reply to one of
  their messages**. Discord only lists the reply target in
  `message.mentions` when the reply pings, so both the `on_message`
  pre-filter and `evaluate_message`'s mention set fold in
  `intake_service.reply_target_id` — otherwise a canned reply with its ping
  off is dropped before intake ever evaluates it. Applies to greets and
  both kinds of code alike.
- **Close paths:** completion, mod-only Dismiss button, member leave
  (`left`) or ban (`banned` — the ban hook closes first so the remove hook
  finds nothing). **No expiry**: cards otherwise stay open; the queue is
  always the truth.
- **Payouts:** ticking a step pays the greeter through the `intake_step`
  income source (`economy/intake_rewards.pay_intake_steps`, called from all
  three human tick paths — the `greeted` auto-tick, a posted step code, and
  the manual toggle button — riding the tick's own transaction). Per *step*, not per completed card — completion credits
  whoever posted the code, so a per-card award would miss shared and
  half-finished intakes. Only ticks with a real actor pay: `verified` and
  `role_gained` record `AUTO_ACTOR` (0) and credit nobody, as do skipped
  steps. Dedup is `econ_intake_rewards (guild_id, card_id, step_key)`
  (migration 138) — **not** the step's `done_at`/`done_by`, which the toggle
  button clears, so unticking and re-ticking mints nothing and a second
  greeter cannot re-claim a ticked step. Each award is savepointed so an
  economy failure can never roll back the tick. See economy_spec.md §4.5.
- **Stale nudge:** `intake_loop` (10-min tick) replies once under any open
  card with no step progress for `intake_stale_hours` (default 24; any
  tick resets the clock), pinging the greeter role; `nudged_at` stamps
  even on send failure so it can't re-nudge.
- **Screening-aware:** a stale card is checked against the member's actual
  state before the ping (`intake_service.nudge_action`, presence resolved
  from the member cache with a `fetch_member` fallback). A member still in
  Discord's membership screening (`Member.pending`) holds no roles and can't
  be greeted, so the card is **skipped unstamped** — it pings later, once
  accepting makes greeting possible. A member who has left (an explicit 404,
  never a transient error) had their leave missed by `on_member_remove`, so
  the sweep closes the orphan card as `left` instead of pinging. Anything
  unresolvable is skipped unstamped and re-decided next tick.
- **Hot path:** `on_message` pre-filters via an O(1) watch set of members
  with open cards (`intake_service.is_watched`, seeded at startup, same
  pattern as promotion review); decisions live in
  `intake_service.evaluate_message`. The warm seed covers open cards in
  **all** guilds, enabled or not, so cards survive a disable → restart →
  enable cycle; hook calls in `events_cog` are individually guarded so an
  intake failure can never abort spoiler enforcement, persistence, or the
  leave announcement.

## Ledger

`intake_cards` + `intake_card_steps` (migration 115). One open card per
(guild, member) via partial unique index; resolving records
`resolved_by`/`resolution` (`completed`/`dismissed`/`left`/`banned`).
Steps carry `done_at`/`done_by`/`skipped` — no message content, no answers
(the question lists stay conversational by design). `econ_intake_rewards`
(migration 138) anchors the per-step payout: one row per (guild, card, step),
`user_id`/`amount` recorded for attribution only — the PK excludes them so a
re-tick by anyone pays nothing.

## Procedure reference

Blocks (`intake_reference_blocks` config JSON: `text` | `questions`, title,
body) render to messages. A block's **title is always its own bold
message** — Discord's Copy Text takes the whole message, and most text
blocks are canned messages a greeter copy-pastes, so a heading sharing the
message means trimming it off every paste. Below the heading: text chunks
on line boundaries under the 2000-char cap (rejoined with the newline they
were split on, so the channel matches the editor); questions one message
per line, and a question longer than the cap is rejected on save rather
than 400-ing mid-sync. Either half is omitted when empty (a title-only
block is a bare section heading). Sync
(`intake_reference_service.sync_channel`, run inline on dashboard save) is
a position-wise diff against `intake_reference_messages` (migration 116):
unchanged kept, changed edited in place (ids/links stable), tail posted,
surplus deleted. Only tracked messages are ever touched.

**Hand-deleted messages.** Each sync first sweeps the channel history once
(`after` the oldest tracked message, stopping as soon as every tracked id
is accounted for — normally inside one request, versus a fetch per
position) to learn which tracked ids still exist. Without that check an
unchanged config hashes `keep` at every position and the sync makes **no
Discord calls at all** — which is how a deleted message used to stay gone
however often you saved. Discord can't insert into the middle of a channel,
so the diff rebuilds from the *first* gap onward (re-send + delete the
stale copies) to preserve reading order; positions above the gap keep their
ids. A gap at or past the rendered range is ignored — that message is
surplus the delete pass removes anyway. `repaired` counts replacements that
actually landed, not ones merely planned.

A sweep Discord refuses, or one that hits the 500-message backstop, assumes
the unseen ids are **present** (guessing would delete and re-send a stretch
of live messages) and returns `incomplete: true` so the panel doesn't
report a clean sync — a deletion in the unread tail is simply not noticed
that pass.

A Discord failure mid-plan keeps the affected message tracked under its
**old** hash (so the next save retries it rather than believing it already
synced) and also sets `incomplete`. Note the delete pass still deletes ids
that a failed rebuild left in the mapping, and that pairing is what makes
the next save finish the job: the following sweep sees them missing and
rebuilds from there. Skipping those deletes instead leaves stale copies
whose hashes still match, so the next sync finds nothing to do and the
channel reads out of order permanently. One-time import
(`POST /config/intake/reference/import`) drafts text blocks from a
channel's history, oldest first; refuses a non-empty editor.

## Dashboard

- Config: `PUT /config/intake` (strict validation; steps ≤ 20, keys
  slugged + deduped, stable across re-saves), `PUT
  /config/intake/reference`, import endpoint. Panel `intake-settings.js`.
- Analytics: `GET /reports/intake-report` → Reports → Greeter → **Intake
  Queue** (`intake-report.js`): open queue oldest-first with progress and
  pending steps, outcome counts + median/mean time-to-complete,
  per-welcomer completions and manual ticks (auto-ticks never credited),
  per-step skip rates on completed cards.

## Code

| Piece | File |
|---|---|
| Ledger + gating + reports (unit under test) | `services/intake_service.py` |
| Embed + persistent buttons + hook handlers | `services/intake_views.py` |
| Stale-nudge loop | `services/intake_loop.py` |
| Reference blocks/render/diff/sync/import | `services/intake_reference_service.py` |
| Hooks | `cogs/events_cog.py` (join/remove/ban/member-update/message), `dungeonkeeper/__main__.py` (warm + `add_dynamic_items` + loop) |
| Routes | `web_server/routes/config.py`, `web_server/routes/reports.py` |
| Panels | `static/js/panels/intake.js` (composes `intake-report.js` + `intake-settings.js`) |
| Tests | `tests/test_intake_logic.py`, `test_intake_views.py`, `test_intake_reference_logic.py`, `tests/web/test_config_routes.py`, `test_reports_routes.py` |

See also: [greeting_watch_spec.md](greeting_watch_spec.md),
[role_grant_spec.md](role_grant_spec.md),
[promotion_review_spec.md](promotion_review_spec.md) (the display idiom the
cards mirror), [auto_role_spec.md](auto_role_spec.md).
