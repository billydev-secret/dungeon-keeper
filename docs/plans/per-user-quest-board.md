# Per-user quest board

**Status:** in progress · **Owner:** economy · **Spec:** `docs/economy_spec.md` §4

## Goal

Replace the guild-wide "one active daily + shared weekly/monthly set" model with
a **personal quest board**: each member sees and can earn their *own* subset of
the guild's active quests, refreshed each period, with repeats spaced out so a
member tends not to see the same quest twice within ~a week.

Driven by user request: "make the quests individual per user … random but the
repeats are less likely for a week-ish," board size **2** per cadence.

## Model

- The guild's **active** daily/weekly/monthly quests form three **pools**. Every
  active quest is a pool member (the old `MAX_ACTIVE_DAILY = 1` slot cap and the
  `rotate_tag` daily rotation are retired — see below).
- Each member is assigned **N = 2** quests per cadence per period, drawn
  deterministically from that cadence's pool.
- **Community and event quests are unchanged** — community is a guild-wide
  objective by design; event quests pay per occurrence with no calendar period.
  Only daily/weekly/monthly get personalized.

### Selection: per-user shuffled sequence (no new table)

A member's set is a pure function of `(pool_ids, user_id, period_index, n)`:

1. Deterministically shuffle the pool per user — sort by
   `sha256(f"{user_id}:{quest_id}")`. Stable across processes/Python versions
   (unlike `random.shuffle`), and different per user.
2. Walk the shuffled list N-at-a-time indexed by `period_index`: window
   `start = (period_index * n) % len`, take `n` cycling.

A quest can't recur until the member has cycled the whole pool, so the **repeat
gap ≈ floor(poolsize / n) periods**. With a daily pool of ~6 and n=2 that's ~3
days; true 7-day spacing needs a ~14-quest daily pool (documented, not blocking).
`n >= poolsize` returns the whole pool (everyone sees everything — graceful
degenerate case for small pools).

`period_index(qtype, local_day)`: daily → `date.toordinal()`; weekly →
`iso.year*53 + iso.week`; monthly → `year*12 + (month-1)`. Monotonic, one integer
per period; assignment cadence therefore equals the quest's claim period, so
counted-quest progress never fragments.

## Behavior change (intended)

A member only earns a kind when the matching quest is in **their** board this
period. Sending a message on a day their board has no message quest → no coins
for it. This is the point of personalizing; called out in the spec.

## Code changes

1. **`economy/quests.py`** (pure, table-tested):
   - `assigned_quest_ids(pool_ids, user_id, period_index, n) -> list[int]`
   - `period_index(qtype, local_day) -> int`
   - Retire the daily slot cap: `MAX_ACTIVE_DAILY` → a generous pool cap (20) to
     match weekly/monthly, so managers can fill the pool. `can_activate` follows.
2. **`services/economy_quests_service.py`**:
   - `list_active_pool_ids(conn, guild_id, qtype) -> list[int]` — active quest
     ids of a cadence.
   - `assigned_pool_ids(conn, guild_id, user_id, qtype, local_day, n)` — thin
     wrapper: pool → `assigned_quest_ids`. Memoize-friendly.
   - **`fire_trigger_quests`**: for daily/weekly/monthly quests, skip any quest
     not in the member's assigned set for its cadence+period (compute the set
     once per cadence encountered). Event quests unaffected.
3. **`cogs/economy_cog.py` `_load_quests_state`**: filter the daily/weekly/monthly
   rows to the member's assigned sets before building entries. Community/event
   pass through.
4. **`PERSONAL_BOARD_SIZE = 2`** module constant (per-cadence tunable dict), used
   by both wiring points. Dashboard config knob is a parking-lot follow-up.

## Data rollout (prod, user-confirmed — the live-money step)

- Activate all 15 seeded quests (all become pool members).
- Clear `rotate_tag` on the 5 dailies (kills the now-obsolete rotation; with a
  cleared tag `rotate_pool` no-ops).
- Add **"Show & Tell"** daily — `media_post` ×1 (always-winnable; image activity
  is high).
- **Drop "Question Answered" from the daily pool** (leave inactive) — QOTD isn't
  posted daily, so a member assigned it on a no-QOTD day would be stuck.

## Tests

- Pure: determinism, per-user divergence, no-repeat-until-cycle, stable within a
  period, `n>=pool` returns all, empty pool → empty.
- `period_index` monotonic + distinct per period per cadence.
- Integration: `fire_trigger_quests` fires only assigned quests; `_load_quests_state`
  shows only the member's board; community/event unaffected.

## Out of scope / parking lot

- Dashboard knob for board size (constant for v1).
- Larger daily pool for true 7-day repeat spacing.
- Per-user weekly/monthly N tuning (2 for all in v1).
