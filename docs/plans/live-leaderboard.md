# Live leaderboard — the economy centerpiece

**Status:** built (all stages, 2026-07-18) · **Owner:** economy · **Spec:** `docs/economy_spec.md` §7
(leaderboard panel bullet) · **Extends:** quest-variety stage 4
(`docs/plans/quest-variety-and-community-weeklies.md`)

## Goal

Turn the `/bank post-leaderboard` panel from an hourly snapshot into a live
status board — the thing members watch to see the game moving. User ask
(2026-07-18): "extend the economy leaderboard to show live status updates …
go full hawg on it, it's the centerpiece of the game."

Two halves: **richer content** (what's happening right now) and **faster
refresh** (an event-driven, debounced in-place edit so the panel moves within
a couple of minutes of the action, not on the next hour).

## Constraints carried forward

- **Anonymous ticker** (locked 2026-07-18): live-activity lines are aggregates
  — quest titles, counts, timestamps — never member names. Top earners keep
  names (that's the leaderboard's job); the *feed* stays nameless.
- No new tables, no migration: everything derives from `econ_ledger`,
  `econ_quest_claims`, `econ_community_progress/_contrib`, and
  `econ_kind_activity`.
- Collector + builder stay pure (`economy/leaderboard.py`); Discord I/O stays
  in the cog and the loops.
- Countdowns render as Discord relative timestamps (`<t:…:R>`) — they tick
  client-side, so the panel reads "live" even between edits.

## Stage 1 — collector + builder ("what's happening now")

`collect_leaderboard_data` gains (one sync read, same conn):

- **Pulse** — guild-local *today*: coins paid (positive ledger ex
  `transfer_in`), quests completed (paid claims), distinct members who earned.
- **Today deltas** — today's income per top-5 earner → "(+X today)" suffix.
- **Community goals** (auto weeklies) — contributor count, tier thresholds
  (40/70/100% of target, `ceil`), daily-bucket pace (expected = target ×
  elapsed_days/7; on-track ≥ 90% of expected — same rule as `compute_live`),
  today's contribution delta from `econ_kind_activity` (only when the quest
  has no channel scope — the activity table can't see scope), and the
  week-end deadline timestamp. Manual goals render exactly as before.
- **Live feed** — today's completions aggregated per quest (title, ×count,
  latest timestamp), newest first, capped at 5, plus a full-board
  (`quest_bonus`) count. Empty state keeps the field warm.
- **Clocks** — next guild-local day roll (dailies reset) and ISO-week roll
  (new weeklies + spotlight flip) as epoch seconds.

Embed layout (top to bottom): pulse field → top earners (+today) → community
goals (bar, tier line, pace line, ends `<t:R>`) → quest board (spotlight line
gains "until `<t:R>`") → live feed → "Your progress". Footer becomes
"Live — updates within ~2 min of activity", keeping the edit timestamp.

## Stage 2 — event-driven debounced refresh

- **`economy/live_signal.py`** — tiny import-free module: a process-local
  dirty-guild set (`mark_dirty` / `take_ready`). Deliberately in-memory: a
  lost signal costs at most one hour (the hourly tick is the backstop).
- **Producers** — `economy_service.apply_credit` (every payout: quests, set
  bonuses, conversions, games, grants), `_bump_community_kind` (community
  progress moves without a payout), `set_community_progress` (dashboard
  manual edits). A mark inside a transaction that later rolls back just
  causes one harmless refresh.
- **Consumer** — `leaderboard_live_loop` (economy_loop.py, registered in
  `__main__.py`): polls every ~20 s, refreshes a dirty guild via the existing
  `run_guild_leaderboard`, at most once per 120 s per guild (well inside
  Discord edit limits; a burst coalesces into one edit ≤2 min later). A guild
  marked mid-refresh is picked up next tick. Guilds without a posted panel
  exit in one settings read.
- Hourly `run_guild_leaderboard` stays as-is — backstop for restarts and
  quiet-period drift.

## Stage 3 — docs

`economy_spec.md` leaderboard bullet rewritten (live cadence + content),
manual.html `/bank post-leaderboard` row + cog reply copy, this plan marked
built. QA cards post automatically from the commit's Testing: section
(TESTING_QUEUE.md retired 2026-07-18).

## Risks

- **Refresh-vs-commit race:** a mark is visible before its transaction
  commits; the 120 s debounce makes reading stale data unlikely and the
  hourly tick corrects it regardless.
- **Deanonymization in a small guild:** a per-quest "×1 · just now" line can
  sometimes be correlated with visible activity. Accepted: it reveals only
  that *some* member completed a titled quest — no names, and payout amounts
  shown are the quest's public reward, not the member's wallet.
- **Embed budget:** worst case (5 goals + 12 quest lines + 5 feed lines)
  stays under the 1024-per-field and 6000-total limits; the board cap and
  feed cap are the guards.
