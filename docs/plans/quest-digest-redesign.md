# Quest digest redesign — daily-login DM

**Status:** in progress (branch `quest-digest-redesign`)

Reworks the daily-login DM's quest checklist (the "Daily Streak" digest sent
by `events_cog._econ_login_embed`). Driven by a live-server request: the old
single-line-per-quest list ran the bar/counts at ragged columns, capped at 6
quests with "…and N more", and carried no context.

## Goals

1. **Aligned bars.** Bars + counts render inside a monospace code span so they
   line up down the column. (A full monospace table was rejected — it can't
   hold bold, channel links, or blurbs.)
2. **Show every open quest**, including the member's full personal board — no
   6-item cap, no "…and N more" tail.
3. **A blurb under each quest** — from the quest's `description`, with a light
   per-cadence fallback when a quest has none.
4. **Channel links** — when a quest is scoped to a channel
   (`econ_quests.trigger_channel_id`), render `<#id>` in its blurb.
5. **Biggest movers yesterday** — a new section listing the community goals
   that advanced the most on the previous guild-local day.

## Data: daily community-progress snapshots

Community progress is a single cumulative `current` per quest — no per-day
history — so "yesterday's gain" needs new tracking.

- Migration `118_econ_community_progress_snapshots.sql`:
  `econ_community_progress_snapshots(guild_id, quest_id, day, current)`,
  PK `(quest_id, day)`.
- `snapshot_community_progress(conn, guild_id, day)` records each active
  community quest's `current` for `day` (INSERT OR REPLACE → idempotent).
- Hooked into `economy_loop.run_guild_day_roll` at the **start of the day-roll
  block**, keyed to `last_day` (the day that just ended), **before** any weekly
  settlement zeroes `current`. Replay-safe like the rest of the roll.
- `community_gains_for_day(conn, guild_id, day, limit=3)` diffs
  `snapshot(day) − snapshot(previous_local_day(day))`, positive-only, sorted
  desc. Empty (section omitted) until both snapshots exist — so a member who
  logs in before the hourly roll just sees no movers yet.

## Formatting: `bot_modules/economy/quest_digest.py` (new, pure)

No Discord objects — the cog turns returned `(name, value)` pairs into embed
fields, so the layout is unit-tested directly.

- `digest_sections(quests_out, gains)` → `list[(field_name, field_value)]`:
  optional movers field first, then open quests grouped by cadence
  (Daily / Weekly / Monthly / Community / Anytime), each block = title + bar +
  blurb, packed into ≤1024-char fields (`… (cont.)` on overflow).
- `bar_fill` extracted into `leaderboard.py` and shared with `progress_bar`
  (output unchanged) so the digest's spaced meter reuses the fill math.

## Touch list

- `src/migrations/118_econ_community_progress_snapshots.sql` (new)
- `bot_modules/economy/quests.py` — `previous_local_day`
- `bot_modules/economy/leaderboard.py` — extract `bar_fill`
- `bot_modules/economy/quest_digest.py` (new)
- `bot_modules/services/economy_quests_service.py` — surface
  `trigger_channel_id` in `load_member_quest_board`; add
  `snapshot_community_progress`, `community_gains_for_day`
- `bot_modules/services/economy_loop.py` — snapshot at day roll
- `bot_modules/cogs/events_cog.py` — rebuild `_econ_login_embed` on
  `quest_digest`; drop the old `_quest_recap_*` helpers
- Tests: `tests/test_quest_digest.py` (new), additions to
  `test_economy_quests_service.py` and `test_events.py`
- Docs: `docs/economy_spec.md`, `manual.html` quest section
