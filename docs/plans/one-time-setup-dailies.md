# One-time setup quests in the daily pool (the welcome guide)

**Status:** code landed · **Owner:** economy · **Spec:** `docs/economy_spec.md` §4
(the "One-time setup quests" note, right after the removed onboarding path)

## Goal

Driven by user request: surface the one-time member-setup tasks (fill out your
bio, set your birthday) **inside the random daily quest board** — only for
members who haven't done them yet — so they act as a subtle, pull-not-push
welcome guide. The sanctioned successor to the removed join-time onboarding DM
(which pushed the economy at members who hadn't opted in).

Product decisions (asked 2026-07-18): steps = **bio + birthday**; reward =
**same as a normal daily**.

## Why it needed real design, not just a new quest

`bio_set` / `birthday_set` fire on *save/update* with a constant occurrence
`"set"`, and the member action is once-in-a-lifetime. A naive daily quest on
those kinds breaks two ways:

1. **Re-earn farm.** A plain daily claims per calendar day, so re-saving your
   bio each day would re-earn it. (This is exactly why the kinds were
   event-only before.)
2. **Board-timing leak / ordering trap.** The board is a stateless
   deterministic draw; a once-ever action done on a day the quest wasn't drawn
   would never pay, and completion state exists *before* the fire hook runs, so
   gating the fire on `has_bio` would block the very claim that completes it.

## Model (implemented)

`quests.SETUP_QUEST_KINDS = {bio_set, birthday_set}` (pure, single source of
truth). A **daily** quest on one of these kinds gets two service-layer
special-cases:

1. **Claim once ever, board-independent** — `fire_trigger_quests` claims a
   setup-kind board quest on the constant period `"<kind>:set"` (not the
   calendar day) and skips the board-membership gate. Completing member always
   paid once; re-saves collide on the constant key and pay nothing; no
   dependency on `has_bio`, so no ordering trap.
2. **Hide once done, no refill** — `assigned_board_ids` drops a setup quest
   from a member's board when `_setup_quest_done` (underlying `bios` /
   `member_birthdays` row exists, or a prior paid claim). Drop **without**
   refill so a completed slot never reshuffles the window and strands a counted
   quest's progress. Only members who haven't done it ever see it.

Supporting: setup quests are excluded from the clear-the-board set-bonus
requirement (their `:`-keyed claim isn't in any day's board set, and it also
guards `maybe_pay_set_bonus` against feeding a non-calendar period to the
board math); rerolls won't swap a member into a completed setup quest; the
`birthday_set` display label was added (`bio_set` already had one).

## Code changes

- `economy/quests.py`: `SETUP_QUEST_KINDS` constant.
- `services/economy_quests_service.py`: `_setup_underlying_done`,
  `_setup_quest_done`, `_setup_kinds_by_id`, `_drop_completed_setup`;
  `assigned_board_ids` applies the drop; `fire_trigger_quests` setup branch;
  `maybe_pay_set_bonus` `:`-period guard + setup exclusion; `reroll_board_slot`
  skips completed setup quests.
- `cogs/economy_cog.py`: `birthday_set` state label.

## Tests

`tests/test_economy_quests_service.py` — claims once-ever not per-day
(bug-fix-first: a plain daily would re-earn); pays even with an empty board;
drops off the board once the bio/birthday exists; drops after claim even if the
row is later deleted; birthday parity; excluded from the set bonus; setup claim
doesn't crash the set-bonus math; kinds registered.

## Enablement (live-money data step — user runs it)

The two kinds already exist as **event** quests in the main guild
(`1469491362444480666`): "Introduce Yourself" (`bio_set`, id 26, reward 50) and
"Cake Day on File" (`birthday_set`, id 39, reward 25). **Convert them to daily**
rather than adding new dailies — an event + a daily on the same kind would pay
twice per save. Conversion preserves claim history, so members who already did
it neither re-earn (same `quest_id` + constant period → claim collision) nor get
re-nudged (`has_bio` / ever-claimed hides it).

```sql
-- reward = normal daily band (adjust to taste; they were 50 / 25 as one-time
-- event rewards). XP mirrors the other dailies (8).
UPDATE econ_quests SET qtype='daily', reward=15, reward_xp=8 WHERE id=26; -- bio
UPDATE econ_quests SET qtype='daily', reward=15, reward_xp=8 WHERE id=39; -- bday
```

Alternatively add them fresh via the dashboard Quests page (kind = Bio / Birthday,
cadence = daily) and deactivate the two event quests — same end state.

## Out of scope / parking lot

- More setup steps (pen-pals opt-in, first perk customization) — the predicate
  registry (`_setup_underlying_done`) is where a new kind's check would slot in.
- A dashboard affordance that flags a kind as "one-time setup" instead of the
  hard-coded `SETUP_QUEST_KINDS` set (only two today; revisit if it grows).
