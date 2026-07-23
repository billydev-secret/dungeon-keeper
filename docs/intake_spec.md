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
  `greeted` (greeter-role member @mentions the newcomer in the intake
  channel), `verified` (unverified role removed), `role_gained` (member
  gains the step's configured role — `/grant` or a manual add; `role_id 0`
  never ticks, and the dashboard refuses to store it). Step keys are
  normalized to `[\w-]` and capped at 64 chars on save so persistent-button
  custom_ids always fullmatch the dispatch template after a restart.
- **Completion:** a greeter/mod message in **any channel** containing
  `intake_completion_code` and @mentioning the newcomer. Unticked steps
  are stamped **skipped** (code always wins), the poster becomes the
  welcomer of record, the card flips to "🎉 Intake complete", 🎉 reaction
  on the trigger message. Empty code = detection off.
- **Close paths:** completion, mod-only Dismiss button, member leave
  (`left`) or ban (`banned` — the ban hook closes first so the remove hook
  finds nothing). **No expiry**: cards otherwise stay open; the queue is
  always the truth.
- **Stale nudge:** `intake_loop` (10-min tick) replies once under any open
  card with no step progress for `intake_stale_hours` (default 24; any
  tick resets the clock), pinging the greeter role; `nudged_at` stamps
  even on send failure so it can't re-nudge.
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
(the question lists stay conversational by design).

## Procedure reference

Blocks (`intake_reference_blocks` config JSON: `text` | `questions`, title,
body) render to messages — text chunks under the 2000-char cap; questions
one message per line with an optional bold header. Sync
(`intake_reference_service.sync_channel`, run inline on dashboard save) is
a position-wise diff against `intake_reference_messages` (migration 116):
unchanged kept, changed edited in place (ids/links stable), tail posted,
surplus deleted, hand-deleted tracked messages reposted. Only tracked
messages are ever touched. One-time import
(`POST /config/intake/reference/import`) drafts text blocks from a
channel's history, oldest first; refuses a non-empty editor.

## Dashboard

- Config: `PUT /config/intake` (strict validation; steps ≤ 20, keys
  slugged + deduped, stable across re-saves), `PUT
  /config/intake/reference`, import endpoint. Panel `config-intake.js`.
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
| Panels | `static/js/panels/config-intake.js`, `intake-report.js` |
| Tests | `tests/test_intake_logic.py`, `test_intake_views.py`, `test_intake_reference_logic.py`, `tests/web/test_config_routes.py`, `test_reports_routes.py` |

See also: [greeting_watch_spec.md](greeting_watch_spec.md),
[role_grant_spec.md](role_grant_spec.md),
[promotion_review_spec.md](promotion_review_spec.md) (the display idiom the
cards mirror), [auto_role_spec.md](auto_role_spec.md).
